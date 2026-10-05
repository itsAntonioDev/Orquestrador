"""Tabelas do histórico de execuções.

Hierarquia: ``runs`` 1─N ``job_runs`` 1─N ``step_runs`` 1─N ``log_lines``.
As chaves estrangeiras usam ``ON DELETE CASCADE``: apagar uma execução
remove jobs, steps e logs no próprio banco.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orquestrador.db.base import Base, UTCDateTime

#: Big integer primary key, with auto-increment support on SQLite as well.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class RunRow(Base):
    """Uma execução de pipeline (um evento processado)."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(20))
    repository: Mapped[str] = mapped_column(String(255))
    event: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str | None] = mapped_column(String(255))
    branch: Mapped[str | None] = mapped_column(String(255))
    tag: Mapped[str | None] = mapped_column(String(255))
    commit: Mapped[str] = mapped_column("commit_sha", String(64))
    actor: Mapped[str | None] = mapped_column(String(255))
    base_branch: Mapped[str | None] = mapped_column(String(255))
    pull_request: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    pipeline: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration: Mapped[float | None] = mapped_column(Float)

    jobs: Mapped[list[JobRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="JobRow.position",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_runs_created_at", "created_at"),
        Index("ix_runs_project_created_at", "project", "created_at"),
        Index("ix_runs_status", "status"),
    )


class JobRow(Base):
    """Um job dentro de uma execução."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    job_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration: Mapped[float | None] = mapped_column(Float)
    allowed_failure: Mapped[bool] = mapped_column(Boolean, default=False)

    run: Mapped[RunRow] = relationship(back_populates="jobs")
    steps: Mapped[list[StepRow]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="StepRow.index",
        passive_deletes=True,
    )

    __table_args__ = (UniqueConstraint("run_id", "job_id"),)


class StepRow(Base):
    """Um step dentro de um job."""

    __tablename__ = "step_runs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("job_runs.id", ondelete="CASCADE"),
    )
    index: Mapped[int] = mapped_column("step_index", Integer)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration: Mapped[float | None] = mapped_column(Float)
    allowed_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    truncated_lines: Mapped[int] = mapped_column(Integer, default=0)
    group: Mapped[str | None] = mapped_column("group_name", String(100))

    job: Mapped[JobRow] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("job_run_id", "step_index"),)


class LogLineRow(Base):
    """Uma linha de saída de um step."""

    __tablename__ = "log_lines"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    step_run_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("step_runs.id", ondelete="CASCADE"),
    )
    seq: Mapped[int] = mapped_column(Integer)
    stream: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())

    __table_args__ = (Index("ix_log_lines_step_run_id_seq", "step_run_id", "seq"),)


class SecretRow(Base):
    """Um secret criptografado (Fernet). ``project`` vazio = secret global."""

    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(100))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())

    __table_args__ = (UniqueConstraint("project", "name"),)
