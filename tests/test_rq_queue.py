"""Testes da fila de execuções (Redis + RQ) usando fakeredis."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.exceptions import RedisError
from rq import SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

from orquestrador.api import create_app
from orquestrador.bootstrap import build_dispatcher, build_run_service
from orquestrador.config import DispatcherKind, Settings, get_settings
from orquestrador.dispatch import RunOutcome, RunRequest, RunService, ThreadDispatcher, rq_queue
from orquestrador.dispatch.rq_queue import (
    TASK_PATH,
    RQDispatcher,
    configure_worker_service,
    create_queue,
    create_worker,
    execute_run_request,
    get_worker_service,
)
from orquestrador.projects import Project, ProjectRegistry, Provider
from orquestrador.webhooks import TriggerEvent
from tests.conftest import GitRepoFactory, requires_git

QUEUE = "testes"


def make_request(project: str = "api") -> RunRequest:
    return RunRequest(
        project=project,
        trigger=TriggerEvent(
            provider=Provider.GITHUB,
            event="push",
            repository="acme/api",
            branch="main",
            commit="a" * 40,
        ),
    )


class StubService:
    """Substitui o RunService registrando os pedidos recebidos."""

    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[RunRequest] = []
        self.error = error

    def execute(self, request: RunRequest) -> RunOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return RunOutcome(
            run_id=request.run_id, project=request.project, status="skipped", reason="stub"
        )


@pytest.fixture
def connection() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def offline_connection() -> fakeredis.FakeRedis:
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeRedis(server=server)


@pytest.fixture(autouse=True)
def reset_worker_service() -> Iterator[None]:
    configure_worker_service(None)
    yield
    configure_worker_service(None)


def use_stub(service: StubService) -> None:
    configure_worker_service(service)  # type: ignore[arg-type]


def run_worker_burst(connection: fakeredis.FakeRedis) -> None:
    create_worker(connection, [QUEUE], worker_class="simple").work(burst=True)


class TestDispatcher:
    def test_enqueues_json_job(self, connection: fakeredis.FakeRedis) -> None:
        queue = create_queue(connection, QUEUE)
        dispatcher = RQDispatcher(queue, job_timeout=120, result_ttl=60, failure_ttl=90)
        request = make_request()

        assert dispatcher.dispatch(request) == request.run_id

        assert queue.count == 1
        job = queue.fetch_job(request.run_id)
        assert job is not None
        assert job.func_name == TASK_PATH
        assert list(job.args) == [request.model_dump(mode="json")]
        assert job.timeout == 120
        assert job.result_ttl == 60
        assert job.failure_ttl == 90
        assert job.meta["project"] == "api"
        assert job.meta["commit"] == "a" * 40
        assert job.description == "api: push aaaaaaaaaa"

    def test_redis_unavailable_raises(self, offline_connection: fakeredis.FakeRedis) -> None:
        dispatcher = RQDispatcher(create_queue(offline_connection, QUEUE))
        with pytest.raises(RedisError):
            dispatcher.dispatch(make_request())

    def test_stats(self, connection: fakeredis.FakeRedis) -> None:
        dispatcher = RQDispatcher(create_queue(connection, QUEUE))
        dispatcher.dispatch(make_request())
        dispatcher.dispatch(make_request())
        assert dispatcher.stats() == {
            "kind": "rq",
            "queue": QUEUE,
            "queued": 2,
            "started": 0,
            "failed": 0,
            "workers": 0,
        }

    def test_stats_with_redis_down(self, offline_connection: fakeredis.FakeRedis) -> None:
        stats = RQDispatcher(create_queue(offline_connection, QUEUE)).stats()
        assert stats["kind"] == "rq"
        assert "error" in stats

    def test_from_settings(
        self, monkeypatch: pytest.MonkeyPatch, connection: fakeredis.FakeRedis
    ) -> None:
        urls: list[str] = []

        def fake_connection(url: str) -> fakeredis.FakeRedis:
            urls.append(url)
            return connection

        monkeypatch.setattr(rq_queue, "redis_connection", fake_connection)
        settings = Settings(
            redis_url="redis://redis:6379/1",
            queue_name="ci",
            run_timeout=600,
            result_ttl=60,
            failure_ttl=120,
        )
        dispatcher = RQDispatcher.from_settings(settings)
        assert urls == ["redis://redis:6379/1"]
        assert dispatcher.queue.name == "ci"
        assert (dispatcher.job_timeout, dispatcher.result_ttl, dispatcher.failure_ttl) == (
            600,
            60,
            120,
        )


class TestWorker:
    def test_worker_executes_enqueued_run(self, connection: fakeredis.FakeRedis) -> None:
        service = StubService()
        use_stub(service)
        queue = create_queue(connection, QUEUE)
        request = make_request()
        RQDispatcher(queue).dispatch(request)

        run_worker_burst(connection)

        assert service.requests == [request]
        job = queue.fetch_job(request.run_id)
        assert job is not None
        assert job.get_status() == "finished"
        outcome = RunOutcome.model_validate(job.return_value())
        assert outcome.status == "skipped"
        assert outcome.run_id == request.run_id
        assert queue.count == 0

    def test_worker_processes_all_runs_in_fifo_order(self, connection: fakeredis.FakeRedis) -> None:
        service = StubService()
        use_stub(service)
        dispatcher = RQDispatcher(create_queue(connection, QUEUE))
        requests = [make_request(project=f"p{i}") for i in range(3)]
        for request in requests:
            dispatcher.dispatch(request)

        run_worker_burst(connection)

        assert [r.project for r in service.requests] == ["p0", "p1", "p2"]

    def test_task_errors_go_to_failed_registry(self, connection: fakeredis.FakeRedis) -> None:
        use_stub(StubService(error=RuntimeError("explodiu")))
        queue = create_queue(connection, QUEUE)
        request = make_request()
        RQDispatcher(queue).dispatch(request)

        run_worker_burst(connection)

        job = queue.fetch_job(request.run_id)
        assert job is not None
        assert job.get_status() == "failed"
        assert queue.failed_job_registry.count == 1
        assert "explodiu" in (job.latest_result().exc_string or "")  # type: ignore[union-attr]

    def test_synchronous_queue(self, connection: fakeredis.FakeRedis) -> None:
        service = StubService()
        use_stub(service)
        queue = create_queue(connection, QUEUE, is_async=False)
        RQDispatcher(queue).dispatch(make_request())
        assert len(service.requests) == 1

    def test_create_worker_classes(self, connection: fakeredis.FakeRedis) -> None:
        assert type(create_worker(connection, [QUEUE], worker_class="simple")) is SimpleWorker
        assert type(create_worker(connection, [QUEUE], worker_class="fork")) is Worker
        auto = create_worker(connection, ["alta", "baixa"], name="w1")
        assert type(auto) is (Worker if hasattr(os, "fork") else SimpleWorker)
        assert [queue.name for queue in auto.queues] == ["alta", "baixa"]
        assert auto.name == "w1"
        if not hasattr(signal, "SIGALRM"):
            assert auto.death_penalty_class is TimerDeathPenalty


class TestTask:
    def test_execute_run_request_roundtrip(self) -> None:
        service = StubService()
        use_stub(service)
        request = make_request()
        result = execute_run_request(request.model_dump(mode="json"))
        assert result["status"] == "skipped"
        assert result["run_id"] == request.run_id
        assert service.requests == [request]

    def test_invalid_payload(self) -> None:
        use_stub(StubService())
        with pytest.raises(ValidationError):
            execute_run_request({"project": "x"})

    def test_worker_service_built_from_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        write_file: Callable[..., Path],
    ) -> None:
        projects = write_file("projects: []\n", name="projects.yml")
        monkeypatch.setenv("ORQ_PROJECTS_FILE", str(projects))
        monkeypatch.setenv("ORQ_DATA_DIR", str(tmp_path))
        get_settings.cache_clear()
        try:
            service = get_worker_service()
            assert isinstance(service, RunService)
            assert service is get_worker_service()
            assert service.settings.projects_file == projects
        finally:
            get_settings.cache_clear()


class TestBootstrap:
    def test_thread_dispatcher_by_default(self, tmp_path: Path) -> None:
        dispatcher = build_dispatcher(Settings(data_dir=tmp_path), ProjectRegistry())
        assert isinstance(dispatcher, ThreadDispatcher)
        assert dispatcher.stats() == {"kind": "thread", "active": 0}
        dispatcher.shutdown()

    def test_rq_dispatcher_from_settings(
        self, monkeypatch: pytest.MonkeyPatch, connection: fakeredis.FakeRedis
    ) -> None:
        monkeypatch.setattr(rq_queue, "redis_connection", lambda url: connection)
        settings = Settings(dispatcher=DispatcherKind.RQ)
        assert isinstance(build_dispatcher(settings, ProjectRegistry()), RQDispatcher)

    def test_build_run_service_uses_given_registry(self, tmp_path: Path) -> None:
        registry = ProjectRegistry()
        service = build_run_service(Settings(data_dir=tmp_path), registry)
        assert service.registry is registry

    def test_health_reports_queue(
        self, monkeypatch: pytest.MonkeyPatch, connection: fakeredis.FakeRedis, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(rq_queue, "redis_connection", lambda url: connection)
        settings = Settings(data_dir=tmp_path, dispatcher=DispatcherKind.RQ, queue_name=QUEUE)
        with TestClient(create_app(settings, ProjectRegistry())) as client:
            dispatcher = client.get("/health").json()["dispatcher"]
        assert dispatcher["kind"] == "rq"
        assert dispatcher["queue"] == QUEUE
        assert dispatcher["queued"] == 0


@requires_git
def test_webhook_to_worker_end_to_end(
    connection: fakeredis.FakeRedis, git_repo: GitRepoFactory, tmp_path: Path
) -> None:
    """Webhook -> API (202) -> Redis -> worker -> checkout -> pipeline executado."""
    from tests.test_api import GITHUB_SECRET, post_github
    from tests.test_run_service import PIPELINE
    from tests.test_webhooks_github import push_payload

    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "fila"})
    settings = Settings(data_dir=tmp_path / "data")
    registry = ProjectRegistry(
        [
            Project(
                name="api",
                provider=Provider.GITHUB,
                repository="acme/api",
                secret=GITHUB_SECRET,
                clone_url=str(repo),
            )
        ]
    )
    queue = create_queue(connection, QUEUE)

    with TestClient(create_app(settings, registry, RQDispatcher(queue))) as client:
        response = post_github(client, push_payload(after=sha))
        assert response.status_code == 202
        assert client.get("/health").json()["dispatcher"]["queued"] == 1

    run_id = response.json()["run_id"]
    configure_worker_service(RunService(settings, registry))
    run_worker_burst(connection)

    job = queue.fetch_job(run_id)
    assert job is not None
    assert job.get_status() == "finished"
    outcome = RunOutcome.model_validate(job.return_value())
    assert outcome.status == "completed", outcome.reason
    assert outcome.result is not None
    assert outcome.result.jobs["build"].steps[0].stdout == "fila main"
