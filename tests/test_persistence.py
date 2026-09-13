"""Testes da persistência integrada ao runner, ao RunService e aos dispatchers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orquestrador.config import Settings
from orquestrador.db import (
    DatabaseRunListener,
    NewLogLine,
    PersistenceObserver,
    PipelineIds,
    RunRepository,
    RunStatus,
)
from orquestrador.dispatch import (
    Dispatcher,
    ListeningDispatcher,
    RunListener,
    RunOutcome,
    RunRequest,
    RunService,
)
from orquestrador.execution import (
    CompositeObserver,
    LogLine,
    PipelineResult,
    PipelineRunner,
    RunContext,
)
from orquestrador.executors import LocalExecutor
from orquestrador.projects import Project, ProjectRegistry, Provider
from tests.conftest import GitRepoFactory, make_pipeline, py, requires_git
from tests.test_db_repository import make_request


def test_observer_persists_real_run(repository: RunRepository, tmp_path: Path) -> None:
    repository.create_run(make_request())
    pipeline = make_pipeline(
        {
            "build": {
                "steps": [
                    py("import sys\nprint('ola')\nprint('erro', file=sys.stderr)", name="saida"),
                    py("import sys; sys.exit(3)", name="quebra"),
                    py("print('nunca')", name="pulado"),
                ]
            },
            "deploy": {"needs": "build", "steps": [py("print(1)")]},
        }
    )
    observer = PersistenceObserver(repository, "run1")

    result = PipelineRunner(LocalExecutor(), [observer]).run(
        pipeline, RunContext(run_id="run1", workspace=tmp_path)
    )

    run = repository.get_run("run1")
    assert run is not None
    build, deploy = run.jobs
    assert build.status == RunStatus.FAILURE
    assert build.error == "step 'quebra' falhou"
    assert build.duration == pytest.approx(result.jobs["build"].duration)
    assert [step.status for step in build.steps] == [
        RunStatus.SUCCESS,
        RunStatus.FAILURE,
        RunStatus.SKIPPED,
    ]
    assert build.steps[0].started_at is not None
    assert build.steps[1].exit_code == 3
    assert deploy.status == RunStatus.SKIPPED
    assert deploy.steps[0].status == RunStatus.SKIPPED
    logs = repository.get_logs("run1", "build", 0) or []
    assert {(line.stream, line.text) for line in logs} == {("stdout", "ola"), ("stderr", "erro")}
    assert sorted(line.seq for line in logs) == [1, 2]


def test_observer_truncates_logs(repository: RunRepository, tmp_path: Path) -> None:
    repository.create_run(make_request())
    pipeline = make_pipeline(
        {"j": {"steps": [py("for i in range(10): print(f'linha-longa-{i}')")]}}
    )
    observer = PersistenceObserver(repository, "run1", max_log_lines_per_step=3, max_line_length=5)

    PipelineRunner(LocalExecutor(), [observer]).run(
        pipeline, RunContext(run_id="run1", workspace=tmp_path)
    )

    run = repository.get_run("run1")
    assert run is not None
    step = run.jobs[0].steps[0]
    assert (step.log_lines, step.truncated_lines) == (3, 7)
    assert [line.text for line in repository.get_logs("run1", "j", 0) or []] == ["linha"] * 3


class FakeRepository:
    """Registra as chamadas do observer sem banco."""

    def __init__(self, fail_register: bool = False) -> None:
        self.fail_register = fail_register
        self.batches: list[list[NewLogLine]] = []
        self.updates: list[tuple[str, int]] = []

    def register_pipeline(self, run_id: str, result: PipelineResult) -> PipelineIds:
        if self.fail_register:
            raise RuntimeError("banco fora do ar")
        return PipelineIds(jobs={"j": 10}, steps={("j", 0): 20})

    def append_logs(self, lines: list[NewLogLine]) -> None:
        self.batches.append(list(lines))

    def update_job(self, job_run_id: int, result: Any) -> None:
        self.updates.append(("job", job_run_id))

    def update_step(self, step_run_id: int, result: Any, **kwargs: Any) -> None:
        self.updates.append(("step", step_run_id))


def test_observer_flushes_logs_in_batches() -> None:
    fake = FakeRepository()
    pipeline = make_pipeline({"j": {"steps": [{"run": "x"}]}})
    result = PipelineRunner.create_result(pipeline, RunContext(run_id="r"))
    job, step = pipeline.jobs["j"], pipeline.jobs["j"].steps[0]
    step_result = result.jobs["j"].steps[0]
    observer = PersistenceObserver(fake, "r", batch_size=2, flush_interval=3600)  # type: ignore[arg-type]

    observer.on_pipeline_start(pipeline, RunContext(run_id="r"), result)
    for number in range(5):
        observer.on_step_output(job, step, step_result, LogLine(stream="stdout", text=str(number)))
    assert [len(batch) for batch in fake.batches] == [2, 2]

    observer.on_step_end(job, step, step_result)
    assert [len(batch) for batch in fake.batches] == [2, 2, 1]
    assert [line.seq for batch in fake.batches for line in batch] == [1, 2, 3, 4, 5]
    assert {line.step_run_id for batch in fake.batches for line in batch} == {20}
    assert ("step", 20) in fake.updates


def test_observer_is_noop_when_registration_fails() -> None:
    fake = FakeRepository(fail_register=True)
    pipeline = make_pipeline({"j": {"steps": [{"run": "x"}]}})
    result = PipelineRunner.create_result(pipeline, RunContext(run_id="r"))
    job, step = pipeline.jobs["j"], pipeline.jobs["j"].steps[0]
    composite = CompositeObserver([PersistenceObserver(fake, "r")])  # type: ignore[arg-type]

    composite.on_pipeline_start(pipeline, RunContext(run_id="r"), result)
    composite.on_job_start(job, result.jobs["j"])
    composite.on_step_start(job, step, result.jobs["j"].steps[0])
    composite.on_step_output(
        job, step, result.jobs["j"].steps[0], LogLine(stream="stdout", text="x")
    )
    composite.on_step_end(job, step, result.jobs["j"].steps[0])
    composite.on_job_end(job, result.jobs["j"])
    composite.on_pipeline_end(pipeline, result)

    assert fake.batches == []
    assert fake.updates == []


@requires_git
def test_run_service_records_full_history(
    repository: RunRepository, git_repo: GitRepoFactory, tmp_path: Path
) -> None:
    from tests.test_run_service import PIPELINE
    from tests.test_run_service import make_request as service_request

    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "banco"})
    registry = ProjectRegistry(
        [
            Project(
                name="app",
                provider=Provider.GITHUB,
                repository="acme/app",
                secret="s",
                clone_url=str(repo),
            )
        ]
    )
    service = RunService(
        Settings(data_dir=tmp_path / "data"),
        registry,
        listeners=[DatabaseRunListener(repository)],
    )
    request = service_request(sha)

    outcome = service.execute(request)

    run = repository.get_run(request.run_id)
    assert run is not None
    assert outcome.status == "completed"
    assert run.status == RunStatus.SUCCESS
    assert run.pipeline == "ci"
    assert run.started_at is not None and run.finished_at is not None
    assert run.duration is not None and run.duration > 0
    assert run.jobs[0].status == RunStatus.SUCCESS
    logs = repository.get_logs(request.run_id, "build", 0) or []
    assert [line.text for line in logs] == ["banco main"]


def test_run_service_records_errors(repository: RunRepository, tmp_path: Path) -> None:
    service = RunService(
        Settings(data_dir=tmp_path), ProjectRegistry(), listeners=[DatabaseRunListener(repository)]
    )
    request = make_request(project="fantasma")

    service.execute(request)

    run = repository.get_run(request.run_id)
    assert run is not None
    assert run.status == RunStatus.ERROR
    assert run.reason == "projeto 'fantasma' não configurado"


class CapturingDispatcher(Dispatcher):
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[RunRequest] = []
        self.error = error
        self.closed = False

    def dispatch(self, request: RunRequest) -> str:
        if self.error is not None:
            raise self.error
        self.requests.append(request)
        return request.run_id

    def stats(self) -> dict[str, Any]:
        return {"kind": "capturing"}

    def shutdown(self, *, wait: bool = True) -> None:
        self.closed = True


def test_listening_dispatcher_records_queued_run(repository: RunRepository) -> None:
    inner = CapturingDispatcher()
    dispatcher = ListeningDispatcher(inner, [DatabaseRunListener(repository)])
    request = make_request()

    assert dispatcher.dispatch(request) == request.run_id

    assert inner.requests == [request]
    run = repository.get_run(request.run_id)
    assert run is not None and run.status == RunStatus.QUEUED
    assert dispatcher.stats() == {"kind": "capturing"}
    dispatcher.shutdown()
    assert inner.closed


def test_listening_dispatcher_records_dispatch_failure(repository: RunRepository) -> None:
    dispatcher = ListeningDispatcher(
        CapturingDispatcher(ConnectionError("Redis fora")), [DatabaseRunListener(repository)]
    )
    request = make_request()

    with pytest.raises(ConnectionError):
        dispatcher.dispatch(request)

    run = repository.get_run(request.run_id)
    assert run is not None
    assert run.status == RunStatus.ERROR
    assert run.reason == "falha ao agendar a execução: Redis fora"


class BrokenListener(RunListener):
    def run_queued(self, request: RunRequest) -> None:
        raise RuntimeError("queued")

    def run_started(self, request: RunRequest) -> None:
        raise RuntimeError("started")

    def run_observers(self, request: RunRequest) -> list[Any]:
        raise RuntimeError("observers")

    def run_finished(self, outcome: RunOutcome) -> None:
        raise RuntimeError("finished")


def test_broken_listeners_never_break_runs(tmp_path: Path) -> None:
    service = RunService(
        Settings(data_dir=tmp_path), ProjectRegistry(), listeners=[BrokenListener()]
    )
    outcome = service.execute(make_request(project="nenhum"))
    assert outcome.status == "error"

    inner = CapturingDispatcher()
    request = make_request()
    assert ListeningDispatcher(inner, [BrokenListener()]).dispatch(request) == request.run_id
