"""Testes do repositório de execuções (SQLite com as migrações reais)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from orquestrador.db import Database, NewLogLine, RunRepository, RunStatus
from orquestrador.db.models import JobRow, LogLineRow, StepRow
from orquestrador.dispatch import RunOutcome, RunRequest
from orquestrador.execution import PipelineResult, PipelineRunner, RunContext, Status
from orquestrador.projects import Provider
from orquestrador.webhooks import TriggerEvent
from tests.conftest import make_pipeline

BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_request(run_id: str = "run1", project: str = "api", minutes: int = 0) -> RunRequest:
    trigger = TriggerEvent(
        provider=Provider.GITHUB,
        event="push",
        repository="acme/api",
        ref="refs/heads/main",
        branch="main",
        commit="a" * 40,
        actor="octocat",
        message="corrige bug",
    )
    return RunRequest(
        run_id=run_id,
        project=project,
        trigger=trigger,
        requested_at=BASE_TIME + timedelta(minutes=minutes),
    )


def make_result(run_id: str = "run1", status: Status = Status.PENDING) -> PipelineResult:
    pipeline = make_pipeline(
        {
            "build": {"steps": [{"name": "compilar", "run": "a"}, {"name": "testar", "run": "b"}]},
            "deploy": {"needs": "build", "steps": [{"run": "c"}]},
        }
    )
    result = PipelineRunner.create_result(pipeline, RunContext(run_id=run_id))
    result.status = status
    return result


def completed(run_id: str, status: Status, duration: float = 4.2) -> RunOutcome:
    result = make_result(run_id, status)
    result.duration = duration
    return RunOutcome(run_id=run_id, project="api", status="completed", result=result)


def test_create_and_get_run(repository: RunRepository) -> None:
    assert repository.create_run(make_request()) is True

    run = repository.get_run("run1")

    assert run is not None
    assert run.status == RunStatus.QUEUED
    assert (run.project, run.provider, run.repository, run.event) == (
        "api",
        "github",
        "acme/api",
        "push",
    )
    assert (run.branch, run.commit, run.actor, run.message) == (
        "main",
        "a" * 40,
        "octocat",
        "corrige bug",
    )
    assert run.created_at == BASE_TIME
    assert run.created_at.tzinfo is not None
    assert run.started_at is None
    assert run.jobs == []


def test_create_run_is_idempotent(repository: RunRepository) -> None:
    assert repository.create_run(make_request()) is True
    assert repository.create_run(make_request()) is False


def test_start_run_creates_missing_run(repository: RunRepository) -> None:
    repository.start_run(make_request())
    run = repository.get_run("run1")
    assert run is not None
    assert run.status == RunStatus.RUNNING
    assert run.started_at is not None


def test_start_run_updates_queued_run(repository: RunRepository) -> None:
    repository.create_run(make_request())
    repository.start_run(make_request())
    run = repository.get_run("run1")
    assert run is not None and run.status == RunStatus.RUNNING


def test_register_pipeline(repository: RunRepository) -> None:
    repository.create_run(make_request())

    ids = repository.register_pipeline("run1", make_result())

    assert set(ids.jobs) == {"build", "deploy"}
    assert set(ids.steps) == {("build", 0), ("build", 1), ("deploy", 0)}
    run = repository.get_run("run1")
    assert run is not None
    assert run.pipeline == "teste"
    assert [job.job_id for job in run.jobs] == ["build", "deploy"]
    assert [step.name for step in run.jobs[0].steps] == ["compilar", "testar"]
    assert {step.status for job in run.jobs for step in job.steps} == {RunStatus.PENDING}


def test_register_pipeline_again_replaces_jobs(repository: RunRepository) -> None:
    repository.create_run(make_request())
    repository.register_pipeline("run1", make_result())
    repository.register_pipeline("run1", make_result())
    run = repository.get_run("run1")
    assert run is not None and len(run.jobs) == 2


def test_register_pipeline_for_unknown_run(repository: RunRepository) -> None:
    with pytest.raises(LookupError):
        repository.register_pipeline("fantasma", make_result())


def test_update_job_and_step(repository: RunRepository) -> None:
    repository.create_run(make_request())
    result = make_result()
    ids = repository.register_pipeline("run1", result)
    job = result.jobs["build"]
    job.status = Status.FAILURE
    job.error = "step 'testar' falhou"
    job.started_at = BASE_TIME
    job.finished_at = BASE_TIME + timedelta(seconds=3)
    job.duration = 3.0
    step = job.steps[1]
    step.status = Status.FAILURE
    step.exit_code = 2
    step.error = "comando terminou com código de saída 2"
    step.duration = 1.5

    repository.update_job(ids.jobs["build"], job)
    repository.update_step(ids.steps[("build", 1)], step, truncated_lines=5)
    repository.update_job(999_999, job)  # inexistente: ignorado

    run = repository.get_run("run1")
    assert run is not None
    build = run.jobs[0]
    assert (build.status, build.error, build.duration) == (RunStatus.FAILURE, job.error, 3.0)
    assert build.started_at == BASE_TIME
    assert build.steps[1].status == RunStatus.FAILURE
    assert build.steps[1].exit_code == 2
    assert build.steps[1].truncated_lines == 5
    assert build.steps[0].status == RunStatus.PENDING


def test_logs(repository: RunRepository) -> None:
    repository.create_run(make_request())
    ids = repository.register_pipeline("run1", make_result())
    step_id = ids.steps[("build", 0)]
    repository.append_logs(
        [
            NewLogLine(step_id, seq, "stdout" if seq % 2 else "stderr", f"linha {seq}", BASE_TIME)
            for seq in range(1, 6)
        ]
    )
    repository.append_logs([])

    logs = repository.get_logs("run1", "build", 0)
    assert logs is not None
    assert [line.seq for line in logs] == [1, 2, 3, 4, 5]
    assert (logs[0].stream, logs[0].text, logs[0].timestamp) == ("stdout", "linha 1", BASE_TIME)
    assert logs[1].stream == "stderr"
    assert [line.seq for line in repository.get_logs("run1", "build", 0, after=3) or []] == [4, 5]
    assert [line.seq for line in repository.get_logs("run1", "build", 0, limit=2) or []] == [1, 2]

    run = repository.get_run("run1")
    assert run is not None
    assert run.jobs[0].steps[0].log_lines == 5
    assert run.jobs[0].steps[1].log_lines == 0


def test_logs_for_unknown_step(repository: RunRepository) -> None:
    repository.create_run(make_request())
    repository.register_pipeline("run1", make_result())
    assert repository.get_logs("run1", "build", 9) is None
    assert repository.get_logs("run1", "fantasma", 0) is None
    assert repository.get_logs("outra", "build", 0) is None


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (completed("run1", Status.SUCCESS), RunStatus.SUCCESS),
        (completed("run1", Status.FAILURE), RunStatus.FAILURE),
        (completed("run1", Status.CANCELLED), RunStatus.CANCELLED),
        (
            RunOutcome(run_id="run1", project="api", status="skipped", reason="gatilho"),
            RunStatus.SKIPPED,
        ),
        (
            RunOutcome(run_id="run1", project="api", status="error", reason="checkout"),
            RunStatus.ERROR,
        ),
    ],
)
def test_finish_run_status_mapping(
    repository: RunRepository, outcome: RunOutcome, expected: RunStatus
) -> None:
    repository.create_run(make_request())
    repository.start_run(make_request())

    repository.finish_run(outcome)

    run = repository.get_run("run1")
    assert run is not None
    assert run.status == expected
    assert run.reason == outcome.reason
    assert run.finished_at is not None
    if outcome.result is not None:
        assert run.duration == 4.2
        assert run.pipeline == "teste"
    else:
        assert run.duration is not None and run.duration >= 0


def test_finish_unknown_run_is_ignored(repository: RunRepository) -> None:
    repository.finish_run(completed("fantasma", Status.SUCCESS))
    assert repository.get_run("fantasma") is None


def test_fail_run(repository: RunRepository) -> None:
    repository.create_run(make_request())
    repository.fail_run("run1", "Redis indisponível")
    repository.fail_run("fantasma", "ignorado")
    run = repository.get_run("run1")
    assert run is not None
    assert (run.status, run.reason) == (RunStatus.ERROR, "Redis indisponível")


def test_list_runs_filters_and_pagination(repository: RunRepository) -> None:
    fixtures: list[tuple[str, Status | None]] = [
        ("api", Status.SUCCESS),
        ("api", Status.FAILURE),
        ("web", Status.SUCCESS),
        ("api", Status.SUCCESS),
        ("web", None),
    ]
    for position, (project, status) in enumerate(fixtures):
        run_id = f"run{position}"
        repository.create_run(make_request(run_id, project, minutes=position))
        if status is not None:
            repository.finish_run(completed(run_id, status))

    def ids(**filters: Any) -> list[str]:
        return [run.run_id for run in repository.list_runs(**filters).items]

    assert ids() == ["run4", "run3", "run2", "run1", "run0"]
    assert repository.list_runs().total == 5
    assert repository.list_runs(project="api").total == 3
    assert ids(status=RunStatus.SUCCESS) == ["run3", "run2", "run0"]
    assert ids(status="queued") == ["run4"]
    assert ids(project="api", status=RunStatus.SUCCESS) == ["run3", "run0"]
    page = repository.list_runs(limit=2, offset=2)
    assert [run.run_id for run in page.items] == ["run2", "run1"]
    assert (page.total, page.limit, page.offset) == (5, 2, 2)


def test_delete_run_cascades(repository: RunRepository, database: Database) -> None:
    repository.create_run(make_request())
    ids = repository.register_pipeline("run1", make_result())
    repository.append_logs([NewLogLine(ids.steps[("build", 0)], 1, "stdout", "x", BASE_TIME)])

    assert repository.delete_run("run1") is True
    assert repository.delete_run("run1") is False

    assert repository.get_run("run1") is None
    with database.session() as session:
        for model in (JobRow, StepRow, LogLineRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0
