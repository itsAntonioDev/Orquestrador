"""Serviço que transforma um ``RunRequest`` numa execução de pipeline.

Fluxo: checkout do commit -> leitura do pipeline do repositório -> filtro
pelos gatilhos ``on`` -> ``PipelineRunner`` -> relatório JSON -> limpeza.

É esta função que os dispatchers executam — em thread (Fase 2) ou num
worker de fila (Fase 4) — por isso ela recebe e devolve objetos serializáveis.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from orquestrador.config import Settings
from orquestrador.dispatch.listeners import RunListener, collect_observers, notify_listeners
from orquestrador.dispatch.models import RunOutcome, RunRequest
from orquestrador.execution import LoggingObserver, PipelineRunner, RunContext, RunObserver
from orquestrador.executors import Executor, create_executor
from orquestrador.pipeline import PipelineError, PipelineValidationError, load_pipeline
from orquestrador.projects import Project, ProjectRegistry
from orquestrador.scm import CheckoutError, checkout
from orquestrador.utils import remove_tree
from orquestrador.webhooks.matching import event_matches_triggers

logger = logging.getLogger(__name__)

ExecutorFactory = Callable[[], Executor]
ObserverFactory = Callable[[RunRequest], Iterable[RunObserver]]
#: Recebe o nome do projeto e devolve os secrets disponíveis para ele.
SecretProvider = Callable[[str], Mapping[str, str]]


class RunService:
    """Executa pedidos de execução de ponta a ponta."""

    def __init__(
        self,
        settings: Settings,
        registry: ProjectRegistry,
        *,
        executor_factory: ExecutorFactory | None = None,
        observer_factory: ObserverFactory | None = None,
        listeners: Sequence[RunListener] = (),
        secret_provider: SecretProvider | None = None,
    ) -> None:
        """Cria o serviço.

        Args:
            settings: Configurações (diretórios, paralelismo, git, executor).
            registry: Projetos configurados.
            executor_factory: Cria o executor de cada execução
                (padrão: o configurado em ``settings.executor``).
            observer_factory: Cria observadores extras para cada execução.
            listeners: Recebem início e fim de cada execução (persistência,
                notificações) e podem fornecer observadores.
            secret_provider: Fonte dos secrets de cada projeto (ex.: ``SecretVault.resolve``).
        """
        self.listeners = list(listeners)
        self.secret_provider = secret_provider
        self.settings = settings
        self.registry = registry
        self.executor_factory: ExecutorFactory = executor_factory or (
            lambda: create_executor(self.settings)
        )
        self.observer_factory = observer_factory

    def execute(self, request: RunRequest) -> RunOutcome:
        """Processa o pedido. Nunca levanta exceção por falhas esperadas.

        Args:
            request: Pedido de execução.

        Returns:
            O desfecho, também gravado em ``<data_dir>/runs/<run_id>.json``.
        """
        logger.info(
            "[%s] processando %s de %s (commit %s)",
            request.run_id,
            request.trigger.event,
            request.trigger.repository,
            request.trigger.commit[:10],
        )
        notify_listeners(self.listeners, "run_started", request)
        project = self.registry.get(request.project)
        if project is None:
            return self._finish(
                RunOutcome(
                    run_id=request.run_id,
                    project=request.project,
                    status="error",
                    reason=f"projeto '{request.project}' não configurado",
                )
            )

        workspace = (self.settings.workspaces_dir / request.run_id).resolve()
        try:
            return self._finish(self._execute(project, request, workspace))
        finally:
            if not self.settings.keep_workspaces:
                remove_tree(workspace)

    def _execute(self, project: Project, request: RunRequest, workspace: Path) -> RunOutcome:
        trigger = request.trigger

        def error(reason: str) -> RunOutcome:
            return RunOutcome(
                run_id=request.run_id, project=project.name, status="error", reason=reason
            )

        clone_url = project.clone_url or trigger.clone_url
        if not clone_url:
            return error("URL de clone não definida no projeto nem no evento")
        try:
            checkout(
                clone_url,
                trigger.commit,
                workspace,
                git=self.settings.git_executable,
                timeout=self.settings.checkout_timeout,
            )
        except CheckoutError as exc:
            return error(f"falha no checkout: {exc}")

        pipeline_file = (workspace / project.pipeline).resolve()
        if not pipeline_file.is_relative_to(workspace):
            return error("o caminho do pipeline aponta para fora do repositório")
        try:
            pipeline = load_pipeline(pipeline_file)
        except PipelineValidationError as exc:
            return error(f"pipeline inválido: {'; '.join(exc.errors)}")
        except PipelineError as exc:
            return (
                error(f"pipeline não encontrado no repositório: {project.pipeline}")
                if (not pipeline_file.exists())
                else error(str(exc))
            )

        if not event_matches_triggers(pipeline.triggers, trigger):
            return RunOutcome(
                run_id=request.run_id,
                project=project.name,
                status="skipped",
                reason="o evento não corresponde aos gatilhos (on) do pipeline",
            )

        secrets: dict[str, str] = {}
        secrets_allowed = (
            trigger.event != "pull_request" or self.settings.secrets_for_pull_requests
        )
        if self.secret_provider is not None and secrets_allowed:
            try:
                secrets = dict(self.secret_provider(project.name))
            except Exception as exc:
                logger.exception("[%s] falha ao carregar secrets", request.run_id)
                return error(f"falha ao carregar os secrets: {exc.__class__.__name__}")

        context = RunContext(
            run_id=request.run_id,
            workspace=workspace,
            event=trigger.event,
            ref=trigger.ref,
            branch=trigger.branch,
            tag=trigger.tag,
            commit=trigger.commit,
            repository=trigger.repository,
            actor=trigger.actor,
            base_branch=trigger.base_branch,
            pull_request=trigger.pull_request,
            secrets=secrets,
        )
        extra = list(self.observer_factory(request)) if self.observer_factory else []
        extra.extend(collect_observers(self.listeners, request))
        runner = PipelineRunner(
            self.executor_factory(),
            [LoggingObserver(), *extra],
            max_parallel_jobs=self.settings.max_parallel_jobs,
        )
        result = runner.run(pipeline, context)
        return RunOutcome(
            run_id=request.run_id, project=project.name, status="completed", result=result
        )

    def _finish(self, outcome: RunOutcome) -> RunOutcome:
        """Grava o relatório JSON e registra o desfecho no log."""
        reports = self.settings.reports_dir
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{outcome.run_id}.json").write_text(
            outcome.model_dump_json(indent=2), encoding="utf-8"
        )
        final = outcome.result.status.value if outcome.result else outcome.status
        logger.info("[%s] desfecho: %s %s", outcome.run_id, final, outcome.reason or "")
        notify_listeners(self.listeners, "run_finished", outcome)
        return outcome
