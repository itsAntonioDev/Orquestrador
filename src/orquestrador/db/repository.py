"""Acesso ao histórico de execuções.

Cada método abre sua própria transação curta, então o repositório pode ser
usado por várias threads (jobs paralelos) e processos (API e workers).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import selectinload

from orquestrador.db.models import JobRow, LogLineRow, RunRow, StepRow
from orquestrador.db.schemas import (
    JobRecord,
    LogLineRecord,
    RunDetail,
    RunPage,
    RunStatus,
    RunSummary,
    StepRecord,
    run_status_for,
)
from orquestrador.db.session import Database
from orquestrador.dispatch.models import RunOutcome, RunRequest
from orquestrador.execution.results import JobResult, PipelineResult, StepResult, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineIds:
    """IDs gerados para os jobs e steps de uma execução.

    Attributes:
        jobs: ``job_id`` -> ID da linha em ``job_runs``.
        steps: ``(job_id, índice do step)`` -> ID da linha em ``step_runs``.
    """

    jobs: dict[str, int]
    steps: dict[tuple[str, int], int]


@dataclass(frozen=True)
class NewLogLine:
    """Linha de log a inserir."""

    step_run_id: int
    seq: int
    stream: str
    text: str
    timestamp: datetime


def _new_run_row(request: RunRequest, status: RunStatus) -> RunRow:
    trigger = request.trigger
    return RunRow(
        id=request.run_id,
        project=request.project,
        provider=trigger.provider.value,
        repository=trigger.repository,
        event=trigger.event,
        ref=trigger.ref,
        branch=trigger.branch,
        tag=trigger.tag,
        commit=trigger.commit,
        actor=trigger.actor,
        base_branch=trigger.base_branch,
        pull_request=trigger.pull_request,
        message=trigger.message,
        status=status.value,
        created_at=request.requested_at,
    )


def _summary_fields(row: RunRow) -> dict[str, Any]:
    return {
        "run_id": row.id,
        "project": row.project,
        "provider": row.provider,
        "repository": row.repository,
        "event": row.event,
        "ref": row.ref,
        "branch": row.branch,
        "tag": row.tag,
        "commit": row.commit,
        "actor": row.actor,
        "base_branch": row.base_branch,
        "pull_request": row.pull_request,
        "message": row.message,
        "pipeline": row.pipeline,
        "status": row.status,
        "reason": row.reason,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "duration": row.duration,
    }


class RunRepository:
    """Gravação e consulta do histórico de execuções."""

    def __init__(self, database: Database) -> None:
        """Cria o repositório.

        Args:
            database: Conexão com o banco.
        """
        self.database = database

    # ------------------------------------------------------------------ escrita

    def create_run(self, request: RunRequest, status: RunStatus = RunStatus.QUEUED) -> bool:
        """Registra uma execução nova.

        Returns:
            ``False`` se a execução já existia (nada é alterado).
        """
        with self.database.transaction() as session:
            if session.get(RunRow, request.run_id) is not None:
                return False
            session.add(_new_run_row(request, status))
            return True

    def start_run(self, request: RunRequest) -> None:
        """Marca a execução como ``running`` (criando-a se não foi registrada na fila)."""
        with self.database.transaction() as session:
            row = session.get(RunRow, request.run_id)
            if row is None:
                row = _new_run_row(request, RunStatus.RUNNING)
                session.add(row)
            row.status = RunStatus.RUNNING.value
            row.started_at = utcnow()
            row.finished_at = None
            row.reason = None

    def register_pipeline(self, run_id: str, result: PipelineResult) -> PipelineIds:
        """Cria as linhas de jobs e steps (``pending``) a partir do resultado inicial.

        Se a execução já tinha jobs (reprocessamento), eles são substituídos.

        Raises:
            LookupError: Se a execução não existir.
        """
        with self.database.transaction() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise LookupError(f"execução não encontrada: {run_id}")
            run.pipeline = result.pipeline
            run.jobs.clear()
            session.flush()
            for position, job in enumerate(result.jobs.values()):
                job_row = JobRow(
                    job_id=job.job_id,
                    name=job.name,
                    position=position,
                    status=job.status.value,
                    allowed_failure=job.allowed_failure,
                )
                job_row.steps = [
                    StepRow(
                        index=step.index,
                        name=step.name,
                        status=step.status.value,
                        allowed_failure=step.allowed_failure,
                        truncated_lines=0,
                        group=step.group,
                    )
                    for step in job.steps
                ]
                run.jobs.append(job_row)
            session.flush()
            return PipelineIds(
                jobs={job.job_id: job.id for job in run.jobs},
                steps={(job.job_id, step.index): step.id for job in run.jobs for step in job.steps},
            )

    def update_job(self, job_run_id: int, result: JobResult) -> None:
        """Atualiza status, erro e tempos de um job."""
        with self.database.transaction() as session:
            row = session.get(JobRow, job_run_id)
            if row is None:
                return
            row.status = result.status.value
            row.error = result.error
            row.started_at = result.started_at
            row.finished_at = result.finished_at
            row.duration = result.duration

    def update_step(
        self, step_run_id: int, result: StepResult, *, truncated_lines: int | None = None
    ) -> None:
        """Atualiza status, código de saída, erro e tempos de um step."""
        with self.database.transaction() as session:
            row = session.get(StepRow, step_run_id)
            if row is None:
                return
            row.status = result.status.value
            row.exit_code = result.exit_code
            row.error = result.error
            row.started_at = result.started_at
            row.finished_at = result.finished_at
            row.duration = result.duration
            if truncated_lines is not None:
                row.truncated_lines = truncated_lines

    def append_logs(self, lines: Sequence[NewLogLine]) -> None:
        """Insere linhas de log em lote."""
        if not lines:
            return
        with self.database.transaction() as session:
            session.execute(
                insert(LogLineRow),
                [
                    {
                        "step_run_id": line.step_run_id,
                        "seq": line.seq,
                        "stream": line.stream,
                        "text": line.text,
                        "timestamp": line.timestamp,
                    }
                    for line in lines
                ],
            )

    def finish_run(self, outcome: RunOutcome) -> None:
        """Grava o desfecho final da execução."""
        with self.database.transaction() as session:
            row = session.get(RunRow, outcome.run_id)
            if row is None:
                logger.warning("desfecho de execução desconhecida ignorado: %s", outcome.run_id)
                return
            now = utcnow()
            row.status = run_status_for(outcome).value
            row.reason = outcome.reason
            row.finished_at = now
            if outcome.result is not None:
                row.pipeline = outcome.result.pipeline
                row.duration = outcome.result.duration
                if row.started_at is None:
                    row.started_at = outcome.result.started_at
            elif row.started_at is not None:
                row.duration = max((now - row.started_at).total_seconds(), 0.0)

    def fail_run(self, run_id: str, reason: str) -> None:
        """Marca a execução como ``error`` (ex.: não foi possível enfileirar)."""
        with self.database.transaction() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return
            row.status = RunStatus.ERROR.value
            row.reason = reason
            row.finished_at = utcnow()

    def delete_run(self, run_id: str) -> bool:
        """Apaga a execução com jobs, steps e logs.

        Returns:
            ``True`` se algo foi apagado.
        """
        with self.database.transaction() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return False
            session.delete(row)
            return True

    # ------------------------------------------------------------------ leitura

    def list_runs(
        self,
        *,
        project: str | None = None,
        status: RunStatus | str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RunPage:
        """Lista execuções, das mais recentes para as mais antigas."""
        filters = []
        if project is not None:
            filters.append(RunRow.project == project)
        if status is not None:
            filters.append(RunRow.status == RunStatus(status).value)
        with self.database.session() as session:
            total = session.scalar(select(func.count()).select_from(RunRow).where(*filters)) or 0
            rows = session.scalars(
                select(RunRow)
                .where(*filters)
                .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            items = [RunSummary.model_validate(_summary_fields(row)) for row in rows]
        return RunPage(items=items, total=total, limit=limit, offset=offset)

    def get_run(self, run_id: str) -> RunDetail | None:
        """Execução completa com jobs, steps e contagem de linhas de log."""
        with self.database.session() as session:
            row = session.scalars(
                select(RunRow)
                .where(RunRow.id == run_id)
                .options(selectinload(RunRow.jobs).selectinload(JobRow.steps))
            ).first()
            if row is None:
                return None
            step_ids = [step.id for job in row.jobs for step in job.steps]
            counts: dict[int, int] = {}
            if step_ids:
                counts = {
                    step_id: count
                    for step_id, count in session.execute(
                        select(LogLineRow.step_run_id, func.count())
                        .where(LogLineRow.step_run_id.in_(step_ids))
                        .group_by(LogLineRow.step_run_id)
                    ).all()
                }
            jobs = [
                JobRecord(
                    job_id=job.job_id,
                    name=job.name,
                    status=RunStatus(job.status),
                    error=job.error,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                    duration=job.duration,
                    allowed_failure=job.allowed_failure,
                    steps=[
                        StepRecord(
                            index=step.index,
                            name=step.name,
                            status=RunStatus(step.status),
                            exit_code=step.exit_code,
                            error=step.error,
                            started_at=step.started_at,
                            finished_at=step.finished_at,
                            duration=step.duration,
                            allowed_failure=step.allowed_failure,
                            log_lines=counts.get(step.id, 0),
                            truncated_lines=step.truncated_lines,
                            group=step.group,
                        )
                        for step in job.steps
                    ],
                )
                for job in row.jobs
            ]
            return RunDetail.model_validate({**_summary_fields(row), "jobs": jobs})

    def get_logs(
        self,
        run_id: str,
        job_id: str,
        step_index: int,
        *,
        after: int = 0,
        limit: int = 1000,
    ) -> list[LogLineRecord] | None:
        """Linhas de log de um step com ``seq > after`` (paginação incremental).

        Returns:
            As linhas, ou ``None`` se o step não existir.
        """
        with self.database.session() as session:
            step_id = session.scalar(
                select(StepRow.id)
                .join(JobRow, StepRow.job_run_id == JobRow.id)
                .where(
                    JobRow.run_id == run_id,
                    JobRow.job_id == job_id,
                    StepRow.index == step_index,
                )
            )
            if step_id is None:
                return None
            rows = session.scalars(
                select(LogLineRow)
                .where(LogLineRow.step_run_id == step_id, LogLineRow.seq > after)
                .order_by(LogLineRow.seq)
                .limit(limit)
            ).all()
            return [
                LogLineRecord.model_validate(
                    {"seq": r.seq, "stream": r.stream, "text": r.text, "timestamp": r.timestamp}
                )
                for r in rows
            ]
