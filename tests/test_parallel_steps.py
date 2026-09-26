"""Testes dos blocos ``parallel`` de steps."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orquestrador.cli import app
from orquestrador.db import PersistenceObserver, RunRepository
from orquestrador.execution import PipelineRunner, RunContext, Status
from orquestrador.execution.runner import FAIL_FAST_REASON, step_blocks
from orquestrador.executors import LocalExecutor
from orquestrador.pipeline import PipelineValidationError, parse_pipeline, parse_pipeline_data
from tests.conftest import make_pipeline, py
from tests.test_db_repository import make_request


def wait_for(me: str, other: str, name: str) -> dict[str, Any]:
    """Step que só termina se o outro step iniciar enquanto ele ainda roda."""
    code = f"""
    import pathlib, sys, time
    pathlib.Path('{me}.started').touch()
    deadline = time.time() + 15
    while not pathlib.Path('{other}.started').exists():
        if time.time() > deadline:
            sys.exit('o outro step não rodou em paralelo')
        time.sleep(0.05)
    print('{me} ok')
    """
    return py(code, name=name)


def run(pipeline: Any, tmp_path: Path, **kwargs: Any) -> Any:
    return PipelineRunner(LocalExecutor(), **kwargs).run(pipeline, RunContext(workspace=tmp_path))


class TestParsing:
    def test_parallel_block_is_expanded(self) -> None:
        pipeline = parse_pipeline(
            """
            name: x
            jobs:
              build:
                steps:
                  - run: echo antes
                  - name: testes
                    fail-fast: true
                    max-parallel: 2
                    parallel:
                      - {name: unit, run: pytest unit}
                      - {name: integration, run: pytest integration}
                      - {name: e2e, run: pytest e2e}
                  - parallel:
                      - run: lint
                      - run: typecheck
                  - run: echo depois
            """
        )
        job = pipeline.jobs["build"]
        assert [step.name for step in job.steps] == [
            "echo antes",
            "unit",
            "integration",
            "e2e",
            "lint",
            "typecheck",
            "echo depois",
        ]
        assert [step.group for step in job.steps] == [
            None,
            "testes",
            "testes",
            "testes",
            "paralelo-2",
            "paralelo-2",
            None,
        ]
        assert job.groups["testes"].fail_fast is True
        assert job.groups["testes"].max_parallel == 2
        assert job.groups["paralelo-2"].fail_fast is False
        assert [[i for i, _ in block] for block in step_blocks(job)] == [[0], [1, 2, 3], [4, 5], [6]]

    @pytest.mark.parametrize(
        ("steps", "fragment"),
        [
            ([{"parallel": [{"run": "a"}]}], "ao menos 2 steps"),
            ([{"parallel": "a"}], "ao menos 2 steps"),
            ([{"parallel": [{"run": "a"}, {"parallel": [{"run": "b"}, {"run": "c"}]}]}], "aninhados"),
            ([{"parallel": [{"run": "a"}, {"run": "b"}], "timeout": 5}], "campos desconhecidos"),
            (
                [
                    {"name": "g", "parallel": [{"run": "a"}, {"run": "b"}]},
                    {"name": "g", "parallel": [{"run": "c"}, {"run": "d"}]},
                ],
                "grupo paralelo repetido",
            ),
            ([{"run": "a", "group": "fantasma"}], "grupo paralelo inexistente"),
            ([{"parallel": [{"run": "a"}, {"run": "b"}], "max-parallel": 0}], "max-parallel"),
            ([{"parallel": [{"run": "a"}, {"nome": "b"}]}], "run"),
        ],
    )
    def test_invalid_parallel_blocks(self, steps: list[Any], fragment: str) -> None:
        with pytest.raises(PipelineValidationError) as info:
            parse_pipeline_data({"name": "x", "jobs": {"j": {"steps": steps}}})
        assert any(fragment in error for error in info.value.errors), info.value.errors


class TestExecution:
    def test_steps_in_group_run_concurrently(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(
            {
                "j": {
                    "steps": [
                        {"parallel": [wait_for("a", "b", "a"), wait_for("b", "a", "b")]},
                        py("print('depois')", name="depois"),
                    ]
                }
            }
        )
        result = run(pipeline, tmp_path)
        job = result.jobs["j"]
        assert result.status == Status.SUCCESS, [s.stderr for s in job.steps]
        assert [step.group for step in job.steps] == ["paralelo-1", "paralelo-1", None]
        assert job.steps[2].stdout == "depois"

    def test_group_failure_skips_following_steps(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(
            {
                "j": {
                    "steps": [
                        {
                            "parallel": [
                                py("import sys; sys.exit(2)", name="quebra"),
                                py("print('termina')", name="ok"),
                            ]
                        },
                        py("print('nunca')", name="seguinte"),
                        py("print('limpa')", name="limpeza", **{"if": "always()"}),
                    ]
                }
            }
        )
        job = run(pipeline, tmp_path).jobs["j"]
        assert [step.status for step in job.steps] == [
            Status.FAILURE,
            Status.SUCCESS,
            Status.SKIPPED,
            Status.SUCCESS,
        ]
        assert job.status == Status.FAILURE
        assert job.error == "step 'quebra' falhou"

    def test_fail_fast_cancels_siblings(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(
            {
                "j": {
                    "steps": [
                        {
                            "fail-fast": True,
                            "parallel": [
                                py("import sys, time; time.sleep(0.3); sys.exit(1)", name="rapido"),
                                py("import time; time.sleep(30)", name="lento"),
                            ],
                        }
                    ]
                }
            }
        )
        started = time.monotonic()
        job = run(pipeline, tmp_path).jobs["j"]
        assert time.monotonic() - started < 10
        assert [step.status for step in job.steps] == [Status.FAILURE, Status.CANCELLED]
        assert job.steps[1].error == FAIL_FAST_REASON
        assert job.status == Status.FAILURE

    def test_fail_fast_ignores_continue_on_error(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(
            {
                "j": {
                    "steps": [
                        {
                            "fail-fast": True,
                            "parallel": [
                                py("import sys; sys.exit(1)", **{"continue-on-error": True}),
                                py("import time; time.sleep(0.5); print('fim')", name="lento"),
                            ],
                        }
                    ]
                }
            }
        )
        job = run(pipeline, tmp_path).jobs["j"]
        assert [step.status for step in job.steps] == [Status.FAILURE, Status.SUCCESS]
        assert job.status == Status.SUCCESS

    def test_max_parallel_one_runs_sequentially(self, tmp_path: Path) -> None:
        exclusive = """
        import os, pathlib, sys, time
        lock = pathlib.Path('exclusivo.lock')
        if lock.exists():
            sys.exit('outro step do grupo estava rodando ao mesmo tempo')
        lock.touch()
        time.sleep(0.2)
        lock.unlink()
        print(os.environ['ORQ_STEP_GROUP'])
        """
        pipeline = make_pipeline(
            {"j": {"steps": [{"name": "seq", "max-parallel": 1, "parallel": [py(exclusive), py(exclusive), py(exclusive)]}]}}
        )
        job = run(pipeline, tmp_path).jobs["j"]
        assert job.status == Status.SUCCESS, [step.stderr for step in job.steps]
        assert [step.stdout for step in job.steps] == ["seq", "seq", "seq"]

    def test_parallel_steps_are_persisted_with_group(
        self, repository: RunRepository, tmp_path: Path
    ) -> None:
        repository.create_run(make_request())
        pipeline = make_pipeline(
            {"j": {"steps": [{"name": "grupo", "parallel": [py("print(1)"), py("print(2)")]}]}}
        )
        PipelineRunner(LocalExecutor(), [PersistenceObserver(repository, "run1")]).run(
            pipeline, RunContext(run_id="run1", workspace=tmp_path)
        )
        run_detail = repository.get_run("run1")
        assert run_detail is not None
        assert [step.group for step in run_detail.jobs[0].steps] == ["grupo", "grupo"]
        assert {step.status for step in run_detail.jobs[0].steps} == {"success"}


def test_cli_shows_parallel_group(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.yml"
    path.write_text(
        "name: p\njobs:\n  j:\n    steps:\n      - name: checks\n        parallel:\n"
        "          - {shell: python, run: 'print(1)'}\n          - {shell: python, run: 'print(2)'}\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["run", str(path), "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "[paralelo: checks]" in result.output
