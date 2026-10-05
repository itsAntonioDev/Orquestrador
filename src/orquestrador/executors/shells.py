"""Definições de shells usados para executar os scripts dos steps.

Assim como no GitHub Actions, o conteúdo de ``run`` é gravado num arquivo
de script e o shell é invocado com esse arquivo. Isso permite scripts
multi-linha e, com ``bash -eo pipefail``/``sh -e``, interrompe o script no
primeiro comando que falhar.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

#: Placeholder replaced with the script path.
SCRIPT_PLACEHOLDER = "{0}"


@dataclass(frozen=True)
class ShellSpec:
    """Como invocar um shell.

    Attributes:
        name: Nome do shell.
        argv: Linha de comando, contendo ``{0}`` onde vai o caminho do script.
        extension: Extensão do arquivo de script.
        line_ending: Terminador de linha usado ao gravar o script.
        preamble: Texto inserido no início do script.
    """

    name: str
    argv: tuple[str, ...]
    extension: str = ""
    line_ending: str = "\n"
    preamble: str = ""


BUILTIN_SHELLS: dict[str, ShellSpec] = {
    "bash": ShellSpec("bash", ("bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"), ".sh"),
    "sh": ShellSpec("sh", ("sh", "-e", "{0}"), ".sh"),
    "python": ShellSpec("python", ("python", "-u", "{0}"), ".py"),
    "pwsh": ShellSpec(
        "pwsh", ("pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", "{0}"), ".ps1"
    ),
    "powershell": ShellSpec(
        "powershell",
        (
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "{0}",
        ),
        ".ps1",
    ),
    "cmd": ShellSpec(
        "cmd", ("cmd", "/D", "/E:ON", "/V:OFF", "/C", "{0}"), ".cmd", "\r\n", "@echo off\n"
    ),
}


def default_local_shell() -> str:
    """Shell padrão para execução local: ``cmd`` no Windows, ``bash`` (ou ``sh``) no POSIX."""
    if os.name == "nt":
        return "cmd"
    return "bash" if shutil.which("bash") else "sh"


def split_command(command: str, *, posix: bool | None = None) -> list[str]:
    """Divide uma linha de comando em argumentos, respeitando aspas.

    Args:
        command: Linha de comando.
        posix: Regras POSIX (padrão: as do sistema atual). No modo Windows as
            barras invertidas são preservadas e as aspas externas removidas.

    Returns:
        Lista de argumentos.
    """
    use_posix = os.name != "nt" if posix is None else posix
    parts = shlex.split(command, posix=use_posix)
    if not use_posix:
        parts = [
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part
            for part in parts
        ]
    return parts


def resolve_shell(shell: str, *, posix: bool | None = None) -> ShellSpec:
    """Resolve o nome de um shell ou um template customizado.

    Args:
        shell: Nome embutido (``bash``, ``python``...) ou template com ``{0}``,
            ex.: ``"perl {0}"``.
        posix: Regras de divisão do template (padrão: as do sistema atual).

    Returns:
        A especificação do shell.

    Raises:
        ValueError: Se o shell não for conhecido nem um template válido.
    """
    key = shell.strip()
    if key in BUILTIN_SHELLS:
        return BUILTIN_SHELLS[key]
    if SCRIPT_PLACEHOLDER in key:
        argv = split_command(key, posix=posix)
        if argv:
            return ShellSpec(name=argv[0], argv=tuple(argv))
    known = ", ".join(sorted(BUILTIN_SHELLS))
    raise ValueError(
        f"shell desconhecido: {shell!r} (use um de: {known}; ou um template contendo {{0}})"
    )


def build_argv(spec: ShellSpec, script_path: str) -> list[str]:
    """Monta a linha de comando substituindo ``{0}`` pelo caminho do script."""
    return [part.replace(SCRIPT_PLACEHOLDER, script_path) for part in spec.argv]


def render_script(content: str, spec: ShellSpec) -> str:
    """Prepara o conteúdo do script: preâmbulo, quebra final e terminadores de linha."""
    text = spec.preamble + content.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    if spec.line_ending != "\n":
        text = text.replace("\n", spec.line_ending)
    return text


def write_script(directory: Path, stem: str, content: str, spec: ShellSpec) -> Path:
    """Grava o script de um step em disco.

    Args:
        directory: Diretório de destino.
        stem: Nome do arquivo sem extensão.
        content: Conteúdo de ``run``.
        spec: Shell que executará o script.

    Returns:
        Caminho do arquivo gravado.
    """
    path = directory / f"{stem}{spec.extension}"
    path.write_text(render_script(content, spec), encoding="utf-8", newline="")
    return path
