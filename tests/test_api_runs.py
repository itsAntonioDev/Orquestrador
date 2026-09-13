"""Testes da API REST de histórico (/api/runs)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orquestrador.api import create_app
from orquestrador.config import Settings
from orquestrador.db import NewLogLine, RunRepository, RunStatus
from orquestrador.dispatch import RunOutcome, SyncDispatcher
from orquestrador.execution import Status
from orquestrador.projects import Project, ProjectRegistry, Provider
from tests.test_db_repository import BASE_TIME, completed, make_request, make_result


def skip_outcome(request: object) -> RunOutcome:
    raise AssertionError("não deveria despachar")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path), ProjectRegistry(), SyncDispatcher(skip_outcome))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def repository(client: TestClient) -> RunRepository:
    repository: RunRepository = client.app.state.repository  # type: ignore[attr-defined]
    return repository


def seed(repository: RunRepository) -> None:
    repository.create_run(make_request("run-a", "api", minutes=0))
    repository.finish_run(completed("run-a", Status.SUCCESS))
    repository.create_run(make_request("run-b", "web", minutes=1))
    ids = repository.register_pipeline("run-b", make_result("run-b"))
    repository.append_logs(
        [
            NewLogLine(ids.steps[("build", 0)], seq, "stdout", f"linha {seq}", BASE_TIME)
            for seq in range(1, 4)
        ]
    )


def test_list_runs(client: TestClient, repository: RunRepository) -> None:
    seed(repository)

    data = client.get("/api/runs").json()

    assert data["total"] == 2
    assert [item["run_id"] for item in data["items"]] == ["run-b", "run-a"]
    assert data["items"][1]["status"] == "success"
    assert data["items"][0]["commit"] == "a" * 40


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?project=api", ["run-a"]),
        ("?status=queued", ["run-b"]),
        ("?status=success&project=web", []),
        ("?limit=1&offset=1", ["run-a"]),
    ],
)
def test_list_runs_filters(
    client: TestClient, repository: RunRepository, query: str, expected: list[str]
) -> None:
    seed(repository)
    items = client.get(f"/api/runs{query}").json()["items"]
    assert [item["run_id"] for item in items] == expected


@pytest.mark.parametrize("query", ["?status=explodiu", "?limit=0", "?limit=500", "?offset=-1"])
def test_list_runs_invalid_query(client: TestClient, query: str) -> None:
    assert client.get(f"/api/runs{query}").status_code == 422


def test_get_run_detail(client: TestClient, repository: RunRepository) -> None:
    seed(repository)

    data = client.get("/api/runs/run-b").json()

    assert data["run_id"] == "run-b"
    assert data["pipeline"] == "teste"
    assert [job["job_id"] for job in data["jobs"]] == ["build", "deploy"]
    first_step = data["jobs"][0]["steps"][0]
    assert (first_step["name"], first_step["status"], first_step["log_lines"]) == (
        "compilar",
        "pending",
        3,
    )


def test_get_unknown_run(client: TestClient) -> None:
    response = client.get("/api/runs/nao-existe")
    assert response.status_code == 404
    assert response.json()["detail"] == "execução não encontrada"


def test_step_logs(client: TestClient, repository: RunRepository) -> None:
    seed(repository)

    lines = client.get("/api/runs/run-b/jobs/build/steps/0/logs").json()
    assert [line["text"] for line in lines] == ["linha 1", "linha 2", "linha 3"]
    more = client.get("/api/runs/run-b/jobs/build/steps/0/logs?after=2").json()
    assert [line["seq"] for line in more] == [3]


def test_step_logs_not_found(client: TestClient, repository: RunRepository) -> None:
    seed(repository)
    assert client.get("/api/runs/run-b/jobs/build/steps/7/logs").status_code == 404
    assert client.get("/api/runs/run-b/jobs/build/steps/-1/logs").status_code == 422


def test_persistence_disabled_returns_503(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, persistence_enabled=False)
    app = create_app(settings, ProjectRegistry(), SyncDispatcher(skip_outcome))
    with TestClient(app) as client:
        assert client.get("/api/runs").status_code == 503
    assert not (tmp_path / "orquestrador.db").exists()


def test_webhook_run_is_recorded_from_queue_to_finish(tmp_path: Path) -> None:
    """Webhook -> 'queued' no banco -> thread executa -> 'error' (clone inválido) com motivo."""
    from tests.test_api import GITHUB_SECRET, post_github
    from tests.test_webhooks_github import push_payload

    registry = ProjectRegistry(
        [
            Project(
                name="api",
                provider=Provider.GITHUB,
                repository="acme/api",
                secret=GITHUB_SECRET,
                clone_url=str(tmp_path / "repositorio-inexistente"),
            )
        ]
    )
    app = create_app(Settings(data_dir=tmp_path / "data"), registry)
    with TestClient(app) as client:
        response = post_github(client, push_payload())
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        deadline = time.monotonic() + 20
        run = client.get(f"/api/runs/{run_id}").json()
        while run["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            run = client.get(f"/api/runs/{run_id}").json()

    assert run["status"] == RunStatus.ERROR
    assert run["reason"].startswith("falha no checkout")
    assert run["branch"] == "main"
    assert run["started_at"] is not None and run["finished_at"] is not None
