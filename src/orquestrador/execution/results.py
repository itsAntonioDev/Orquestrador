"""Resultados de execução (pipeline, jobs e steps).

São modelos Pydantic para poderem ser serializados em JSON diretamente —
útil para o relatório da CLI, a fila de jobs e a API/dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Retorna o instante atual com fuso UTC."""
    return datetime.now(UTC)


class Status(StrEnum):
    """Estado de um pipeline, job ou step."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Se o estado é final (não muda mais)."""
        return self in {Status.SUCCESS, Status.FAILURE, Status.SKIPPED, Status.CANCELLED}


class LogLine(BaseModel):
    """Uma linha de saída produzida por um step.

    Attributes:
        stream: Origem da linha (``stdout``, ``stderr`` ou ``system``).
        text: Conteúdo, sem a quebra de linha final.
        timestamp: Momento em que a linha foi recebida.
    """

    stream: Literal["stdout", "stderr", "system"]
    text: str
    timestamp: datetime = Field(default_factory=utcnow)


class StepResult(BaseModel):
    """Resultado de um step.

    Attributes:
        index: Posição do step dentro do job.
        name: Nome do step.
        status: Estado final ou atual.
        exit_code: Código de saída do comando (``None`` se não executou).
        stdout: Saída padrão capturada.
        stderr: Saída de erro capturada.
        output: Saída combinada, na ordem de chegada.
        error: Descrição do motivo da falha, se houver.
        started_at: Início da execução.
        finished_at: Fim da execução.
        duration: Duração em segundos.
        allowed_failure: Se a falha é tolerada (``continue-on-error``).
        group: Grupo paralelo ao qual o step pertence (``None`` se sequencial).
    """

    index: int
    name: str
    status: Status = Status.PENDING
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float = 0.0
    allowed_failure: bool = False
    group: str | None = None


class JobResult(BaseModel):
    """Resultado de um job.

    Attributes:
        job_id: ID do job.
        name: Nome do job.
        status: Estado final ou atual.
        steps: Resultados dos steps, na ordem de declaração.
        error: Motivo da falha, se houver.
        started_at: Início da execução.
        finished_at: Fim da execução.
        duration: Duração em segundos.
        allowed_failure: Se a falha é tolerada (``continue-on-error``).
    """

    job_id: str
    name: str
    status: Status = Status.PENDING
    steps: list[StepResult] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float = 0.0
    allowed_failure: bool = False

    @property
    def succeeded(self) -> bool:
        """Se o job conta como sucesso para quem depende dele."""
        return self.status == Status.SUCCESS or (
            self.status == Status.FAILURE and self.allowed_failure
        )


class PipelineResult(BaseModel):
    """Resultado da execução completa de um pipeline.

    Attributes:
        run_id: Identificador único da execução.
        pipeline: Nome do pipeline.
        status: Estado final ou atual.
        jobs: Resultados indexados por ID do job.
        started_at: Início da execução.
        finished_at: Fim da execução.
        duration: Duração em segundos.
    """

    run_id: str
    pipeline: str
    status: Status = Status.PENDING
    jobs: dict[str, JobResult] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float = 0.0

    @property
    def succeeded(self) -> bool:
        """Se o pipeline terminou com sucesso."""
        return self.status == Status.SUCCESS
