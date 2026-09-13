"""API REST de leitura do histórico de execuções."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Path as PathParam

from orquestrador.api.deps import get_repository
from orquestrador.db import LogLineRecord, RunDetail, RunPage, RunRepository, RunStatus

router = APIRouter(prefix="/api/runs", tags=["runs"])

Repository = Annotated[RunRepository, Depends(get_repository)]


@router.get("", response_model=RunPage)
def list_runs(
    repository: Repository,
    project: Annotated[str | None, Query(description="Filtra por projeto.")] = None,
    run_status: Annotated[
        RunStatus | None, Query(alias="status", description="Filtra por status.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    """Lista execuções, das mais recentes para as mais antigas."""
    return repository.list_runs(project=project, status=run_status, limit=limit, offset=offset)


@router.get(
    "/{run_id}", response_model=RunDetail, responses={404: {"description": "Não encontrada"}}
)
def get_run(run_id: str, repository: Repository) -> RunDetail:
    """Detalhes de uma execução, com jobs e steps."""
    run = repository.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "execução não encontrada")
    return run


@router.get(
    "/{run_id}/jobs/{job_id}/steps/{step_index}/logs",
    response_model=list[LogLineRecord],
    responses={404: {"description": "Step não encontrado"}},
)
def get_step_logs(
    run_id: str,
    job_id: str,
    step_index: Annotated[int, PathParam(ge=0)],
    repository: Repository,
    after: Annotated[int, Query(ge=0, description="Retorna linhas com seq maior que este.")] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> list[LogLineRecord]:
    """Linhas de log de um step, de forma incremental (use ``after`` para continuar)."""
    logs = repository.get_logs(run_id, job_id, step_index, after=after, limit=limit)
    if logs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "step não encontrado")
    return logs
