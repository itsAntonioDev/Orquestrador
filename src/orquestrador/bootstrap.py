"""Composition root: monta os componentes da aplicação a partir das configurações.

API, worker e CLI usam estas funções, então integrações novas (persistência,
notificações...) são conectadas num único lugar.
"""

from __future__ import annotations

from collections.abc import Sequence

from orquestrador.config import DispatcherKind, Settings
from orquestrador.db import Database, DatabaseRunListener, RunRepository
from orquestrador.db.migrations import upgrade_database
from orquestrador.dispatch import (
    Dispatcher,
    ListeningDispatcher,
    RunListener,
    RunService,
    ThreadDispatcher,
)
from orquestrador.projects import ProjectRegistry


def build_registry(settings: Settings) -> ProjectRegistry:
    """Carrega os projetos de ``settings.projects_file``.

    Raises:
        ProjectRegistryError: Se o arquivo não existir ou for inválido.
    """
    return ProjectRegistry.from_file(settings.projects_file)


def build_database(settings: Settings) -> Database:
    """Conecta ao banco e, se configurado, aplica as migrações pendentes."""
    url = settings.effective_database_url
    if settings.database_auto_migrate:
        upgrade_database(url)
    return Database(url, echo=settings.database_echo)


def build_repository(settings: Settings) -> RunRepository | None:
    """Repositório do histórico, ou ``None`` com ``ORQ_PERSISTENCE_ENABLED=false``."""
    if not settings.persistence_enabled:
        return None
    return RunRepository(build_database(settings))


def build_listeners(settings: Settings, repository: RunRepository | None) -> list[RunListener]:
    """Listeners do ciclo de vida das execuções (persistência, ...)."""
    listeners: list[RunListener] = []
    if repository is not None:
        listeners.append(
            DatabaseRunListener(repository, max_log_lines_per_step=settings.max_log_lines_per_step)
        )
    return listeners


def build_run_service(
    settings: Settings,
    registry: ProjectRegistry | None = None,
    listeners: Sequence[RunListener] | None = None,
) -> RunService:
    """Cria o serviço que executa pedidos (usado pelo worker e pelo dispatcher em threads)."""
    if listeners is None:
        listeners = build_listeners(settings, build_repository(settings))
    return RunService(
        settings,
        registry if registry is not None else build_registry(settings),
        listeners=listeners,
    )


def build_dispatcher(
    settings: Settings,
    registry: ProjectRegistry,
    listeners: Sequence[RunListener] = (),
) -> Dispatcher:
    """Cria o dispatcher configurado em ``ORQ_DISPATCHER`` (``thread`` ou ``rq``).

    Com listeners, o dispatcher é decorado para registrar execuções na fila.
    """
    inner: Dispatcher
    if settings.dispatcher is DispatcherKind.RQ:
        from orquestrador.dispatch.rq_queue import RQDispatcher

        inner = RQDispatcher.from_settings(settings)
    else:
        service = RunService(settings, registry, listeners=listeners)
        inner = ThreadDispatcher(service.execute, max_workers=settings.max_concurrent_runs)
    return ListeningDispatcher(inner, listeners) if listeners else inner
