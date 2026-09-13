"""Testes do comando ``orquestrador serve``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

from orquestrador.cli import app

cli = CliRunner()


def test_serve_starts_uvicorn_with_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_file: Callable[..., Path]
) -> None:
    projects = write_file("projects: []\n", name="projects.yml")
    calls: dict[str, Any] = {}

    def fake_run(application: FastAPI, **kwargs: Any) -> None:
        calls["app"] = application
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = cli.invoke(
        app,
        [
            "serve",
            "--projects",
            str(projects),
            "--data-dir",
            str(tmp_path),
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 9001
    assert isinstance(calls["app"], FastAPI)
    assert calls["app"].state.settings.projects_file == projects
    calls["app"].state.dispatcher.shutdown()


def test_serve_fails_with_missing_projects_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("não deveria subir"))
    result = cli.invoke(app, ["serve", "--projects", str(tmp_path / "nao-existe.yml")])
    assert result.exit_code == 2
    assert "não encontrado" in result.output
