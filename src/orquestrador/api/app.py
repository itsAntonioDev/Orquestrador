"""Fábrica da aplicação FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from orquestrador import __version__
from orquestrador.api.routes import health, runs, webhooks
from orquestrador.bootstrap import (
    build_dispatcher,
    build_listeners,
    build_registry,
    build_repository,
)
from orquestrador.config import Settings, get_settings
from orquestrador.db import RunRepository
from orquestrador.dispatch import Dispatcher
from orquestrador.projects import ProjectRegistry
from orquestrador.web import STATIC_DIR, live, pages


def create_app(
    settings: Settings | None = None,
    registry: ProjectRegistry | None = None,
    dispatcher: Dispatcher | None = None,
    repository: RunRepository | None = None,
) -> FastAPI:
    """Monta a aplicação com injeção explícita de dependências.

    Args:
        settings: Configurações (padrão: lidas do ambiente).
        registry: Projetos (padrão: carregados de ``settings.projects_file``).
        dispatcher: Destino das execuções (padrão: conforme ``ORQ_DISPATCHER``,
            decorado para registrar execuções no histórico).
        repository: Histórico de execuções (padrão: conforme ``ORQ_DATABASE_URL``;
            ``None`` com persistência desativada).

    Returns:
        A aplicação FastAPI pronta para servir.

    Raises:
        ProjectRegistryError: Se o arquivo de projetos for inválido.
    """
    settings = settings or get_settings()
    registry = registry if registry is not None else build_registry(settings)
    if repository is None:
        repository = build_repository(settings)
    active_dispatcher = dispatcher or build_dispatcher(
        settings, registry, build_listeners(settings, repository, registry), repository
    )
    active_repository = repository

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        active_dispatcher.shutdown(wait=False)
        if active_repository is not None:
            active_repository.database.dispose()

    app = FastAPI(
        title="Orquestrador",
        description="Orquestrador de CI/CD self-hosted com pipelines em YAML.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.dispatcher = active_dispatcher
    app.state.repository = active_repository
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(runs.router)
    app.include_router(live.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
