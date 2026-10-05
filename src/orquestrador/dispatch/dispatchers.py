"""Dispatchers: desacoplam o recebimento do evento da execução do pipeline.

A API só chama ``dispatcher.dispatch(request)`` e responde imediatamente.
Implementações:

* ``SyncDispatcher`` — executa na hora (testes e uso embutido).
* ``ThreadDispatcher`` — executa num pool de threads do próprio processo.
* ``RQDispatcher`` (Fase 4) — enfileira no Redis para workers separados.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from orquestrador.dispatch.models import RunOutcome, RunRequest

logger = logging.getLogger(__name__)

RunHandler = Callable[[RunRequest], RunOutcome]


class Dispatcher(ABC):
    """Destino dos pedidos de execução."""

    @abstractmethod
    def dispatch(self, request: RunRequest) -> str:
        """Agenda a execução.

        Args:
            request: Pedido de execução.

        Returns:
            O ``run_id`` agendado.

        Raises:
            Exception: Se não for possível agendar (ex.: fila indisponível).
        """

    def shutdown(self, *, wait: bool = True) -> None:  # noqa: B027 - optional by design
        """Libera recursos (padrão: nada a fazer).

        Args:
            wait: Aguarda as execuções em andamento terminarem.
        """

    def stats(self) -> dict[str, Any]:
        """Métricas exibidas no health check (padrão: apenas o tipo)."""
        return {"kind": type(self).__name__}


class SyncDispatcher(Dispatcher):
    """Executa o pedido imediatamente, na thread de quem chamou."""

    def __init__(self, handler: RunHandler) -> None:
        """Cria o dispatcher.

        Args:
            handler: Função que processa o pedido (ex.: ``RunService.execute``).
        """
        self.handler = handler
        self.outcomes: list[RunOutcome] = []

    def dispatch(self, request: RunRequest) -> str:
        """Executa o pedido e guarda o desfecho em ``outcomes``."""
        self.outcomes.append(self.handler(request))
        return request.run_id

    def stats(self) -> dict[str, Any]:
        """Quantidade de execuções processadas."""
        return {"kind": "sync", "processed": len(self.outcomes)}


class ThreadDispatcher(Dispatcher):
    """Executa pedidos em background num pool de threads limitado."""

    def __init__(self, handler: RunHandler, *, max_workers: int = 2) -> None:
        """Cria o dispatcher.

        Args:
            handler: Função que processa o pedido.
            max_workers: Execuções simultâneas.
        """
        self.handler = handler
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="orq-run")
        self._futures: dict[str, Future[RunOutcome | None]] = {}
        self._lock = threading.Lock()

    def dispatch(self, request: RunRequest) -> str:
        """Submete o pedido ao pool e retorna sem esperar."""
        future = self._pool.submit(self._run, request)
        with self._lock:
            self._futures[request.run_id] = future
        future.add_done_callback(lambda _done, run_id=request.run_id: self._forget(run_id))
        return request.run_id

    def _run(self, request: RunRequest) -> RunOutcome | None:
        try:
            return self.handler(request)
        except Exception:
            logger.exception("[%s] erro inesperado ao processar a execução", request.run_id)
            return None

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)

    @property
    def active(self) -> int:
        """Quantidade de execuções em andamento ou aguardando no pool."""
        with self._lock:
            return len(self._futures)

    def stats(self) -> dict[str, Any]:
        """Execuções em andamento ou aguardando no pool."""
        return {"kind": "thread", "active": self.active}

    def shutdown(self, *, wait: bool = True) -> None:
        """Encerra o pool; sem ``wait``, cancela o que ainda não começou."""
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
