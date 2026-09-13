"""Observadores do ciclo de vida de uma execução.

O runner não sabe nada sobre terminal, banco de dados ou WebSocket: ele apenas
emite eventos. Cada integração (saída no console, persistência, streaming de
logs para o dashboard, notificações) é um ``RunObserver`` independente.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orquestrador.execution.context import RunContext
    from orquestrador.execution.results import JobResult, LogLine, PipelineResult, StepResult
    from orquestrador.pipeline.models import Job, Pipeline, Step

logger = logging.getLogger(__name__)


class RunObserver:
    """Recebe eventos de uma execução. Todos os métodos são opcionais (no-op).

    Com jobs em paralelo, os métodos podem ser chamados de threads diferentes;
    implementações com estado compartilhado devem ser thread-safe.
    Jobs e steps ignorados/cancelados recebem apenas o evento ``*_end``.
    """

    def on_pipeline_start(
        self, pipeline: Pipeline, context: RunContext, result: PipelineResult
    ) -> None:
        """Chamado quando a execução do pipeline começa."""

    def on_pipeline_end(self, pipeline: Pipeline, result: PipelineResult) -> None:
        """Chamado quando a execução do pipeline termina."""

    def on_job_start(self, job: Job, result: JobResult) -> None:
        """Chamado quando um job começa a executar."""

    def on_job_end(self, job: Job, result: JobResult) -> None:
        """Chamado quando um job termina (ou é ignorado/cancelado)."""

    def on_step_start(self, job: Job, step: Step, result: StepResult) -> None:
        """Chamado imediatamente antes de um step executar."""

    def on_step_output(self, job: Job, step: Step, result: StepResult, line: LogLine) -> None:
        """Chamado para cada linha de saída de um step."""

    def on_step_end(self, job: Job, step: Step, result: StepResult) -> None:
        """Chamado quando um step termina (ou é ignorado)."""


class CompositeObserver(RunObserver):
    """Repassa eventos a vários observadores, isolando falhas de cada um.

    Uma exceção num observador (ex.: banco fora do ar) é registrada em log
    mas não interrompe a execução do pipeline nem os demais observadores.
    """

    def __init__(self, observers: Iterable[RunObserver] = ()) -> None:
        """Cria o observador composto.

        Args:
            observers: Observadores iniciais.
        """
        self._observers: list[RunObserver] = list(observers)

    def add(self, observer: RunObserver) -> None:
        """Adiciona um observador."""
        self._observers.append(observer)

    def _dispatch(self, method: str, *args: object) -> None:
        for observer in self._observers:
            try:
                getattr(observer, method)(*args)
            except Exception:
                logger.exception("observador %r falhou em %s", observer, method)

    def on_pipeline_start(
        self, pipeline: Pipeline, context: RunContext, result: PipelineResult
    ) -> None:
        self._dispatch("on_pipeline_start", pipeline, context, result)

    def on_pipeline_end(self, pipeline: Pipeline, result: PipelineResult) -> None:
        self._dispatch("on_pipeline_end", pipeline, result)

    def on_job_start(self, job: Job, result: JobResult) -> None:
        self._dispatch("on_job_start", job, result)

    def on_job_end(self, job: Job, result: JobResult) -> None:
        self._dispatch("on_job_end", job, result)

    def on_step_start(self, job: Job, step: Step, result: StepResult) -> None:
        self._dispatch("on_step_start", job, step, result)

    def on_step_output(self, job: Job, step: Step, result: StepResult, line: LogLine) -> None:
        self._dispatch("on_step_output", job, step, result, line)

    def on_step_end(self, job: Job, step: Step, result: StepResult) -> None:
        self._dispatch("on_step_end", job, step, result)


class LoggingObserver(RunObserver):
    """Registra o progresso da execução no ``logging`` (usado pelo servidor).

    Uma instância deve ser usada por execução, pois guarda o ``run_id``.
    """

    def __init__(self, log: logging.Logger | None = None) -> None:
        """Cria o observador.

        Args:
            log: Logger de destino (padrão: ``orquestrador.run``).
        """
        self.log = log or logging.getLogger("orquestrador.run")
        self.run_id = "-"

    def on_pipeline_start(
        self, pipeline: Pipeline, context: RunContext, result: PipelineResult
    ) -> None:
        self.run_id = context.run_id
        self.log.info("[%s] pipeline '%s' iniciado", self.run_id, pipeline.name)

    def on_pipeline_end(self, pipeline: Pipeline, result: PipelineResult) -> None:
        self.log.info(
            "[%s] pipeline '%s' terminou: %s (%.2fs)",
            self.run_id,
            pipeline.name,
            result.status.value,
            result.duration,
        )

    def on_job_end(self, job: Job, result: JobResult) -> None:
        self.log.info(
            "[%s] job '%s': %s%s",
            self.run_id,
            job.id,
            result.status.value,
            f" - {result.error}" if result.error else "",
        )

    def on_step_output(self, job: Job, step: Step, result: StepResult, line: LogLine) -> None:
        self.log.debug("[%s] %s/%s | %s", self.run_id, job.id, step.name, line.text)

    def on_step_end(self, job: Job, step: Step, result: StepResult) -> None:
        level = logging.WARNING if result.error else logging.INFO
        self.log.log(
            level,
            "[%s] step '%s/%s': %s%s",
            self.run_id,
            job.id,
            step.name,
            result.status.value,
            f" - {result.error}" if result.error else "",
        )
