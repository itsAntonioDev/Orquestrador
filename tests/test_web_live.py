"""Testes das atualizações ao vivo (RunLiveTracker e WebSocket)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from orquestrador.api import create_app
from orquestrador.config import Settings
from orquestrador.db import NewLogLine, RunRepository
from orquestrador.dispatch import SyncDispatcher
from orquestrador.execution import Status
from orquestrador.projects import ProjectRegistry
from orquestrador.web.live import CLOSE_INTERNAL_ERROR, RunLiveTracker
from tests.test_api_runs import skip_outcome
from tests.test_db_repository import BASE_TIME, completed, make_request, make_result

Event = dict[str, Any]


def log(step_id: int, seq: int, text: str, stream: str = "stdout") -> NewLogLine:
    return NewLogLine(step_id, seq, stream, text, BASE_TIME)


class TestTracker:
    def test_first_poll_sends_snapshot_and_existing_logs(self, repository: RunRepository) -> None:
        repository.create_run(make_request())
        ids = repository.register_pipeline("run1", make_result())
        repository.append_logs([log(ids.steps[("build", 0)], seq, f"l{seq}") for seq in (1, 2)])
        tracker = RunLiveTracker(repository, "run1")

        events = tracker.poll()

        assert [event["type"] for event in events] == ["run", "logs"]
        assert events[0]["run"]["run_id"] == "run1"
        assert events[1]["job_id"] == "build"
        assert events[1]["step_index"] == 0
        assert [line["text"] for line in events[1]["lines"]] == ["l1", "l2"]
        assert tracker.finished is False

    def test_only_changes_are_sent(self, repository: RunRepository) -> None:
        repository.create_run(make_request())
        ids = repository.register_pipeline("run1", make_result())
        tracker = RunLiveTracker(repository, "run1")
        tracker.poll()

        assert tracker.poll() == []

        repository.append_logs([log(ids.steps[("build", 1)], 1, "novo", "stderr")])
        events = tracker.poll()
        assert [event["type"] for event in events] == ["run", "logs"]
        assert events[1]["step_index"] == 1
        assert events[1]["lines"][0]["stream"] == "stderr"
        assert tracker.poll() == []

    def test_logs_are_sent_in_batches(self, repository: RunRepository) -> None:
        repository.create_run(make_request())
        ids = repository.register_pipeline("run1", make_result())
        repository.append_logs([log(ids.steps[("build", 0)], seq, str(seq)) for seq in range(1, 6)])
        repository.finish_run(completed("run1", Status.SUCCESS))
        tracker = RunLiveTracker(repository, "run1", log_batch=2)

        batches: list[list[str]] = []
        while not tracker.finished:
            for event in tracker.poll():
                if event["type"] == "logs":
                    batches.append([line["text"] for line in event["lines"]])

        assert batches == [["1", "2"], ["3", "4"], ["5"]]

    def test_finishes_when_run_is_terminal(self, repository: RunRepository) -> None:
        repository.create_run(make_request())
        tracker = RunLiveTracker(repository, "run1")
        tracker.poll()
        assert not tracker.finished

        repository.finish_run(completed("run1", Status.FAILURE))

        events = tracker.poll()
        assert events[0]["run"]["status"] == "failure"
        assert tracker.finished and not tracker.missing

    def test_missing_run(self, repository: RunRepository) -> None:
        tracker = RunLiveTracker(repository, "fantasma")
        assert tracker.poll() == [{"type": "error", "message": "execução não encontrada"}]
        assert tracker.finished and tracker.missing


@pytest.fixture
def live_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, live_poll_interval=0.02)
    app = create_app(settings, ProjectRegistry(), SyncDispatcher(skip_outcome))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def live_repository(live_client: TestClient) -> RunRepository:
    repository: RunRepository = live_client.app.state.repository  # type: ignore[attr-defined]
    return repository


def receive_until(
    socket: WebSocketTestSession, predicate: Callable[[Event], bool], limit: int = 200
) -> list[Event]:
    received: list[Event] = []
    for _ in range(limit):
        event: Event = socket.receive_json()
        received.append(event)
        if predicate(event):
            return received
    raise AssertionError(f"evento esperado não chegou: {received}")


def test_websocket_finished_run(live_client: TestClient, live_repository: RunRepository) -> None:
    live_repository.create_run(make_request())
    ids = live_repository.register_pipeline("run1", make_result())
    live_repository.append_logs([log(ids.steps[("build", 0)], 1, "pronto")])
    live_repository.finish_run(completed("run1", Status.SUCCESS))

    with live_client.websocket_connect("/ws/runs/run1") as socket:
        events = receive_until(socket, lambda event: event["type"] == "end")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()

    assert [event["type"] for event in events] == ["run", "logs", "end"]
    assert events[0]["run"]["status"] == "success"
    assert events[1]["lines"][0]["text"] == "pronto"


def test_websocket_streams_live_updates(
    live_client: TestClient, live_repository: RunRepository
) -> None:
    request = make_request()
    live_repository.create_run(request)

    with live_client.websocket_connect("/ws/runs/run1") as socket:
        first = socket.receive_json()
        assert first["type"] == "run"
        assert first["run"]["status"] == "queued"

        live_repository.start_run(request)
        ids = live_repository.register_pipeline("run1", make_result())
        live_repository.append_logs([log(ids.steps[("build", 0)], 1, "compilando...")])
        updates = receive_until(socket, lambda event: event["type"] == "logs")
        runs = [event["run"] for event in updates if event["type"] == "run"]
        assert runs and runs[-1]["status"] == "running"
        assert [job["job_id"] for job in runs[-1]["jobs"]] == ["build", "deploy"]
        assert updates[-1]["lines"][0]["text"] == "compilando..."

        live_repository.append_logs([log(ids.steps[("build", 0)], 2, "concluído")])
        live_repository.finish_run(completed("run1", Status.SUCCESS))
        final = receive_until(socket, lambda event: event["type"] == "end")

    texts = [line["text"] for event in final if event["type"] == "logs" for line in event["lines"]]
    assert texts == ["concluído"]
    assert any(e["type"] == "run" and e["run"]["status"] == "success" for e in final)


def test_websocket_unknown_run(live_client: TestClient) -> None:
    with live_client.websocket_connect("/ws/runs/nao-existe") as socket:
        assert socket.receive_json() == {"type": "error", "message": "execução não encontrada"}
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_websocket_without_persistence(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, persistence_enabled=False)
    app = create_app(settings, ProjectRegistry(), SyncDispatcher(skip_outcome))
    with TestClient(app) as client, client.websocket_connect("/ws/runs/x") as socket:
        assert socket.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as info:
            socket.receive_json()
    assert info.value.code == CLOSE_INTERNAL_ERROR
