"""Observador que grava o progresso da execução no banco em tempo (quase) real."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from orquestrador.db.repository import NewLogLine, PipelineIds
from orquestrador.execution.events import RunObserver

if TYPE_CHECKING:
    from orquestrador.db.repository import RunRepository
    from orquestrador.execution.context import RunContext
    from orquestrador.execution.results import JobResult, LogLine, PipelineResult, StepResult
    from orquestrador.pipeline.models import Job, Pipeline, Step


class PersistenceObserver(RunObserver):
    """Persiste status de jobs/steps e linhas de log de uma execução.

    Logs são acumulados e gravados em lotes (a cada ``batch_size`` linhas,
    ``flush_interval`` segundos ou no fim do step), evitando uma transação por
    linha. Cada step guarda no máximo ``max_log_lines_per_step`` linhas; o
    excedente é contado em ``truncated_lines``.
    """

    def __init__(
        self,
        repository: RunRepository,
        run_id: str,
        *,
        max_log_lines_per_step: int = 20_000,
        max_line_length: int = 10_000,
        batch_size: int = 100,
        flush_interval: float = 0.5,
    ) -> None:
        """Cria o observador.

        Args:
            repository: Repositório de execuções.
            run_id: Execução observada (deve existir no banco).
            max_log_lines_per_step: Limite de linhas armazenadas por step.
            max_line_length: Linhas maiores são cortadas.
            batch_size: Linhas acumuladas antes de gravar.
            flush_interval: Intervalo máximo entre gravações de log (segundos).
        """
        self.repository = repository
        self.run_id = run_id
        self.max_log_lines_per_step = max_log_lines_per_step
        self.max_line_length = max_line_length
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._ids: PipelineIds | None = None
        self._lock = threading.Lock()
        self._buffer: list[NewLogLine] = []
        self._line_counts: dict[int, int] = defaultdict(int)
        self._last_flush = time.monotonic()

    def _step_id(self, job: Job, step: Step, result: StepResult) -> int | None:
        if self._ids is None:
            return None
        return self._ids.steps.get((job.id, result.index))

    def flush(self) -> None:
        """Grava as linhas de log acumuladas."""
        with self._lock:
            pending, self._buffer = self._buffer, []
            self._last_flush = time.monotonic()
        if pending:
            self.repository.append_logs(pending)

    def on_pipeline_start(
        self, pipeline: Pipeline, context: RunContext, result: PipelineResult
    ) -> None:
        self._ids = self.repository.register_pipeline(self.run_id, result)

    def on_job_start(self, job: Job, result: JobResult) -> None:
        if self._ids is not None and job.id in self._ids.jobs:
            self.repository.update_job(self._ids.jobs[job.id], result)

    def on_step_start(self, job: Job, step: Step, result: StepResult) -> None:
        step_id = self._step_id(job, step, result)
        if step_id is not None:
            self.repository.update_step(step_id, result)

    def on_step_output(self, job: Job, step: Step, result: StepResult, line: LogLine) -> None:
        step_id = self._step_id(job, step, result)
        if step_id is None:
            return
        with self._lock:
            self._line_counts[step_id] += 1
            seq = self._line_counts[step_id]
            if seq <= self.max_log_lines_per_step:
                self._buffer.append(
                    NewLogLine(
                        step_run_id=step_id,
                        seq=seq,
                        stream=line.stream,
                        text=line.text[: self.max_line_length],
                        timestamp=line.timestamp,
                    )
                )
            should_flush = (
                len(self._buffer) >= self.batch_size
                or time.monotonic() - self._last_flush >= self.flush_interval
            )
        if should_flush:
            self.flush()

    def on_step_end(self, job: Job, step: Step, result: StepResult) -> None:
        step_id = self._step_id(job, step, result)
        if step_id is None:
            return
        self.flush()
        with self._lock:
            truncated = max(self._line_counts[step_id] - self.max_log_lines_per_step, 0)
        self.repository.update_step(step_id, result, truncated_lines=truncated)

    def on_job_end(self, job: Job, result: JobResult) -> None:
        if self._ids is None or job.id not in self._ids.jobs:
            return
        self.flush()
        job_id = self._ids.jobs[job.id]
        self.repository.update_job(job_id, result)
        for step_result in result.steps:
            step_id = self._ids.steps.get((job.id, step_result.index))
            if (
                step_id is not None
                and step_result.status.is_terminal
                and not step_result.started_at
            ):
                # Steps that never started (skipped/cancelled job) do not receive on_step_end.
                self.repository.update_step(step_id, step_result)

    def on_pipeline_end(self, pipeline: Pipeline, result: PipelineResult) -> None:
        self.flush()
