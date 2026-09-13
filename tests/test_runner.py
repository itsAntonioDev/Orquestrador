"""Testes do ``PipelineRunner``."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orquestrador.execution import (
    JobResult,
    LogLine,
    PipelineResult,
    PipelineRunner,
    RunContext,
    RunObserver,
    Status,
    StepResult,
)
from orquestrador.executors import (
    CommandRequest,
    CommandResult,
    Executor,
    JobSession,
    LocalExecutor,
)
from orquestrador.executors.base import OutputCallback
from orquestrador.pipeline import Job, Pipeline, Step
from tests.conftest import make_pipeline, py

RunFn = Callable[..., PipelineResult]


@pytest.fixture
def run(tmp_path: Path) -> RunFn:
    def _run(
        pipeline: Pipeline,
        *,
        observers: tuple[RunObserver, ...] = (),
        parallel: int = 1,
        executor: Executor | None = None,
        context: RunContext | None = None,
        **context_kwargs: Any,
    ) -> PipelineResult:
        runner = PipelineRunner(executor or LocalExecutor(), observers, max_parallel_jobs=parallel)
        ctx = context or RunContext(workspace=tmp_path, **context_kwargs)
        return runner.run(pipeline, ctx)

    return _run


class RecordingObserver(RunObserver):
    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []

    def on_pipeline_start(
        self, pipeline: Pipeline, context: RunContext, result: PipelineResult
    ) -> None:
        self.events.append(("pipeline_start", pipeline.name))

    def on_pipeline_end(self, pipeline: Pipeline, result: PipelineResult) -> None:
        self.events.append(("pipeline_end", result.status))

    def on_job_start(self, job: Job, result: JobResult) -> None:
        self.events.append(("job_start", job.id))

    def on_job_end(self, job: Job, result: JobResult) -> None:
        self.events.append(("job_end", job.id, result.status))

    def on_step_start(self, job: Job, step: Step, result: StepResult) -> None:
        self.events.append(("step_start", step.name))

    def on_step_output(self, job: Job, step: Step, result: StepResult, line: LogLine) -> None:
        self.events.append(("output", line.text))

    def on_step_end(self, job: Job, step: Step, result: StepResult) -> None:
        self.events.append(("step_end", step.name, result.status))


def statuses(job: JobResult) -> list[Status]:
    return [step.status for step in job.steps]


def test_successful_pipeline(run: RunFn) -> None:
    pipeline = make_pipeline(
        {"build": {"steps": [py("print('um')", name="a"), py("print('dois')", name="b")]}}
    )
    result = run(pipeline)

    assert result.status == Status.SUCCESS
    job = result.jobs["build"]
    assert job.status == Status.SUCCESS
    assert statuses(job) == [Status.SUCCESS, Status.SUCCESS]
    assert job.steps[0].stdout == "um"
    assert job.steps[1].exit_code == 0
    assert job.steps[0].duration > 0
    assert job.steps[0].started_at is not None and job.steps[0].finished_at is not None
    assert result.started_at is not None and result.finished_at is not None
    assert result.duration >= job.duration > 0


def test_failing_step_stops_job(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "build": {
                "steps": [
                    py("print('ok')"),
                    py("import sys; print('boom', file=sys.stderr); sys.exit(2)", name="quebra"),
                    py("print('nunca')", name="depois"),
                ]
            }
        }
    )
    result = run(pipeline)
    job = result.jobs["build"]

    assert result.status == Status.FAILURE
    assert job.status == Status.FAILURE
    assert statuses(job) == [Status.SUCCESS, Status.FAILURE, Status.SKIPPED]
    assert job.steps[1].exit_code == 2
    assert job.steps[1].stderr == "boom"
    assert "código de saída 2" in (job.steps[1].error or "")
    assert job.error == "step 'quebra' falhou"
    assert job.steps[2].stdout == ""


def test_continue_on_error_does_not_stop_job(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "build": {
                "steps": [
                    py("import sys; sys.exit(1)", **{"continue-on-error": True}),
                    py("print('continua')"),
                ]
            }
        }
    )
    result = run(pipeline)
    job = result.jobs["build"]
    assert statuses(job) == [Status.FAILURE, Status.SUCCESS]
    assert job.steps[0].allowed_failure
    assert job.status == Status.SUCCESS
    assert result.status == Status.SUCCESS


def test_always_and_failure_steps_run_after_failure(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "build": {
                "steps": [
                    py("import sys; sys.exit(1)"),
                    py("print('normal')"),
                    py("print('limpeza')", **{"if": "always()"}),
                    py("print('notifica')", **{"if": "failure()"}),
                    py("print('ok?')", **{"if": "success()"}),
                ]
            }
        }
    )
    job = run(pipeline).jobs["build"]
    assert statuses(job) == [
        Status.FAILURE,
        Status.SKIPPED,
        Status.SUCCESS,
        Status.SUCCESS,
        Status.SKIPPED,
    ]
    assert job.status == Status.FAILURE


def test_dependent_job_skipped_when_dependency_fails(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "build": {"steps": [py("import sys; sys.exit(1)")]},
            "test": {"needs": "build", "steps": [py("print('t')")]},
            "report": {"needs": "build", "if": "always()", "steps": [py("print('r')")]},
            "independent": {"steps": [py("print('i')")]},
        }
    )
    result = run(pipeline)
    assert result.jobs["build"].status == Status.FAILURE
    assert result.jobs["test"].status == Status.SKIPPED
    assert statuses(result.jobs["test"]) == [Status.SKIPPED]
    assert result.jobs["report"].status == Status.SUCCESS
    assert result.jobs["independent"].status == Status.SUCCESS
    assert result.status == Status.FAILURE


def test_skipped_dependency_skips_dependents(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "deploy": {"if": "branch == 'main'", "steps": [py("print('d')")]},
            "smoke": {"needs": "deploy", "steps": [py("print('s')")]},
        }
    )
    result = run(pipeline, branch="dev")
    assert result.jobs["deploy"].status == Status.SKIPPED
    assert result.jobs["smoke"].status == Status.SKIPPED
    assert result.status == Status.SUCCESS


def test_job_continue_on_error_lets_dependents_run(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "lint": {"continue-on-error": True, "steps": [py("import sys; sys.exit(1)")]},
            "build": {"needs": "lint", "steps": [py("print('b')")]},
        }
    )
    result = run(pipeline)
    assert result.jobs["lint"].status == Status.FAILURE
    assert result.jobs["build"].status == Status.SUCCESS
    assert result.status == Status.SUCCESS


def test_job_condition_uses_context(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "deploy": {
                "if": "branch == 'main' && event == 'push'",
                "steps": [py("print('deploy')")],
            }
        }
    )
    assert run(pipeline, branch="main", event="push").jobs["deploy"].status == Status.SUCCESS
    assert run(pipeline, branch="main", event="manual").jobs["deploy"].status == Status.SKIPPED


def test_step_condition_uses_env(run: RunFn) -> None:
    pipeline = make_pipeline(
        {"j": {"env": {"DEPLOY": "no"}, "steps": [py("print(1)", **{"if": "env.DEPLOY == 'yes'"})]}}
    )
    assert statuses(run(pipeline).jobs["j"]) == [Status.SKIPPED]


def test_invalid_job_condition_fails_job(run: RunFn) -> None:
    pipeline = make_pipeline({"j": {"if": "__import__('os')", "steps": [py("print(1)")]}})
    result = run(pipeline)
    assert result.jobs["j"].status == Status.FAILURE
    assert "condição 'if' inválida" in (result.jobs["j"].error or "")
    assert result.status == Status.FAILURE


def test_invalid_step_condition_fails_step(run: RunFn) -> None:
    pipeline = make_pipeline(
        {"j": {"steps": [py("print(1)", **{"if": "nope =="}), py("print(2)")]}}
    )
    job = run(pipeline).jobs["j"]
    assert statuses(job) == [Status.FAILURE, Status.SKIPPED]
    assert "condição 'if' inválida" in (job.steps[0].error or "")


def test_env_precedence_and_builtin_variables(run: RunFn) -> None:
    code = """
    import json, os
    keys = ['A', 'B', 'C', 'D', 'CI',
            'ORQ_RUN_ID', 'ORQ_JOB', 'ORQ_STEP', 'ORQ_BRANCH', 'ORQ_PIPELINE']
    print(json.dumps({k: os.environ.get(k) for k in keys}))
    """
    pipeline = make_pipeline(
        {
            "j": {
                "env": {"B": "job", "C": "job"},
                "steps": [py(code, name="mostra", env={"C": "step"})],
            }
        },
        env={"A": "pipeline", "B": "pipeline", "C": "pipeline", "D": "pipeline"},
    )
    context = RunContext(run_id="run42", branch="main", env={"D": "cli"})
    context.workspace = Path.cwd()
    values = json.loads(run(pipeline, context=context).jobs["j"].steps[0].stdout)
    assert values == {
        "A": "pipeline",
        "B": "job",
        "C": "step",
        "D": "cli",
        "CI": "true",
        "ORQ_RUN_ID": "run42",
        "ORQ_JOB": "j",
        "ORQ_STEP": "mostra",
        "ORQ_BRANCH": "main",
        "ORQ_PIPELINE": "teste",
    }


def test_step_timeout(run: RunFn) -> None:
    pipeline = make_pipeline(
        {"j": {"steps": [py("import time; time.sleep(30)", timeout=0.5), py("print('x')")]}}
    )
    started = time.monotonic()
    job = run(pipeline).jobs["j"]
    assert time.monotonic() - started < 10
    assert statuses(job) == [Status.FAILURE, Status.SKIPPED]
    assert job.steps[0].error == "tempo limite do step excedido (0.5s)"


def test_job_timeout(run: RunFn) -> None:
    pipeline = make_pipeline(
        {
            "j": {
                "timeout": 1,
                "steps": [
                    py("import time; time.sleep(30)"),
                    py("print('limpeza')", **{"if": "always()"}),
                ],
            }
        }
    )
    started = time.monotonic()
    job = run(pipeline).jobs["j"]
    assert time.monotonic() - started < 10
    assert job.status == Status.FAILURE
    assert job.error == "tempo limite do job excedido (1s)"
    assert job.steps[0].error == "tempo limite do job excedido"
    assert job.steps[1].status == Status.SKIPPED


def test_observer_receives_events_in_order(run: RunFn) -> None:
    observer = RecordingObserver()
    pipeline = make_pipeline(
        {
            "a": {"steps": [py("print('saida')", name="s1")]},
            "b": {"needs": "a", "if": "false", "steps": [py("print(2)", name="s2")]},
        }
    )
    run(pipeline, observers=(observer,))
    assert observer.events == [
        ("pipeline_start", "teste"),
        ("job_start", "a"),
        ("step_start", "s1"),
        ("output", "saida"),
        ("step_end", "s1", Status.SUCCESS),
        ("job_end", "a", Status.SUCCESS),
        ("job_end", "b", Status.SKIPPED),
        ("pipeline_end", Status.SUCCESS),
    ]


def test_broken_observer_does_not_break_run(run: RunFn) -> None:
    class Broken(RunObserver):
        def on_step_output(self, *args: object) -> None:
            raise RuntimeError("observador quebrado")

    recording = RecordingObserver()
    result = run(make_pipeline({"j": {"steps": [py("print(1)")]}}), observers=(Broken(), recording))
    assert result.status == Status.SUCCESS
    assert ("output", "1") in recording.events


def test_parallel_jobs_run_concurrently(run: RunFn, tmp_path: Path) -> None:
    def wait_for(me: str, other: str) -> dict[str, Any]:
        code = f"""
        import pathlib, sys, time
        pathlib.Path('{me}.started').touch()
        deadline = time.time() + 15
        while not pathlib.Path('{other}.started').exists():
            if time.time() > deadline:
                sys.exit('o outro job não iniciou em paralelo')
            time.sleep(0.05)
        """
        return py(code)

    pipeline = make_pipeline(
        {"a": {"steps": [wait_for("a", "b")]}, "b": {"steps": [wait_for("b", "a")]}}
    )
    result = run(pipeline, parallel=2)
    assert result.status == Status.SUCCESS, {k: j.steps[0].stderr for k, j in result.jobs.items()}


class FakeSession(JobSession):
    def __init__(
        self, behaviour: Callable[[CommandRequest, OutputCallback], CommandResult]
    ) -> None:
        self.behaviour = behaviour
        self.closed = False

    def run(self, request: CommandRequest, on_output: OutputCallback) -> CommandResult:
        return self.behaviour(request, on_output)

    def close(self) -> None:
        self.closed = True


class FakeExecutor(Executor):
    name = "fake"

    def __init__(
        self, behaviour: Callable[[CommandRequest, OutputCallback], CommandResult]
    ) -> None:
        self.behaviour = behaviour
        self.sessions: list[FakeSession] = []

    def open_session(self, job: Job, context: RunContext) -> JobSession:
        session = FakeSession(self.behaviour)
        self.sessions.append(session)
        return session


def test_runner_works_with_any_executor(run: RunFn) -> None:
    def behaviour(request: CommandRequest, on_output: OutputCallback) -> CommandResult:
        on_output("stdout", f"fake: {request.command}")
        return CommandResult(exit_code=0)

    executor = FakeExecutor(behaviour)
    result = run(make_pipeline({"j": {"steps": [{"run": "make build"}]}}), executor=executor)
    assert result.jobs["j"].steps[0].stdout == "fake: make build"
    assert executor.sessions[0].closed


def test_executor_setup_failure_fails_job(run: RunFn) -> None:
    class Broken(Executor):
        def open_session(self, job: Job, context: RunContext) -> JobSession:
            raise RuntimeError("docker indisponível")

    result = run(make_pipeline({"j": {"steps": [{"run": "x"}]}}), executor=Broken())
    job = result.jobs["j"]
    assert job.status == Status.FAILURE
    assert job.error == "falha ao preparar o ambiente do job: docker indisponível"
    assert statuses(job) == [Status.SKIPPED]


def test_executor_exception_fails_step_and_closes_session(run: RunFn) -> None:
    def behaviour(request: CommandRequest, on_output: OutputCallback) -> CommandResult:
        raise RuntimeError("explodiu")

    executor = FakeExecutor(behaviour)
    job = run(make_pipeline({"j": {"steps": [{"run": "x"}]}}), executor=executor).jobs["j"]
    assert job.status == Status.FAILURE
    assert job.steps[0].error == "erro interno do executor: explodiu"
    assert executor.sessions[0].closed


def test_infrastructure_error_is_reported(run: RunFn) -> None:
    executor = FakeExecutor(lambda req, out: CommandResult(exit_code=None, error="shell ausente"))
    job = run(make_pipeline({"j": {"steps": [{"run": "x"}]}}), executor=executor).jobs["j"]
    assert job.steps[0].error == "shell ausente"
    assert job.status == Status.FAILURE


def test_cancelled_before_start(run: RunFn, tmp_path: Path) -> None:
    context = RunContext(workspace=tmp_path)
    context.cancel()
    result = run(make_pipeline({"j": {"steps": [py("print(1)")]}}), context=context)
    assert result.status == Status.CANCELLED
    assert result.jobs["j"].status == Status.CANCELLED
    assert statuses(result.jobs["j"]) == [Status.CANCELLED]


def test_cancel_during_run(run: RunFn, tmp_path: Path) -> None:
    context = RunContext(workspace=tmp_path)

    class CancelOnStart(RunObserver):
        def on_step_start(self, job: Job, step: Step, result: StepResult) -> None:
            context.cancel()

    pipeline = make_pipeline(
        {
            "a": {"steps": [py("import time; time.sleep(30)"), py("print(2)")]},
            "b": {"needs": "a", "steps": [py("print(3)")]},
        }
    )
    started = time.monotonic()
    result = run(pipeline, context=context, observers=(CancelOnStart(),))
    assert time.monotonic() - started < 10
    assert result.status == Status.CANCELLED
    assert statuses(result.jobs["a"]) == [Status.CANCELLED, Status.CANCELLED]
    assert result.jobs["b"].status == Status.CANCELLED


def test_create_result_prepopulates_pending_entries() -> None:
    pipeline = make_pipeline(
        {"j": {"steps": [{"run": "a"}, {"run": "b", "continue-on-error": True}]}}
    )
    result = PipelineRunner.create_result(pipeline, RunContext(run_id="abc"))
    assert result.run_id == "abc"
    assert result.status == Status.PENDING
    assert [s.name for s in result.jobs["j"].steps] == ["a", "b"]
    assert result.jobs["j"].steps[1].allowed_failure
    assert json.loads(result.model_dump_json())["jobs"]["j"]["status"] == "pending"


def test_invalid_parallelism() -> None:
    with pytest.raises(ValueError):
        PipelineRunner(LocalExecutor(), max_parallel_jobs=0)
