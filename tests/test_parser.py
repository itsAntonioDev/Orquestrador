"""Testes do parser e dos modelos de pipeline."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from orquestrador.pipeline import (
    PipelineError,
    PipelineValidationError,
    load_pipeline,
    parse_pipeline,
    parse_pipeline_data,
)

VALID = """
name: demo
on:
  push:
    branches: [main]
env:
  GLOBAL: 1
  FLAG: true
  EMPTY:
jobs:
  build:
    steps:
      - name: compile
        run: echo build
  test:
    needs: build
    if: branch == 'main'
    timeout: 60
    steps:
      - run: echo test
        continue-on-error: true
        timeout: 5
        working-directory: src
"""


def errors_of(text: str) -> list[str]:
    with pytest.raises(PipelineValidationError) as info:
        parse_pipeline(text)
    return info.value.errors


def test_parse_valid_pipeline() -> None:
    pipeline = parse_pipeline(VALID)

    assert pipeline.name == "demo"
    assert pipeline.triggers["push"].branches == ["main"]
    assert pipeline.env == {"GLOBAL": "1", "FLAG": "true", "EMPTY": ""}
    assert list(pipeline.jobs) == ["build", "test"]

    build = pipeline.jobs["build"]
    assert build.id == "build"
    assert build.name == "build"
    assert build.steps[0].name == "compile"

    test = pipeline.jobs["test"]
    assert test.needs == ["build"]
    assert test.condition == "branch == 'main'"
    assert test.timeout == 60
    step = test.steps[0]
    assert step.name == "echo test"
    assert step.continue_on_error is True
    assert step.timeout == 5
    assert step.working_directory == "src"


def test_yes_and_no_are_strings_not_booleans() -> None:
    pipeline = parse_pipeline(
        """
        name: x
        env: {A: yes, B: no, C: on}
        jobs:
          j: {steps: [{run: echo}]}
        """
    )
    assert pipeline.env == {"A": "yes", "B": "no", "C": "on"}


@pytest.mark.parametrize(
    ("on_value", "expected"),
    [
        ("push", ["push"]),
        ("[push, pull_request]", ["push", "pull_request"]),
        ("{push: {branches: main}, tag: }", ["push", "tag"]),
    ],
)
def test_trigger_shorthands(on_value: str, expected: list[str]) -> None:
    pipeline = parse_pipeline(f"name: x\non: {on_value}\njobs:\n  j: {{steps: [{{run: echo}}]}}\n")
    assert list(pipeline.triggers) == expected


def test_trigger_branch_string_becomes_list() -> None:
    pipeline = parse_pipeline(
        "name: x\non: {push: {branches: main}}\njobs: {j: {steps: [{run: a}]}}"
    )
    assert pipeline.triggers["push"].branches == ["main"]


def test_long_command_name_is_truncated() -> None:
    pipeline = parse_pipeline_data({"name": "x", "jobs": {"j": {"steps": [{"run": "a" * 100}]}}})
    assert len(pipeline.jobs["j"].steps[0].name) == 60


def test_missing_name() -> None:
    errors = errors_of("jobs: {j: {steps: [{run: echo}]}}")
    assert errors == ["name: campo obrigatório"]


def test_missing_jobs() -> None:
    assert "jobs: campo obrigatório" in errors_of("name: x")


def test_unknown_field_is_rejected() -> None:
    errors = errors_of("name: x\njobs:\n  j:\n    stepz: []\n    steps: [{run: echo}]")
    assert "jobs.j.stepz: campo desconhecido" in errors


def test_empty_steps() -> None:
    errors = errors_of("name: x\njobs: {j: {steps: []}}")
    assert any(e.startswith("jobs.j.steps: deve conter ao menos 1") for e in errors)


def test_step_without_run() -> None:
    assert "jobs.j.steps.0.run: campo obrigatório" in errors_of(
        "name: x\njobs: {j: {steps: [{name: a}]}}"
    )


def test_blank_run() -> None:
    errors = errors_of("name: x\njobs: {j: {steps: [{run: '   '}]}}")
    assert any("run não pode ser vazio" in e for e in errors)


def test_invalid_job_id() -> None:
    errors = errors_of("name: x\njobs: {'build job': {steps: [{run: echo}]}}")
    assert any("ID de job inválido" in e for e in errors)


def test_needs_unknown_job() -> None:
    errors = errors_of("name: x\njobs: {a: {needs: ghost, steps: [{run: echo}]}}")
    assert any("job inexistente 'ghost'" in e for e in errors)


def test_self_dependency() -> None:
    errors = errors_of("name: x\njobs: {a: {needs: a, steps: [{run: echo}]}}")
    assert any("de si mesmo" in e for e in errors)


def test_dependency_cycle() -> None:
    errors = errors_of(
        """
        name: x
        jobs:
          a: {needs: c, steps: [{run: echo}]}
          b: {needs: a, steps: [{run: echo}]}
          c: {needs: b, steps: [{run: echo}]}
        """
    )
    assert any("dependência circular" in e for e in errors)


def test_invalid_env_name() -> None:
    errors = errors_of("name: x\nenv: {'1BAD': x}\njobs: {j: {steps: [{run: a}]}}")
    assert any("nome de variável de ambiente inválido" in e for e in errors)


def test_env_nested_value() -> None:
    errors = errors_of("name: x\nenv: {A: [1, 2]}\njobs: {j: {steps: [{run: a}]}}")
    assert any("deve ser escalar" in e for e in errors)


def test_env_must_be_mapping() -> None:
    errors = errors_of("name: x\nenv: [A]\njobs: {j: {steps: [{run: a}]}}")
    assert any("env deve ser um mapeamento" in e for e in errors)


def test_non_positive_timeout() -> None:
    errors = errors_of("name: x\njobs: {j: {steps: [{run: a, timeout: 0}]}}")
    assert "jobs.j.steps.0.timeout: deve ser maior que 0" in errors


def test_invalid_yaml_syntax() -> None:
    errors = errors_of("name: x\njobs: [unclosed")
    assert errors[0].startswith("YAML inválido")


def test_empty_document() -> None:
    assert errors_of("") == ["o documento está vazio"]


def test_root_must_be_mapping() -> None:
    assert errors_of("- a\n- b") == ["o documento deve ser um mapeamento (chave: valor)"]


def test_duplicate_keys_are_rejected() -> None:
    errors = errors_of(
        """
        name: x
        jobs:
          build: {steps: [{run: a}]}
          build: {steps: [{run: b}]}
        """
    )
    assert "chave duplicada: 'build'" in errors[0]


def test_yaml_anchors_and_merge_keys_work() -> None:
    pipeline = parse_pipeline(
        """
        name: x
        jobs:
          base: &base
            timeout: 10
            steps: [{run: a}]
          j:
            <<: *base
            timeout: 20
        """
    )
    assert pipeline.jobs["j"].timeout == 20
    assert pipeline.jobs["j"].steps[0].run == "a"


def test_error_message_includes_source(write_file: Callable[..., Path]) -> None:
    path = write_file("name: x\n")
    with pytest.raises(PipelineValidationError) as info:
        load_pipeline(path)
    assert str(path) in str(info.value)
    assert info.value.source == str(path)


def test_load_pipeline_from_file(write_file: Callable[..., Path]) -> None:
    path = write_file(VALID)
    assert load_pipeline(path).name == "demo"


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PipelineError) as info:
        load_pipeline(tmp_path / "nope.yml")
    assert not isinstance(info.value, PipelineValidationError)
    assert "não encontrado" in str(info.value)


def test_execution_stages() -> None:
    pipeline = parse_pipeline(
        """
        name: x
        jobs:
          a: {steps: [{run: a}]}
          b: {needs: a, steps: [{run: b}]}
          c: {needs: [a], steps: [{run: c}]}
          d: {needs: [b, c], steps: [{run: d}]}
          e: {steps: [{run: e}]}
        """
    )
    stages = [[job.id for job in stage] for stage in pipeline.execution_stages()]
    assert stages == [["a", "e"], ["b", "c"], ["d"]]
