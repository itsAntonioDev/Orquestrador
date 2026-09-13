"""Liga o ciclo de vida das execuções ao banco de dados."""

from __future__ import annotations

from orquestrador.db.observer import PersistenceObserver
from orquestrador.db.repository import RunRepository
from orquestrador.db.schemas import RunStatus
from orquestrador.dispatch.listeners import RunListener
from orquestrador.dispatch.models import RunOutcome, RunRequest
from orquestrador.execution.events import RunObserver


class DatabaseRunListener(RunListener):
    """Registra cada fase de uma execução: fila, início, progresso e desfecho."""

    def __init__(self, repository: RunRepository, *, max_log_lines_per_step: int = 20_000) -> None:
        """Cria o listener.

        Args:
            repository: Repositório de execuções.
            max_log_lines_per_step: Limite de linhas de log armazenadas por step.
        """
        self.repository = repository
        self.max_log_lines_per_step = max_log_lines_per_step

    def run_queued(self, request: RunRequest) -> None:
        self.repository.create_run(request, RunStatus.QUEUED)

    def run_dispatch_failed(self, request: RunRequest, error: Exception) -> None:
        self.repository.fail_run(request.run_id, f"falha ao agendar a execução: {error}")

    def run_started(self, request: RunRequest) -> None:
        self.repository.start_run(request)

    def run_observers(self, request: RunRequest) -> list[RunObserver]:
        return [
            PersistenceObserver(
                self.repository,
                request.run_id,
                max_log_lines_per_step=self.max_log_lines_per_step,
            )
        ]

    def run_finished(self, outcome: RunOutcome) -> None:
        self.repository.finish_run(outcome)
