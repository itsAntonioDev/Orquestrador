"""Testes da API de webhooks (FastAPI)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from orquestrador import __version__
from orquestrador.api import create_app
from orquestrador.config import Settings
from orquestrador.dispatch import Dispatcher, RunRequest, ThreadDispatcher
from orquestrador.projects import Project, ProjectRegistry, Provider
from orquestrador.webhooks import github, gitlab
from tests.test_webhooks_github import pr_payload
from tests.test_webhooks_github import push_payload as github_push
from tests.test_webhooks_gitlab import push_payload as gitlab_push

GITHUB_SECRET = "segredo-github"
GITLAB_SECRET = "token-gitlab"
BODY_LIMIT = 10_000


class RecordingDispatcher(Dispatcher):
    """Dispatcher falso que só registra os pedidos."""

    def __init__(self) -> None:
        self.requests: list[RunRequest] = []
        self.fail = False

    def dispatch(self, request: RunRequest) -> str:
        if self.fail:
            raise ConnectionError("fila indisponível")
        self.requests.append(request)
        return request.run_id


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def client(tmp_path: Path, dispatcher: RecordingDispatcher) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, max_webhook_body_bytes=BODY_LIMIT)
    registry = ProjectRegistry(
        [
            Project(
                name="api", provider=Provider.GITHUB, repository="acme/api", secret=GITHUB_SECRET
            ),
            Project(
                name="app",
                provider=Provider.GITLAB,
                repository="grupo/sub/app",
                secret=GITLAB_SECRET,
            ),
        ]
    )
    with TestClient(create_app(settings, registry, dispatcher)) as test_client:
        yield test_client


def post_github(
    client: TestClient,
    payload: dict[str, Any] | bytes,
    *,
    event: str | None = "push",
    secret: str = GITHUB_SECRET,
    signature: str | None = None,
) -> httpx.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Delivery": "entrega-123",
        github.SIGNATURE_HEADER: signature or github.compute_signature(secret, body),
    }
    if event is not None:
        headers[github.EVENT_HEADER] = event
    return client.post("/webhooks/github", content=body, headers=headers)


def post_gitlab(
    client: TestClient,
    payload: dict[str, Any],
    *,
    event: str = "Push Hook",
    token: str = GITLAB_SECRET,
) -> httpx.Response:
    headers = {gitlab.EVENT_HEADER: event, gitlab.TOKEN_HEADER: token}
    return client.post("/webhooks/gitlab", json=payload, headers=headers)


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "projects": 2,
        "dispatcher": {"kind": "RecordingDispatcher"},
    }


def test_github_push_is_queued(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_github(client, github_push())

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    assert data["project"] == "api"
    assert len(dispatcher.requests) == 1
    request = dispatcher.requests[0]
    assert data["run_id"] == request.run_id
    assert request.project == "api"
    assert request.trigger.branch == "main"
    assert request.trigger.delivery_id == "entrega-123"


def test_github_pull_request_is_queued(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_github(client, pr_payload(), event="pull_request")
    assert response.status_code == 202
    assert dispatcher.requests[0].trigger.pull_request == 42


def test_github_invalid_signature(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_github(client, github_push(), secret="segredo-errado")
    assert response.status_code == 401
    assert response.json()["detail"] == "assinatura do webhook inválida"
    assert dispatcher.requests == []


def test_github_missing_signature(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    body = json.dumps(github_push()).encode()
    response = client.post("/webhooks/github", content=body, headers={github.EVENT_HEADER: "push"})
    assert response.status_code == 401
    assert dispatcher.requests == []


def test_signature_checked_before_ping(client: TestClient) -> None:
    response = post_github(client, github_push(), event="ping", secret="errado")
    assert response.status_code == 401


def test_github_ping(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_github(client, {"zen": "Keep it simple", **github_push()}, event="ping")
    assert response.status_code == 200
    assert response.json() == {"status": "pong", "project": "api", "run_id": None, "reason": None}
    assert dispatcher.requests == []


def test_unknown_repository(client: TestClient) -> None:
    payload = github_push(repository={"full_name": "outro/repo"})
    response = post_github(client, payload)
    assert response.status_code == 404
    assert "github:outro/repo" in response.json()["detail"]


def test_ignored_event(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_github(client, github_push(), event="issues")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "issues" in response.json()["reason"]
    assert dispatcher.requests == []


def test_deleted_branch_is_ignored(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_github(client, github_push(deleted=True))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert dispatcher.requests == []


def test_invalid_json(client: TestClient) -> None:
    response = post_github(client, b"{isso nao e json")
    assert response.status_code == 400
    assert response.json()["detail"] == "corpo não é um JSON válido"


def test_json_must_be_object(client: TestClient) -> None:
    response = post_github(client, b"[1, 2, 3]")
    assert response.status_code == 400


def test_payload_without_repository(client: TestClient) -> None:
    response = post_github(client, {"ref": "refs/heads/main"})
    assert response.status_code == 400
    assert "repositório" in response.json()["detail"]


def test_missing_event_header(client: TestClient) -> None:
    response = post_github(client, github_push(), event=None)
    assert response.status_code == 400


def test_invalid_payload_fields(client: TestClient) -> None:
    payload = github_push()
    del payload["after"]
    response = post_github(client, payload)
    assert response.status_code == 400
    assert "after" in response.json()["detail"]


def test_body_too_large(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    payload = github_push(padding="x" * (BODY_LIMIT + 1))
    response = post_github(client, payload)
    assert response.status_code == 413
    assert dispatcher.requests == []


def test_unknown_provider(client: TestClient) -> None:
    response = client.post("/webhooks/bitbucket", json={})
    assert response.status_code == 422


def test_dispatch_failure_returns_503(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    dispatcher.fail = True
    response = post_github(client, github_push())
    assert response.status_code == 503


def test_gitlab_push_is_queued(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_gitlab(client, gitlab_push())
    assert response.status_code == 202
    assert response.json()["project"] == "app"
    trigger = dispatcher.requests[0].trigger
    assert trigger.provider == Provider.GITLAB
    assert trigger.branch == "develop"


def test_gitlab_invalid_token(client: TestClient, dispatcher: RecordingDispatcher) -> None:
    response = post_gitlab(client, gitlab_push(), token="errado")
    assert response.status_code == 401
    assert dispatcher.requests == []


def test_gitlab_ignored_event(client: TestClient) -> None:
    response = post_gitlab(client, gitlab_push(), event="Issue Hook")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_create_app_loads_registry_from_settings(
    tmp_path: Path, write_file: Callable[..., Path]
) -> None:
    projects = write_file(
        "projects:\n  - {name: a, provider: github, repository: a/b, secret: s}",
        name="projects.yml",
    )
    app = create_app(Settings(projects_file=projects, data_dir=tmp_path))
    assert isinstance(app.state.dispatcher.inner, ThreadDispatcher)
    with TestClient(app) as test_client:
        assert test_client.get("/health").json()["projects"] == 1


def test_openapi_documents_webhook_route(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/webhooks/{provider}" in schema["paths"]
