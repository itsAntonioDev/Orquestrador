"""Atualizações ao vivo de uma execução via WebSocket.

O servidor consulta o banco periodicamente e envia apenas o que mudou:

* ``{"type": "run", "run": {...}}`` — estado completo quando algo muda;
* ``{"type": "logs", "job_id", "step_index", "lines": [...]}`` — linhas novas;
* ``{"type": "end"}`` — execução finalizada e todos os logs enviados;
* ``{"type": "error", "message"}`` — execução inexistente ou histórico indisponível.

Consultar o banco (em vez de Redis pub/sub) mantém a API e os workers
desacoplados: basta que ambos enxerguem o mesmo banco. O intervalo padrão
(0,5 s) coincide com o lote de gravação de logs do ``PersistenceObserver``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from orquestrador.db import RunRepository

logger = logging.getLogger(__name__)

router = APIRouter()

#: Close code when the server cannot handle the request (RFC 6455).
CLOSE_INTERNAL_ERROR = 1011


class RunLiveTracker:
    """Calcula os eventos novos de uma execução entre duas consultas ao banco."""

    def __init__(self, repository: RunRepository, run_id: str, *, log_batch: int = 1000) -> None:
        """Cria o rastreador.

        Args:
            repository: Histórico de execuções.
            run_id: Execução acompanhada.
            log_batch: Máximo de linhas enviadas por step a cada consulta.
        """
        self.repository = repository
        self.run_id = run_id
        self.log_batch = log_batch
        self.finished = False
        self.missing = False
        self._last_snapshot: str | None = None
        self._cursors: dict[tuple[str, int], int] = {}

    def poll(self) -> list[dict[str, Any]]:
        """Consulta o banco e retorna os eventos desde a última chamada."""
        run = self.repository.get_run(self.run_id)
        if run is None:
            self.finished = True
            self.missing = True
            return [{"type": "error", "message": "execução não encontrada"}]

        events: list[dict[str, Any]] = []
        payload = run.model_dump(mode="json")
        snapshot = json.dumps(payload, sort_keys=True)
        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            events.append({"type": "run", "run": payload})

        logs_pending = False
        for job in run.jobs:
            for step in job.steps:
                key = (job.job_id, step.index)
                cursor = self._cursors.get(key, 0)
                if step.log_lines <= cursor:
                    continue
                lines = self.repository.get_logs(
                    self.run_id, job.job_id, step.index, after=cursor, limit=self.log_batch
                )
                if not lines:
                    continue
                self._cursors[key] = lines[-1].seq
                if lines[-1].seq < step.log_lines:
                    logs_pending = True
                events.append(
                    {
                        "type": "logs",
                        "job_id": job.job_id,
                        "step_index": step.index,
                        "lines": [line.model_dump(mode="json") for line in lines],
                    }
                )

        self.finished = run.status.is_terminal and not logs_pending
        return events


@router.websocket("/ws/runs/{run_id}")
async def run_updates(websocket: WebSocket, run_id: str) -> None:
    """Transmite o progresso da execução até ela terminar."""
    await websocket.accept()
    repository: RunRepository | None = getattr(websocket.app.state, "repository", None)
    if repository is None:
        await websocket.send_json({"type": "error", "message": "persistência desativada"})
        await websocket.close(code=CLOSE_INTERNAL_ERROR)
        return

    interval: float = websocket.app.state.settings.live_poll_interval
    tracker = RunLiveTracker(repository, run_id)
    try:
        while True:
            for event in await run_in_threadpool(tracker.poll):
                await websocket.send_json(event)
            if tracker.finished:
                if not tracker.missing:
                    await websocket.send_json({"type": "end"})
                await websocket.close()
                return
            await asyncio.sleep(interval)
    except (WebSocketDisconnect, RuntimeError):
        logger.debug("cliente desconectou do acompanhamento da execução %s", run_id)
