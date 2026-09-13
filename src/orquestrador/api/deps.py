"""Dependências FastAPI: acesso aos componentes guardados em ``app.state``."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from orquestrador.config import Settings
from orquestrador.db import RunRepository
from orquestrador.dispatch import Dispatcher
from orquestrador.projects import ProjectRegistry


def get_app_settings(request: Request) -> Settings:
    """Configurações da aplicação."""
    settings: Settings = request.app.state.settings
    return settings


def get_registry(request: Request) -> ProjectRegistry:
    """Registro de projetos."""
    registry: ProjectRegistry = request.app.state.registry
    return registry


def get_dispatcher(request: Request) -> Dispatcher:
    """Dispatcher de execuções."""
    dispatcher: Dispatcher = request.app.state.dispatcher
    return dispatcher


def get_repository(request: Request) -> RunRepository:
    """Histórico de execuções.

    Raises:
        HTTPException: 503 se a persistência estiver desativada.
    """
    repository: RunRepository | None = getattr(request.app.state, "repository", None)
    if repository is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "persistência desativada")
    return repository
