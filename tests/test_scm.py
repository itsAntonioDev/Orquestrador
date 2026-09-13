"""Testes do checkout git."""

from __future__ import annotations

from pathlib import Path

import pytest

from orquestrador.scm import CheckoutError, checkout, redact_credentials
from orquestrador.utils import remove_tree
from tests.conftest import GitRepoFactory, requires_git, run_git

pytestmark = requires_git


def test_checkout_specific_commit(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, first_sha = git_repo({"app.txt": "v1"})
    (repo / "app.txt").write_text("v2", encoding="utf-8")
    run_git(repo, "commit", "-q", "-am", "segunda versão")

    destination = tmp_path / "workspace"
    checkout(str(repo), first_sha, destination)

    assert (destination / "app.txt").read_text(encoding="utf-8") == "v1"
    assert run_git(destination, "rev-parse", "HEAD") == first_sha


def test_checkout_with_short_sha(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({"a.txt": "a"})
    checkout(str(repo), sha[:8], tmp_path / "ws")
    assert (tmp_path / "ws" / "a.txt").exists()


def test_unknown_commit(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, _ = git_repo({"a.txt": "a"})
    with pytest.raises(CheckoutError, match="git checkout"):
        checkout(str(repo), "f" * 40, tmp_path / "ws")


def test_unknown_repository(tmp_path: Path) -> None:
    with pytest.raises(CheckoutError, match="git clone"):
        checkout(str(tmp_path / "nao-existe"), "a" * 40, tmp_path / "ws")


@pytest.mark.parametrize("commit", ["", "main", "--upload-pack=touch x", "zzzzzzz", "a" * 65])
def test_invalid_commit_values(commit: str, tmp_path: Path) -> None:
    with pytest.raises(CheckoutError, match="SHA de commit inválido"):
        checkout("https://example.com/repo.git", commit, tmp_path / "ws")


def test_option_injection_in_url(tmp_path: Path) -> None:
    with pytest.raises(CheckoutError, match="URL de clone inválida"):
        checkout("--upload-pack=touch /tmp/x", "a" * 40, tmp_path / "ws")


def test_non_empty_destination(tmp_path: Path) -> None:
    destination = tmp_path / "ws"
    destination.mkdir()
    (destination / "arquivo").write_text("x", encoding="utf-8")
    with pytest.raises(CheckoutError, match="não está vazio"):
        checkout("https://example.com/repo.git", "a" * 40, destination)


def test_missing_git_executable(tmp_path: Path) -> None:
    with pytest.raises(CheckoutError, match="não encontrado"):
        checkout("repo", "a" * 40, tmp_path / "ws", git="git-que-nao-existe-xyz")


def test_disallowed_protocol(tmp_path: Path) -> None:
    with pytest.raises(CheckoutError):
        checkout("ext::sh -c touch% /tmp/pwned", "a" * 40, tmp_path / "ws")


def test_redact_credentials() -> None:
    text = "fatal: https://user:token123@github.com/acme/api.git não encontrado"
    assert redact_credentials(text) == "fatal: https://***@github.com/acme/api.git não encontrado"
    assert redact_credentials("sem credenciais") == "sem credenciais"


def test_remove_tree_handles_git_readonly_files(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({"a.txt": "a"})
    destination = tmp_path / "ws"
    checkout(str(repo), sha, destination)
    remove_tree(destination)
    assert not destination.exists()
    remove_tree(destination)  # não existe mais: não deve falhar
