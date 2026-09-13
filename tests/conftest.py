"""Fixtures e utilitários compartilhados pelos testes."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orquestrador.pipeline import Pipeline, parse_pipeline_data


def py(code: str, **extra: Any) -> dict[str, Any]:
    """Cria um step que roda código Python (portável entre Windows e Linux)."""
    return {"shell": "python", "run": textwrap.dedent(code).strip(), **extra}


def make_pipeline(jobs: dict[str, Any], **extra: Any) -> Pipeline:
    """Cria um pipeline validado a partir de um dicionário de jobs."""
    return parse_pipeline_data({"name": "teste", "jobs": jobs, **extra})


GitRepoFactory = Callable[..., tuple[Path, str]]

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git não instalado")


def run_git(cwd: Path, *args: str) -> str:
    """Executa git com identidade fixa (independente da config global)."""
    process = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Teste",
            "-c",
            "user.email=teste@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return process.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepoFactory:
    """Cria repositórios git locais com arquivos e um commit; retorna ``(caminho, sha)``."""

    def _create(files: dict[str, str], name: str = "repo") -> tuple[Path, str]:
        repo = tmp_path / name
        repo.mkdir(parents=True)
        run_git(repo, "init", "-q", "-b", "main")
        for relative, content in files.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(content), encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "commit inicial")
        return repo, run_git(repo, "rev-parse", "HEAD")

    return _create


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[..., Path]:
    """Grava um arquivo (com ``dedent``) em ``tmp_path`` e retorna o caminho."""

    def _write(content: str, name: str = "pipeline.yml") -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """URL de um SQLite temporário, exclusivo do teste."""
    return f"sqlite:///{(tmp_path / 'orquestrador-teste.db').as_posix()}"


@pytest.fixture
def database(database_url: str) -> Any:
    """Banco SQLite com as migrações reais aplicadas."""
    from orquestrador.db import Database
    from orquestrador.db.migrations import upgrade_database

    upgrade_database(database_url)
    db = Database(database_url)
    yield db
    db.dispose()


@pytest.fixture
def repository(database: Any) -> Any:
    """Repositório de execuções sobre o banco de teste."""
    from orquestrador.db import RunRepository

    return RunRepository(database)
