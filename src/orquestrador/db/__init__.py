"""Persistência do histórico de execuções (SQLAlchemy + Alembic; PostgreSQL ou SQLite)."""

from orquestrador.db.listener import DatabaseRunListener
from orquestrador.db.observer import PersistenceObserver
from orquestrador.db.repository import NewLogLine, PipelineIds, RunRepository
from orquestrador.db.schemas import (
    JobRecord,
    LogLineRecord,
    RunDetail,
    RunPage,
    RunStatus,
    RunSummary,
    StepRecord,
)
from orquestrador.db.session import Database

__all__ = [
    "Database",
    "DatabaseRunListener",
    "JobRecord",
    "LogLineRecord",
    "NewLogLine",
    "PersistenceObserver",
    "PipelineIds",
    "RunDetail",
    "RunPage",
    "RunRepository",
    "RunStatus",
    "RunSummary",
    "StepRecord",
]
