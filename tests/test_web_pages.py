"""Testes das páginas HTML do dashboard."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orquestrador.api import create_app
from orquestrador.config import Settings
from orquestrador.db import RunRepository
from orquestrador.dispatch import SyncDispatcher
from orquestrador.execution import Status
from orquestrador.projects import Project, ProjectRegistry, Provider
from orquestrador.web import pages
from orquestrador.web.formatting import (
    format_duration,
    format_timestamp,
    iso_timestamp,
    short_sha,
    status_class,
    status_label,
)
from tests.test_api_runs import skip_outcome
from tests.test_db_repository import BASE_TIME, completed, make_request, make_result

REGISTRY = ProjectRegistry(
    [
        Project(name="api", provider=Provider.GITHUB, repository="acme/api", secret="segredo-api"),
        Project(name="web", provider=Provider.GITLAB, repository="grupo/web", secret="segredo-web"),
    ]
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path), REGISTRY, SyncDispatcher(skip_outcome))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def repository(client: TestClient) -> RunRepository:
    repository: RunRepository = client.app.state.repository  # type: ignore[attr-defined]
    return repository


def seed(repository: RunRepository) -> None:
    repository.create_run(make_request("run-ok", "api", minutes=0))
    repository.finish_run(completed("run-ok", Status.SUCCESS, duration=75))
    repository.create_run(make_request("run-fila", "web", minutes=1))
    repository.create_run(make_request("run-detalhe", "api", minutes=2))
    repository.register_pipeline("run-detalhe", make_result("run-detalhe"))


def test_root_redirects_to_runs(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/runs"


def test_runs_page_lists_runs(client: TestClient, repository: RunRepository) -> None:
    seed(repository)

    response = client.get("/runs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for run_id in ("run-ok", "run-fila", "run-detalhe"):
        assert f'href="/runs/{run_id}"' in html
    assert "Sucesso" in html and "Na fila" in html
    assert "1m 15s" in html
    assert 'hx-trigger="every 3s"' in html
    assert '<option value="api"' in html and '<option value="web"' in html
    assert "3 de 3 execuções" in html


def test_runs_page_filters(client: TestClient, repository: RunRepository) -> None:
    seed(repository)
    html = client.get("/runs?project=web&status=queued").text
    assert "run-fila" in html
    assert "run-ok" not in html
    assert '<option value="web" selected>' in html
    assert "/runs/table?project=web&amp;status=queued" in html


def test_runs_page_ignores_invalid_status(client: TestClient, repository: RunRepository) -> None:
    seed(repository)
    response = client.get("/runs?status=inventado")
    assert response.status_code == 200
    assert "3 de 3 execuções" in response.text


def test_runs_table_partial(client: TestClient, repository: RunRepository) -> None:
    seed(repository)
    html = client.get("/runs/table?project=api").text
    assert html.lstrip().startswith("<div") or "<table" in html
    assert "<html" not in html
    assert 'id="runs-table"' in html
    assert "run-ok" in html and "run-fila" not in html


def test_runs_pagination(
    client: TestClient, repository: RunRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pages, "PAGE_SIZE", 2)
    seed(repository)
    first = client.get("/runs").text
    assert "Próximas" in first and "Anteriores" not in first
    second = client.get("/runs?page=2").text
    assert "run-ok" in second
    assert "Anteriores" in second and "Próximas" not in second


def test_empty_runs_page(client: TestClient) -> None:
    assert "Nenhuma execução encontrada" in client.get("/runs").text


def test_user_content_is_escaped(client: TestClient, repository: RunRepository) -> None:
    request = make_request("run-xss", "api")
    request.trigger.message = "<script>alert('xss')</script>"
    repository.create_run(request)

    for path in ("/runs", "/runs/run-xss"):
        html = client.get(path).text
        assert "<script>alert(" not in html
        assert "&lt;script&gt;alert(" in html


def test_run_detail_page(client: TestClient, repository: RunRepository) -> None:
    seed(repository)

    response = client.get("/runs/run-detalhe")

    assert response.status_code == 200
    html = response.text
    assert 'data-run-id="run-detalhe"' in html
    assert 'data-ws-path="/ws/runs/run-detalhe"' in html
    assert 'data-job="build"' in html and 'data-job="deploy"' in html
    assert "compilar" in html and "testar" in html
    assert 'id="status-labels"' in html
    assert "/static/run.js" in html


def test_run_detail_not_found(client: TestClient) -> None:
    response = client.get("/runs/nao-existe")
    assert response.status_code == 404
    assert "Execução não encontrada" in response.text


def test_projects_page(client: TestClient, repository: RunRepository) -> None:
    seed(repository)
    response = client.get("/projects")
    assert response.status_code == 200
    html = response.text
    assert "acme/api" in html and "grupo/web" in html
    assert "segredo-api" not in html and "segredo-web" not in html
    assert 'href="/runs/run-detalhe"' in html
    assert 'href="/runs?project=api"' in html


def test_projects_page_without_runs(client: TestClient) -> None:
    assert client.get("/projects").text.count("nunca executado") == 2


def test_pages_with_persistence_disabled(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, persistence_enabled=False)
    app = create_app(settings, REGISTRY, SyncDispatcher(skip_outcome))
    with TestClient(app) as client:
        for path in ("/runs", "/runs/table", "/runs/x"):
            response = client.get(path)
            assert response.status_code == 503
            assert "Histórico indisponível" in response.text
        assert client.get("/projects").status_code == 200


@pytest.mark.parametrize(
    ("path", "content_type", "fragment"),
    [
        ("/static/app.css", "text/css", "--success"),
        ("/static/app.js", "javascript", "localizeTimes"),
        ("/static/run.js", "javascript", "textContent"),
        ("/static/htmx.min.js", "javascript", "htmx"),
    ],
)
def test_static_files(client: TestClient, path: str, content_type: str, fragment: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert fragment in response.text


def test_dashboard_routes_hidden_from_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/runs" not in paths
    assert "/api/runs" in paths


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "-"), (0.42, "0.4s"), (9.96, "10.0s"), (12.4, "12s"), (75, "1m 15s"), (3725, "1h 02m")],
)
def test_format_duration(seconds: float | None, expected: str) -> None:
    assert format_duration(seconds) == expected


def test_other_formatters() -> None:
    assert status_label("success") == "Sucesso"
    assert status_label("desconhecido") == "desconhecido"
    assert status_class("failure") == "status-failure"
    assert format_timestamp(BASE_TIME) == "2026-01-01 12:00:00 UTC"
    assert format_timestamp(None) == "-"
    assert format_timestamp(BASE_TIME.replace(tzinfo=None)) == "2026-01-01 12:00:00 UTC"
    assert iso_timestamp(BASE_TIME) == "2026-01-01T12:00:00+00:00"
    assert iso_timestamp(None) == ""
    assert short_sha("abcdef1234567890") == "abcdef12"
    assert short_sha(None) == "-"
