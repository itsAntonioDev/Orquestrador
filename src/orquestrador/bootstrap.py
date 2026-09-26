"""Composition root: monta os componentes da aplicação a partir das configurações.

API, worker e CLI usam estas funções, então integrações novas (persistência,
secrets, notificações...) são conectadas num único lugar.
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
from orquestrador.dispatch.service import SecretProvider
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


def build_secret_provider(
    settings: Settings, repository: RunRepository | None
) -> SecretProvider | None:
    """Fonte de secrets baseada no cofre, se ``ORQ_SECRET_KEYS`` e o banco estiverem configurados.

    Raises:
        SecretError: Se as chaves forem inválidas.
    """
    if settings.secret_keys is None or repository is None:
        return None
    from orquestrador.vault import SecretCipher, SecretVault

    cipher = SecretCipher.from_config(settings.secret_keys.get_secret_value())
    return SecretVault(repository.database, cipher).resolve


def build_listeners(
    settings: Settings,
    repository: RunRepository | None,
    registry: ProjectRegistry | None = None,
) -> list[RunListener]:
    """Listeners do ciclo de vida das execuções: persistência e notificações."""
    listeners: list[RunListener] = []
    if repository is not None:
        listeners.append(
            DatabaseRunListener(repository, max_log_lines_per_step=settings.max_log_lines_per_step)
        )
    if registry is not None and any(project.notifications for project in registry):
        from orquestrador.notifications.listener import NotificationListener

        listeners.append(
            NotificationListener(
                registry,
                smtp=settings.smtp_settings,
                public_url=settings.public_url,
                timeout=settings.notification_timeout,
            )
        )
    return listeners


def build_run_service(
    settings: Settings,
    registry: ProjectRegistry | None = None,
    listeners: Sequence[RunListener] | None = None,
    repository: RunRepository | None = None,
) -> RunService:
    """Cria o serviço que executa pedidos (usado pelo worker e pelo dispatcher em threads)."""
    registry = registry if registry is not None else build_registry(settings)
    if repository is None:
        repository = build_repository(settings)
    if listeners is None:
        listeners = build_listeners(settings, repository, registry)
    return RunService(
        settings,
        registry,
        listeners=listeners,
        secret_provider=build_secret_provider(settings, repository),
    )


def build_dispatcher(
    settings: Settings,
    registry: ProjectRegistry,
    listeners: Sequence[RunListener] = (),
    repository: RunRepository | None = None,
) -> Dispatcher:
    """Cria o dispatcher configurado em ``ORQ_DISPATCHER`` (``thread`` ou ``rq``).

    Com listeners, o dispatcher é decorado para registrar execuções na fila.
    """
    inner: Dispatcher
    if settings.dispatcher is DispatcherKind.RQ:
        from orquestrador.dispatch.rq_queue import RQDispatcher

        inner = RQDispatcher.from_settings(settings)
    else:
        service = RunService(
            settings,
            registry,
            listeners=listeners,
            secret_provider=build_secret_provider(settings, repository),
        )
        inner = ThreadDispatcher(service.execute, max_workers=settings.max_concurrent_runs)
    return ListeningDispatcher(inner, listeners) if listeners else inner
