"""DTOs de leitura do histórico (independentes da sessão do SQLAlchemy)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from orquestrador.dispatch.models import RunOutcome


class RunStatus(StrEnum):
    """Estado de uma execução, job ou step no histórico.

    Reúne os estados do runner (``Status``) e ``error`` — falha antes de o
    pipeline rodar (checkout, YAML inválido, fila indisponível).
    """

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        """Se o estado é final."""
        return self in {
            RunStatus.SUCCESS,
            RunStatus.FAILURE,
            RunStatus.SKIPPED,
            RunStatus.CANCELLED,
            RunStatus.ERROR,
        }


def run_status_for(outcome: RunOutcome) -> RunStatus:
    """Converte o desfecho de um ``RunService`` no status da execução."""
    return RunStatus(outcome.final_status)


class LogLineRecord(BaseModel):
    """Linha de log persistida."""

    seq: int
    stream: Literal["stdout", "stderr", "system"]
    text: str
    timestamp: datetime


class StepRecord(BaseModel):
    """Step persistido."""

    index: int
    name: str
    status: RunStatus
    exit_code: int | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float | None = None
    allowed_failure: bool = False
    log_lines: int = 0
    truncated_lines: int = 0
    group: str | None = None


class JobRecord(BaseModel):
    """Job persistido, com seus steps."""

    job_id: str
    name: str
    status: RunStatus
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float | None = None
    allowed_failure: bool = False
    steps: list[StepRecord] = []


class RunSummary(BaseModel):
    """Resumo de uma execução (listagens)."""

    run_id: str
    project: str
    provider: str
    repository: str
    event: str
    ref: str | None = None
    branch: str | None = None
    tag: str | None = None
    commit: str
    actor: str | None = None
    base_branch: str | None = None
    pull_request: int | None = None
    message: str | None = None
    pipeline: str | None = None
    status: RunStatus
    reason: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float | None = None


class RunDetail(RunSummary):
    """Execução completa, com jobs e steps."""

    jobs: list[JobRecord] = []


class RunPage(BaseModel):
    """Página de resultados de uma listagem."""

    items: list[RunSummary]
    total: int
    limit: int
    offset: int
