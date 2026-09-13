"""Páginas HTML do dashboard (renderizadas no servidor, atualizadas com HTMX)."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from orquestrador.db import RunRepository, RunStatus
from orquestrador.projects import ProjectRegistry
from orquestrador.web.formatting import FILTERABLE_STATUSES
from orquestrador.web.templating import templates

router = APIRouter(include_in_schema=False)

#: Execuções por página na listagem.
PAGE_SIZE = 25


def _repository(request: Request) -> RunRepository | None:
    repository: RunRepository | None = getattr(request.app.state, "repository", None)
    return repository


def _registry(request: Request) -> ProjectRegistry:
    registry: ProjectRegistry = request.app.state.registry
    return registry


def render_message(request: Request, title: str, message: str, status_code: int) -> Response:
    """Página simples de aviso/erro."""
    return templates.TemplateResponse(
        request,
        "message.html",
        {"title": title, "message": message, "active": ""},
        status_code=status_code,
    )


def _unavailable(request: Request) -> Response:
    return render_message(
        request,
        "Histórico indisponível",
        "A persistência está desativada (ORQ_PERSISTENCE_ENABLED=false).",
        503,
    )


def _runs_context(
    request: Request, repository: RunRepository, project: str, status: str, page: int
) -> dict[str, Any]:
    """Monta o contexto da listagem de execuções (página completa e parcial)."""
    valid_statuses = {item.value for item in RunStatus}
    status_filter = RunStatus(status) if status in valid_statuses else None
    offset = (page - 1) * PAGE_SIZE
    result = repository.list_runs(
        project=project or None, status=status_filter, limit=PAGE_SIZE, offset=offset
    )
    filters = {"project": project, "status": status_filter.value if status_filter else ""}

    def query(page_number: int) -> str:
        params = {key: value for key, value in filters.items() if value}
        if page_number > 1:
            params["page"] = str(page_number)
        return urlencode(params)

    return {
        "active": "runs",
        "page": result,
        "page_number": page,
        "has_previous": page > 1,
        "has_next": offset + len(result.items) < result.total,
        "query": query(page),
        "previous_query": query(page - 1),
        "next_query": query(page + 1),
        "filters": filters,
        "projects": sorted(project.name for project in _registry(request)),
        "statuses": FILTERABLE_STATUSES,
    }


@router.get("/")
def index() -> RedirectResponse:
    """A página inicial é a lista de execuções."""
    return RedirectResponse("/runs", status_code=307)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    project: str = "",
    status: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> Response:
    """Lista de execuções com filtros; a tabela se atualiza sozinha."""
    repository = _repository(request)
    if repository is None:
        return _unavailable(request)
    context = _runs_context(request, repository, project, status, page)
    return templates.TemplateResponse(request, "runs.html", context)


@router.get("/runs/table", response_class=HTMLResponse)
def runs_table(
    request: Request,
    project: str = "",
    status: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> Response:
    """Fragmento HTML da tabela (usado pelo HTMX para filtrar e atualizar)."""
    repository = _repository(request)
    if repository is None:
        return _unavailable(request)
    context = _runs_context(request, repository, project, status, page)
    return templates.TemplateResponse(request, "_runs_table.html", context)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str) -> Response:
    """Detalhe da execução com jobs, steps e logs ao vivo."""
    repository = _repository(request)
    if repository is None:
        return _unavailable(request)
    run = repository.get_run(run_id)
    if run is None:
        return render_message(
            request, "Execução não encontrada", f"Não existe execução com ID {run_id}.", 404
        )
    return templates.TemplateResponse(request, "run_detail.html", {"active": "runs", "run": run})


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request) -> Response:
    """Projetos configurados e a última execução de cada um (sem expor segredos)."""
    repository = _repository(request)
    rows = []
    for project in sorted(_registry(request), key=lambda item: item.name):
        last_run = None
        if repository is not None:
            items = repository.list_runs(project=project.name, limit=1).items
            last_run = items[0] if items else None
        rows.append({"project": project, "last_run": last_run})
    return templates.TemplateResponse(
        request, "projects.html", {"active": "projects", "rows": rows}
    )
