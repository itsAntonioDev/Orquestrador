"""Executor local: roda os steps como subprocessos na própria máquina."""

from __future__ import annotations

import contextlib
import locale
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from orquestrador.executors.base import (
    EXIT_CANCELLED,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    CommandRequest,
    CommandResult,
    Executor,
    JobSession,
    OutputCallback,
    StreamName,
)
from orquestrador.executors.shells import (
    build_argv,
    default_local_shell,
    resolve_shell,
    write_script,
)

if TYPE_CHECKING:
    from orquestrador.execution.context import RunContext
    from orquestrador.pipeline.models import Job

IS_WINDOWS = os.name == "nt"

#: Intervalo de verificação de timeout/cancelamento enquanto o processo roda.
_POLL_INTERVAL = 0.05


class LocalExecutor(Executor):
    """Executa steps diretamente no host via ``subprocess``.

    Não há isolamento: use apenas com pipelines confiáveis ou para
    desenvolvimento. Para isolamento, use o ``DockerExecutor``.
    """

    name = "local"

    def __init__(self, *, inherit_env: bool = True, default_shell: str | None = None) -> None:
        """Cria o executor.

        Args:
            inherit_env: Se os processos herdam as variáveis de ambiente do host.
            default_shell: Shell padrão (``None`` = ``cmd`` no Windows, ``bash``/``sh`` no POSIX).
        """
        self.inherit_env = inherit_env
        self.default_shell = default_shell or default_local_shell()

    def open_session(self, job: Job, context: RunContext) -> LocalJobSession:
        """Garante que o workspace existe e abre a sessão do job."""
        workspace = Path(context.workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return LocalJobSession(
            workspace, inherit_env=self.inherit_env, default_shell=self.default_shell
        )


class LocalJobSession(JobSession):
    """Sessão local: guarda os scripts dos steps num diretório temporário."""

    def __init__(self, workspace: Path, *, inherit_env: bool, default_shell: str) -> None:
        """Cria a sessão.

        Args:
            workspace: Diretório onde os comandos rodam.
            inherit_env: Se herda as variáveis de ambiente do host.
            default_shell: Shell usado quando o step não define um.
        """
        self.workspace = workspace
        self.inherit_env = inherit_env
        self.default_shell = default_shell
        self._scripts = tempfile.TemporaryDirectory(prefix="orquestrador-")

    @property
    def scripts_dir(self) -> Path:
        """Diretório temporário onde os scripts são gravados."""
        return Path(self._scripts.name)

    def run(self, request: CommandRequest, on_output: OutputCallback) -> CommandResult:
        """Grava o script do step e o executa com o shell escolhido."""
        try:
            spec = resolve_shell(request.shell or self.default_shell)
        except ValueError as exc:
            return CommandResult(exit_code=None, error=str(exc))

        cwd = self.workspace
        if request.working_directory:
            cwd = (self.workspace / request.working_directory).resolve()
            if not cwd.is_dir():
                return CommandResult(
                    exit_code=None,
                    error=f"diretório de trabalho não encontrado: {request.working_directory}",
                )

        script = write_script(
            self.scripts_dir, f"step-{uuid.uuid4().hex[:8]}", request.command, spec
        )
        argv = build_argv(spec, str(script))
        if argv[0] == "python":
            argv[0] = sys.executable

        env = {**os.environ, **request.env} if self.inherit_env else dict(request.env)
        return run_process(
            argv,
            cwd=cwd,
            env=env,
            timeout=request.timeout,
            cancel_event=request.cancel_event,
            on_output=on_output,
        )

    def close(self) -> None:
        """Remove o diretório temporário de scripts."""
        self._scripts.cleanup()


def _decode(raw: bytes) -> str:
    """Decodifica uma linha em UTF-8, caindo para a codificação local se necessário."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    return text.rstrip("\r\n")


def _pump(
    pipe: IO[bytes], stream: StreamName, on_output: OutputCallback, lock: threading.Lock
) -> None:
    """Lê um pipe linha a linha e repassa ao callback (roda numa thread)."""
    try:
        for raw in iter(pipe.readline, b""):
            text = _decode(raw)
            with lock:
                on_output(stream, text)
    except (OSError, ValueError):
        pass
    finally:
        pipe.close()


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Mata o processo e todos os seus descendentes."""
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with contextlib.suppress(OSError):
        process.kill()
    process.wait()


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None,
    cancel_event: threading.Event | None,
    on_output: OutputCallback,
) -> CommandResult:
    """Executa um processo transmitindo stdout/stderr em tempo real.

    O processo é criado num grupo/sessão próprio para que timeout e
    cancelamento matem também os processos filhos.

    Args:
        argv: Linha de comando.
        cwd: Diretório de trabalho.
        env: Ambiente completo do processo.
        timeout: Tempo limite em segundos.
        cancel_event: Evento de cancelamento.
        on_output: Callback de saída.

    Returns:
        O resultado do processo.
    """
    popen_kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
    except FileNotFoundError:
        return CommandResult(
            exit_code=EXIT_NOT_FOUND, error=f"executável não encontrado: {argv[0]}"
        )
    except OSError as exc:
        return CommandResult(exit_code=EXIT_NOT_FOUND, error=f"falha ao iniciar o processo: {exc}")

    assert process.stdout is not None and process.stderr is not None
    lock = threading.Lock()
    readers = [
        threading.Thread(
            target=_pump, args=(process.stdout, "stdout", on_output, lock), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(process.stderr, "stderr", on_output, lock), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout if timeout is not None else None
    timed_out = cancelled = False
    while True:
        try:
            process.wait(timeout=_POLL_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            elif deadline is not None and time.monotonic() >= deadline:
                timed_out = True
            else:
                continue
            _kill_process_tree(process)
            break

    for reader in readers:
        reader.join(timeout=5)

    exit_code = process.returncode
    if timed_out:
        exit_code = EXIT_TIMEOUT
    elif cancelled:
        exit_code = EXIT_CANCELLED
    return CommandResult(exit_code=exit_code, timed_out=timed_out, cancelled=cancelled)
