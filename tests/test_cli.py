"""Testes da CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from orquestrador import __version__
from orquestrador.cli import app

cli = CliRunner()

SUCCESS_PIPELINE = """
name: demo-cli
jobs:
  build:
    steps:
      - name: saudacao
        shell: python
        run: |
          import os
          print("hello", os.environ.get("NAME", "ninguem"))
  test:
    needs: build
    steps:
      - shell: python
        run: print("testando")
"""

FAILING_PIPELINE = """
name: falha
jobs:
  build:
    steps:
      - name: quebra
        shell: python
        run: import sys; sys.exit(5)
"""


def test_version() -> None:
    result = cli.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_validate_valid_pipeline(write_file: Callable[..., Path]) -> None:
    path = write_file(SUCCESS_PIPELINE)
    result = cli.invoke(app, ["validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "Pipeline válido" in result.output
    assert "Estágio 1: build" in result.output
    assert "Estágio 2: test" in result.output


def test_validate_invalid_pipeline(write_file: Callable[..., Path]) -> None:
    path = write_file("name: x\njobs: {a: {steps: []}}")
    result = cli.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert "Pipeline inválido" in result.output
    assert "jobs.a.steps" in result.output


def test_run_success(write_file: Callable[..., Path], tmp_path: Path) -> None:
    path = write_file(SUCCESS_PIPELINE)
    result = cli.invoke(app, ["run", str(path), "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "hello ninguem" in result.output
    assert "testando" in result.output
    assert "SUCESSO" in result.output


def test_run_failure_exit_code(write_file: Callable[..., Path], tmp_path: Path) -> None:
    path = write_file(FAILING_PIPELINE)
    result = cli.invoke(app, ["run", str(path), "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "FALHOU" in result.output
    assert "código de saída 5" in result.output


def test_run_with_env_option(write_file: Callable[..., Path], tmp_path: Path) -> None:
    path = write_file(SUCCESS_PIPELINE)
    result = cli.invoke(app, ["run", str(path), "--workspace", str(tmp_path), "-e", "NAME=mundo"])
    assert result.exit_code == 0, result.output
    assert "hello mundo" in result.output


def test_run_quiet_hides_output(write_file: Callable[..., Path], tmp_path: Path) -> None:
    path = write_file(SUCCESS_PIPELINE)
    result = cli.invoke(app, ["run", str(path), "--workspace", str(tmp_path), "--quiet"])
    assert result.exit_code == 0
    assert "hello" not in result.output


def test_run_invalid_env_option(write_file: Callable[..., Path]) -> None:
    path = write_file(SUCCESS_PIPELINE)
    result = cli.invoke(app, ["run", str(path), "-e", "SEM_IGUAL"])
    assert result.exit_code == 2
    assert "NOME=valor" in result.output


def test_run_missing_file(tmp_path: Path) -> None:
    result = cli.invoke(app, ["run", str(tmp_path / "nao-existe.yml")])
    assert result.exit_code == 2
    assert "não encontrado" in result.output


def test_run_invalid_pipeline(write_file: Callable[..., Path]) -> None:
    path = write_file("name: x")
    result = cli.invoke(app, ["run", str(path)])
    assert result.exit_code == 2


def test_run_writes_json_report(write_file: Callable[..., Path], tmp_path: Path) -> None:
    path = write_file(FAILING_PIPELINE)
    report = tmp_path / "out" / "report.json"
    result = cli.invoke(
        app, ["run", str(path), "--workspace", str(tmp_path), "--report", str(report)]
    )
    assert result.exit_code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["status"] == "failure"
    assert data["jobs"]["build"]["steps"][0]["exit_code"] == 5
