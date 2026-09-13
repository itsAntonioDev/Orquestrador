"""Testes dos dispatchers e dos modelos de pedido."""

from __future__ import annotations

import threading
import time

import pytest
from pydantic import ValidationError

from orquestrador.dispatch import RunOutcome, RunRequest, SyncDispatcher, ThreadDispatcher
from orquestrador.projects import Provider
from orquestrador.webhooks import TriggerEvent


def make_request(**fields: str) -> RunRequest:
    trigger = TriggerEvent(
        provider=Provider.GITHUB, event="push", repository="acme/api", commit="a" * 40
    )
    return RunRequest(project="api", trigger=trigger, **fields)


def skipped(request: RunRequest) -> RunOutcome:
    return RunOutcome(run_id=request.run_id, project=request.project, status="skipped")


def test_sync_dispatcher_runs_immediately() -> None:
    dispatcher = SyncDispatcher(skipped)
    request = make_request()
    assert dispatcher.dispatch(request) == request.run_id
    assert [outcome.run_id for outcome in dispatcher.outcomes] == [request.run_id]


def test_thread_dispatcher_returns_before_execution_finishes() -> None:
    started = threading.Event()
    release = threading.Event()

    def handler(request: RunRequest) -> RunOutcome:
        started.set()
        release.wait(10)
        return skipped(request)

    dispatcher = ThreadDispatcher(handler, max_workers=1)
    request = make_request()
    begin = time.monotonic()
    assert dispatcher.dispatch(request) == request.run_id
    assert time.monotonic() - begin < 1
    assert started.wait(5)
    assert dispatcher.active == 1

    release.set()
    dispatcher.shutdown(wait=True)
    assert dispatcher.active == 0


def test_thread_dispatcher_limits_concurrency() -> None:
    lock = threading.Lock()
    running = 0
    peak = 0

    def handler(request: RunRequest) -> RunOutcome:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return skipped(request)

    dispatcher = ThreadDispatcher(handler, max_workers=2)
    for _ in range(6):
        dispatcher.dispatch(make_request())
    dispatcher.shutdown(wait=True)
    assert peak == 2


def test_thread_dispatcher_survives_handler_errors(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def handler(request: RunRequest) -> RunOutcome:
        calls.append(request.run_id)
        if len(calls) == 1:
            raise RuntimeError("falhou")
        return skipped(request)

    dispatcher = ThreadDispatcher(handler, max_workers=1)
    dispatcher.dispatch(make_request())
    dispatcher.dispatch(make_request())
    dispatcher.shutdown(wait=True)
    assert len(calls) == 2
    assert "erro inesperado" in caplog.text


@pytest.mark.parametrize("run_id", ["../../etc", "com espaço", "", "a" * 65])
def test_run_request_rejects_unsafe_run_ids(run_id: str) -> None:
    with pytest.raises(ValidationError):
        make_request(run_id=run_id)


def test_run_request_json_roundtrip() -> None:
    request = make_request()
    restored = RunRequest.model_validate_json(request.model_dump_json())
    assert restored == request
