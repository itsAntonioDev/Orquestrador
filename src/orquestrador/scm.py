"""Checkout de repositórios git para o workspace de uma execução."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

#: Protocols allowed for git (blocks ``ext::`` and similar protocols).
ALLOWED_PROTOCOLS = "file:git:http:https:ssh"

_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_CREDENTIALS_IN_URL = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")


class CheckoutError(Exception):
    """Falha ao clonar o repositório ou posicionar no commit."""


def redact_credentials(text: str) -> str:
    """Oculta credenciais embutidas em URLs (``https://token@host`` -> ``https://***@host``)."""
    return _CREDENTIALS_IN_URL.sub(r"\g<scheme>***@", text)


def _run_git(args: Sequence[str], *, action: str, timeout: float) -> None:
    """Executa um comando git não interativo.

    Args:
        args: Linha de comando completa.
        action: Nome do subcomando, usado nas mensagens de erro.
        timeout: Tempo limite em segundos.

    Raises:
        CheckoutError: Se o git não existir, estourar o tempo ou falhar.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOLS,
    }
    try:
        process = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CheckoutError(f"executável do git não encontrado: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError(f"git excedeu o tempo limite de {timeout:g}s") from exc
    if process.returncode != 0:
        detail = redact_credentials(process.stderr.strip() or process.stdout.strip())
        raise CheckoutError(f"'git {action}' falhou: {detail}")


def checkout(
    clone_url: str,
    commit: str,
    destination: Path,
    *,
    git: str = "git",
    timeout: float = 600,
) -> None:
    """Clona o repositório em ``destination`` e posiciona no commit informado.

    Args:
        clone_url: URL (ou caminho local) do repositório.
        commit: SHA do commit (7 a 64 caracteres hexadecimais).
        destination: Diretório de destino (inexistente ou vazio).
        git: Executável do git.
        timeout: Tempo limite de cada comando git.

    Raises:
        CheckoutError: Se os parâmetros forem inválidos ou o git falhar.
    """
    if not _COMMIT_PATTERN.match(commit):
        raise CheckoutError(f"SHA de commit inválido: {commit!r}")
    if not clone_url or clone_url.startswith("-"):
        raise CheckoutError("URL de clone inválida")
    if destination.exists() and any(destination.iterdir()):
        raise CheckoutError(f"diretório de destino não está vazio: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    _run_git(
        [git, "clone", "--quiet", "--no-checkout", "--", clone_url, str(destination)],
        action="clone",
        timeout=timeout,
    )
    _run_git(
        [git, "-C", str(destination), "checkout", "--quiet", "--force", commit],
        action="checkout",
        timeout=timeout,
    )
