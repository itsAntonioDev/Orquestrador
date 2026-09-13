"""Testes dos comandos ``orquestrador db`` e ``orquestrador history``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from orquestrador.cli import app
from orquestrador.db import RunRepository
from orquestrador.db.migrations import head_revision
from orquestrador.execution import Status
from tests.test_db_repository import completed, make_request

cli = CliRunner()


def test_db_upgrade_and_current(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("ORQ_DATABASE_URL", database_url)

    before = cli.invoke(app, ["db", "current"])
    assert before.exit_code == 0, before.output
    assert "(nenhuma)" in before.output

    upgraded = cli.invoke(app, ["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    assert "banco atualizado para 'head'" in upgraded.output

    after = cli.invoke(app, ["db", "current"])
    assert f"aplicada:  {head_revision()}" in after.output


def test_history_lists_runs(
    monkeypatch: pytest.MonkeyPatch, database_url: str, repository: RunRepository
) -> None:
    repository.create_run(make_request("r1", "api", minutes=0))
    repository.finish_run(completed("r1", Status.SUCCESS, duration=2.5))
    repository.create_run(make_request("r2", "web", minutes=1))
    monkeypatch.setenv("ORQ_DATABASE_URL", database_url)

    result = cli.invoke(app, ["history"])
    assert result.exit_code == 0, result.output
    assert "api" in result.output and "web" in result.output
    assert "success" in result.output and "queued" in result.output
    assert "2 de 2 execuções" in result.output

    filtered = cli.invoke(app, ["history", "--project", "api", "--status", "success"])
    assert "1 de 1 execuções" in filtered.output


def test_history_empty(
    monkeypatch: pytest.MonkeyPatch, database_url: str, repository: RunRepository
) -> None:
    monkeypatch.setenv("ORQ_DATABASE_URL", database_url)
    result = cli.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "Nenhuma execução registrada" in result.output


def test_history_invalid_status(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("ORQ_DATABASE_URL", database_url)
    result = cli.invoke(app, ["history", "--status", "talvez"])
    assert result.exit_code == 2
    assert "use um de" in result.output


def test_history_without_migrations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ORQ_DATABASE_URL", f"sqlite:///{(tmp_path / 'vazio.db').as_posix()}")
    result = cli.invoke(app, ["history"])
    assert result.exit_code == 2
    assert "orquestrador db upgrade" in result.output
