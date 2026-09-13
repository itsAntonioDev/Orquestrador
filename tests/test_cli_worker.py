"""Testes do comando ``orquestrador worker``."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from typer.testing import CliRunner

from orquestrador.cli import app
from orquestrador.dispatch import rq_queue
from orquestrador.dispatch.rq_queue import RQDispatcher, configure_worker_service, create_queue
from tests.test_rq_queue import StubService, make_request

cli = CliRunner()


@pytest.fixture(autouse=True)
def reset_worker_service() -> Iterator[None]:
    yield
    configure_worker_service(None)


def test_worker_burst_processes_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = fakeredis.FakeRedis()
    urls: list[str] = []

    def fake_connection(url: str) -> fakeredis.FakeRedis:
        urls.append(url)
        return connection

    monkeypatch.setattr(rq_queue, "redis_connection", fake_connection)
    monkeypatch.setenv("ORQ_REDIS_URL", "redis://fila:6379/2")
    service = StubService()
    configure_worker_service(service)  # type: ignore[arg-type]
    RQDispatcher(create_queue(connection, "ci")).dispatch(make_request())

    result = cli.invoke(
        app, ["worker", "--burst", "--queue", "ci", "--worker-class", "simple", "--name", "w-teste"]
    )

    assert result.exit_code == 0, result.output
    assert urls == ["redis://fila:6379/2"]
    assert len(service.requests) == 1


def test_worker_fails_when_redis_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(
        rq_queue, "redis_connection", lambda url: fakeredis.FakeRedis(server=server)
    )
    monkeypatch.setenv("ORQ_REDIS_URL", "redis://:senha-secreta@redis:6379/0")

    result = cli.invoke(app, ["worker", "--burst"])

    assert result.exit_code == 2
    assert "Redis indisponível" in result.output
    assert "senha-secreta" not in result.output


def test_worker_rejects_invalid_class() -> None:
    result = cli.invoke(app, ["worker", "--worker-class", "threads"])
    assert result.exit_code == 2
