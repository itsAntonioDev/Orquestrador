"""Saída de execução no terminal (usada pela CLI)."""

from __future__ import annotations

import threading

from rich.console import Console
from rich.table import Table
from rich.text import Text

from orquestrador.execution.context import RunContext
from orquestrador.execution.events import RunObserver
from orquestrador.execution.results import JobResult, LogLine, PipelineResult, Status, StepResult
from orquestrador.pipeline.models import Job, Pipeline, Step

#: Label and style for each status. ASCII labels work in any terminal.
STATUS_STYLES: dict[Status, tuple[str, str]] = {
    Status.PENDING: ("PENDENTE", "dim"),
    Status.QUEUED: ("NA FILA", "dim"),
    Status.RUNNING: ("EXECUTANDO", "cyan"),
    Status.SUCCESS: ("SUCESSO", "bold green"),
    Status.FAILURE: ("FALHOU", "bold red"),
    Status.SKIPPED: ("IGNORADO", "yellow"),
    Status.CANCELLED: ("CANCELADO", "magenta"),
}


def status_text(status: Status) -> Text:
    """Renderiza um status como texto colorido."""
    label, style = STATUS_STYLES[status]
    return Text(label, style=style)


class ConsoleObserver(RunObserver):
    """Imprime o progresso da execução, com a saída dos steps em tempo real."""

    def __init__(self, console: Console | None = None, *, show_output: bool = True) -> None:
        """Cria o observador.

        Args:
            console: Console do Rich (padrão: stdout).
            show_output: Se imprime a saída dos comandos.
        """
        self.console = console or Console()
        self.show_output = show_output
        self._lock = threading.Lock()

    def _print(self, *parts: str | tuple[str, str] | Text) -> None:
        with self._lock:
            self.console.print(Text.assemble(*parts), soft_wrap=True, highlight=False)

    def on_pipeline_start(
        self, pipeline: Pipeline, context: RunContext, result: PipelineResult
    ) -> None:
        with self._lock:
            self.console.rule(Text(f"Pipeline: {pipeline.name}", style="bold"))
        details = [f"run {context.run_id}", f"evento {context.event}"]
        if context.branch:
            details.append(f"branch {context.branch}")
        if context.commit:
            details.append(f"commit {context.commit[:10]}")
        self._print((" | ".join(details), "dim"))

    def on_job_start(self, job: Job, result: JobResult) -> None:
        self._print((f"[{job.id}] ", "bold blue"), ">> iniciando job ", (job.name, "bold"))

    def on_step_start(self, job: Job, step: Step, result: StepResult) -> None:
        group = (f" [paralelo: {step.group}]", "dim") if step.group else ""
        self._print((f"[{job.id}] ", "bold blue"), "-- step: ", (step.name, "bold"), group)

    def on_step_output(self, job: Job, step: Step, result: StepResult, line: LogLine) -> None:
        if not self.show_output:
            return
        style = "red" if line.stream == "stderr" else ""
        self._print((f"[{job.id}] ", "blue"), ("   | ", "dim"), (line.text, style))

    def on_step_end(self, job: Job, step: Step, result: StepResult) -> None:
        parts: list[str | tuple[str, str] | Text] = [
            (f"[{job.id}] ", "bold blue"),
            "   ",
            status_text(result.status),
            f" {step.name}",
        ]
        if result.status not in {Status.SKIPPED, Status.PENDING}:
            parts.append((f" ({result.duration:.2f}s)", "dim"))
        if result.error:
            parts.append((f" - {result.error}", "red"))
        self._print(*parts)

    def on_job_end(self, job: Job, result: JobResult) -> None:
        parts: list[str | tuple[str, str] | Text] = [
            (f"[{job.id}] ", "bold blue"),
            "<< job ",
            (job.name, "bold"),
            ": ",
            status_text(result.status),
        ]
        if result.error:
            parts.append((f" - {result.error}", "red"))
        self._print(*parts)

    def on_pipeline_end(self, pipeline: Pipeline, result: PipelineResult) -> None:
        table = Table(show_edge=False, header_style="bold", pad_edge=False)
        table.add_column("Job")
        table.add_column("Status")
        table.add_column("Duração", justify="right")
        table.add_column("Detalhes")
        for job in result.jobs.values():
            table.add_row(
                job.name, status_text(job.status), f"{job.duration:.2f}s", job.error or ""
            )
        with self._lock:
            self.console.rule(Text("Resumo", style="bold"))
            self.console.print(table)
        self._print(
            "Pipeline ",
            (pipeline.name, "bold"),
            ": ",
            status_text(result.status),
            (f" em {result.duration:.2f}s", "dim"),
        )
