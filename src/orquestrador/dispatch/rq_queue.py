"""Fila de execuções com Redis + RQ.

A API enfileira um ``RunRequest`` (em JSON) e responde na hora; um ou mais
processos ``orquestrador worker`` consomem a fila e chamam ``RunService``.
Escalar = subir mais workers (ex.: ``docker compose up --scale worker=4``).

Usamos o ``JSONSerializer`` do RQ em vez de pickle: o conteúdo da fila fica
legível e um Redis comprometido não consegue injetar objetos Python
arbitrários nos workers.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, SimpleWorker, Worker
from rq.serializers import JSONSerializer
from rq.timeouts import TimerDeathPenalty

from orquestrador.dispatch.dispatchers import Dispatcher
from orquestrador.dispatch.models import RunRequest

if TYPE_CHECKING:
    from orquestrador.config import Settings, WorkerClass
    from orquestrador.dispatch.service import RunService

logger = logging.getLogger(__name__)

#: Caminho importável da tarefa executada pelos workers.
TASK_PATH = "orquestrador.dispatch.rq_queue.execute_run_request"


def redis_connection(url: str) -> Redis:
    """Cria uma conexão Redis a partir de uma URL (``redis://host:6379/0``)."""
    return Redis.from_url(url)


def create_queue(connection: Redis, name: str, *, is_async: bool = True) -> Queue:
    """Cria a fila RQ com serialização JSON.

    Args:
        connection: Conexão Redis.
        name: Nome da fila.
        is_async: ``False`` executa as tarefas na hora (útil em testes).

    Returns:
        A fila.
    """
    return Queue(name, connection=connection, serializer=JSONSerializer, is_async=is_async)


class RQDispatcher(Dispatcher):
    """Enfileira execuções no Redis para workers separados."""

    def __init__(
        self,
        queue: Queue,
        *,
        job_timeout: int = 3 * 3600,
        result_ttl: int = 24 * 3600,
        failure_ttl: int = 7 * 24 * 3600,
    ) -> None:
        """Cria o dispatcher.

        Args:
            queue: Fila RQ de destino.
            job_timeout: Tempo máximo de uma execução no worker (segundos).
            result_ttl: Por quanto tempo o resultado fica no Redis.
            failure_ttl: Por quanto tempo execuções com erro ficam no Redis.
        """
        self.queue = queue
        self.job_timeout = job_timeout
        self.result_ttl = result_ttl
        self.failure_ttl = failure_ttl

    @classmethod
    def from_settings(cls, settings: Settings) -> RQDispatcher:
        """Cria o dispatcher a partir de ``ORQ_REDIS_URL``, ``ORQ_QUEUE_NAME`` etc."""
        queue = create_queue(redis_connection(settings.redis_url), settings.queue_name)
        return cls(
            queue,
            job_timeout=settings.run_timeout,
            result_ttl=settings.result_ttl,
            failure_ttl=settings.failure_ttl,
        )

    def dispatch(self, request: RunRequest) -> str:
        """Enfileira o pedido; o ``run_id`` vira o ID do job no RQ.

        Raises:
            RedisError: Se o Redis estiver indisponível (a API responde 503).
        """
        trigger = request.trigger
        job = self.queue.enqueue(
            TASK_PATH,
            request.model_dump(mode="json"),
            job_id=request.run_id,
            job_timeout=self.job_timeout,
            result_ttl=self.result_ttl,
            failure_ttl=self.failure_ttl,
            description=f"{request.project}: {trigger.event} {trigger.commit[:10]}",
            meta={
                "project": request.project,
                "repository": trigger.repository,
                "event": trigger.event,
                "commit": trigger.commit,
            },
        )
        logger.info("[%s] enfileirado em '%s'", job.id, self.queue.name)
        return str(job.id)

    def stats(self) -> dict[str, Any]:
        """Tamanho da fila, execuções em andamento, falhas e workers ativos."""
        try:
            return {
                "kind": "rq",
                "queue": self.queue.name,
                "queued": self.queue.count,
                "started": self.queue.started_job_registry.count,
                "failed": self.queue.failed_job_registry.count,
                "workers": Worker.count(queue=self.queue),
            }
        except RedisError as exc:
            return {"kind": "rq", "queue": self.queue.name, "error": str(exc)}


_service: RunService | None = None
_service_lock = threading.Lock()


def configure_worker_service(service: RunService | None) -> None:
    """Define (ou limpa) o ``RunService`` usado pelas tarefas deste processo."""
    global _service
    with _service_lock:
        _service = service


def get_worker_service() -> RunService:
    """``RunService`` do processo worker, montado uma única vez a partir do ambiente."""
    global _service
    with _service_lock:
        if _service is None:
            from orquestrador.bootstrap import build_run_service
            from orquestrador.config import get_settings

            _service = build_run_service(get_settings())
        return _service


def execute_run_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Tarefa RQ: processa um ``RunRequest`` serializado e devolve o ``RunOutcome`` em JSON.

    Args:
        payload: ``RunRequest.model_dump(mode="json")``.

    Returns:
        ``RunOutcome.model_dump(mode="json")``.
    """
    request = RunRequest.model_validate(payload)
    outcome = get_worker_service().execute(request)
    return outcome.model_dump(mode="json")


def create_worker(
    connection: Redis,
    queue_names: Sequence[str],
    *,
    worker_class: WorkerClass | str = "auto",
    name: str | None = None,
) -> Worker:
    """Cria um worker RQ para as filas informadas.

    ``fork`` executa cada execução num processo filho (isola vazamentos de
    memória; padrão no Linux). ``simple`` executa no próprio processo (única
    opção no Windows). ``auto`` escolhe conforme o sistema.

    Args:
        connection: Conexão Redis.
        queue_names: Filas consumidas, em ordem de prioridade.
        worker_class: ``auto``, ``fork`` ou ``simple``.
        name: Nome do worker (padrão: gerado pelo RQ).

    Returns:
        O worker, pronto para ``work()``.
    """
    kind = str(worker_class)
    use_simple = kind == "simple" or (kind == "auto" and not hasattr(os, "fork"))
    worker_type: type[Worker] = SimpleWorker if use_simple else Worker
    queues = [create_queue(connection, queue_name) for queue_name in queue_names]
    worker = worker_type(queues, connection=connection, serializer=JSONSerializer, name=name)
    if not hasattr(signal, "SIGALRM"):
        worker.death_penalty_class = TimerDeathPenalty
    return worker
