"""Verificação de saúde da API."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from orquestrador import __version__
from orquestrador.api.deps import get_dispatcher, get_registry
from orquestrador.dispatch import Dispatcher
from orquestrador.projects import ProjectRegistry

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Resposta do health check.

    Attributes:
        status: ``ok`` quando a API está no ar.
        version: Versão do orquestrador.
        projects: Quantidade de projetos configurados.
        dispatcher: Métricas do dispatcher (tipo, fila, workers...).
    """

    status: str
    version: str
    projects: int
    dispatcher: dict[str, Any]


@router.get("/health", response_model=HealthResponse)
def health(
    registry: Annotated[ProjectRegistry, Depends(get_registry)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> HealthResponse:
    """Indica que a API está no ar, quantos projetos existem e o estado da fila."""
    return HealthResponse(
        status="ok",
        version=__version__,
        projects=len(registry),
        dispatcher=dispatcher.stats(),
    )
