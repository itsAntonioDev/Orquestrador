"""Testes do ``LocalExecutor`` (subprocessos reais)."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from orquestrador.execution import RunContext
from orquestrador.executors import CommandRequest, CommandResult, LocalExecutor
from orquestrador.executors.base import EXIT_CANCELLED, EXIT_NOT_FOUND, EXIT_TIMEOUT
from orquestrador.executors.local import LocalJobSession
from orquestrador.executors.shells import resolve_shell, split_command
from tests.conftest import make_pipeline


class Collector:
    """Callback de saída que acumula as linhas recebidas."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __call__(self, stream: str, text: str) -> None:
        self.lines.append((stream, text))

    def texts(self, stream: str | None = None) -> list[str]:
        return [text for s, text in self.lines if stream is None or s == stream]


@pytest.fixture
def session(tmp_path: Path) -> Iterator[LocalJobSession]:
    job = make_pipeline({"j": {"steps": [{"run": "echo"}]}}).jobs["j"]
    with LocalExecutor().open_session(job, RunContext(workspace=tmp_path)) as opened:
        yield opened


def run(session: LocalJobSession, command: str, **kwargs: Any) -> tuple[CommandResult, Collector]:
    collector = Collector()
    kwargs.setdefault("shell", "python")
    result = session.run(CommandRequest(command=command, **kwargs), collector)
    return result, collector


def test_captures_stdout(session: LocalJobSession) -> None:
    result, out = run(session, "print('olá mundo')")
    assert result.ok
    assert result.exit_code == 0
    assert out.texts("stdout") == ["olá mundo"]


def test_captures_stderr_separately(session: LocalJobSession) -> None:
    result, out = run(session, "import sys\nprint('out')\nprint('err', file=sys.stderr)")
    assert result.exit_code == 0
    assert out.texts("stdout") == ["out"]
    assert out.texts("stderr") == ["err"]


def test_non_zero_exit_code(session: LocalJobSession) -> None:
    result, _ = run(session, "import sys; sys.exit(3)")
    assert result.exit_code == 3
    assert not result.ok


def test_output_is_streamed_in_order(session: LocalJobSession) -> None:
    _, out = run(session, "for i in range(20): print(i)")
    assert out.texts() == [str(i) for i in range(20)]


def test_default_shell_runs_echo(session: LocalJobSession) -> None:
    result, out = run(session, "echo hello", shell=None)
    assert result.exit_code == 0
    assert "hello" in [line.strip() for line in out.texts()]


def test_default_shell_propagates_exit_code(session: LocalJobSession) -> None:
    result, _ = run(session, "exit 4", shell=None)
    assert result.exit_code == 4


def test_env_vars_are_passed(session: LocalJobSession) -> None:
    _, out = run(session, "import os; print(os.environ['FOO'])", env={"FOO": "bar"})
    assert out.texts() == ["bar"]


def test_host_env_is_inherited(session: LocalJobSession) -> None:
    _, out = run(session, "import os; print('PATH' in os.environ)")
    assert out.texts() == ["True"]


@pytest.mark.skipif(os.name == "nt", reason="Python no Windows exige SYSTEMROOT no ambiente")
def test_host_env_not_inherited_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORQ_TEST_SECRET", "vazou")
    session = LocalJobSession(tmp_path, inherit_env=False, default_shell="sh")
    try:
        result, out = run(session, "import os; print(os.environ.get('ORQ_TEST_SECRET'))")
    finally:
        session.close()
    assert result.exit_code == 0
    assert out.texts() == ["None"]


def test_runs_in_workspace(session: LocalJobSession, tmp_path: Path) -> None:
    _, out = run(session, "import os; print(os.getcwd())")
    assert Path(out.texts()[0]).resolve() == tmp_path.resolve()


def test_working_directory(session: LocalJobSession, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    _, out = run(session, "import os; print(os.getcwd())", working_directory="sub")
    assert Path(out.texts()[0]).resolve() == (tmp_path / "sub").resolve()


def test_missing_working_directory(session: LocalJobSession) -> None:
    result, _ = run(session, "print(1)", working_directory="nao-existe")
    assert result.exit_code is None
    assert result.error is not None and "não encontrado" in result.error


def test_timeout_kills_process(session: LocalJobSession) -> None:
    started = time.monotonic()
    result, _ = run(session, "import time; print('start', flush=True); time.sleep(30)", timeout=0.5)
    assert result.timed_out
    assert result.exit_code == EXIT_TIMEOUT
    assert time.monotonic() - started < 10


def test_cancel_event_stops_process(session: LocalJobSession) -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    result, _ = run(session, "import time; time.sleep(30)", cancel_event=cancel)
    assert result.cancelled
    assert result.exit_code == EXIT_CANCELLED
    assert time.monotonic() - started < 10


def test_unknown_shell(session: LocalJobSession) -> None:
    result, _ = run(session, "echo", shell="fish-inexistente")
    assert result.exit_code is None
    assert result.error is not None and "shell desconhecido" in result.error


def test_missing_shell_binary(session: LocalJobSession) -> None:
    result, _ = run(session, "echo", shell="binario-que-nao-existe-xyz {0}")
    assert result.exit_code == EXIT_NOT_FOUND
    assert result.error is not None


def test_custom_shell_template(session: LocalJobSession) -> None:
    result, out = run(session, "print('custom')", shell=f'"{sys.executable}" -u {{0}}')
    assert result.exit_code == 0
    assert out.texts() == ["custom"]


def test_multiline_script(session: LocalJobSession) -> None:
    _, out = run(session, "x = 1\ny = 2\nprint(x + y)")
    assert out.texts() == ["3"]


@pytest.mark.skipif(os.name == "nt", reason="depende de sh/bash")
def test_posix_script_stops_on_first_error(session: LocalJobSession) -> None:
    result, out = run(session, "echo a\nfalse\necho b", shell=None)
    assert result.exit_code != 0
    assert out.texts() == ["a"]


def test_close_removes_scripts_dir(tmp_path: Path) -> None:
    session = LocalJobSession(tmp_path, inherit_env=True, default_shell="python")
    scripts = session.scripts_dir
    run(session, "print(1)")
    assert scripts.exists()
    session.close()
    assert not scripts.exists()


def test_open_session_creates_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "novo" / "workspace"
    job = make_pipeline({"j": {"steps": [{"run": "echo"}]}}).jobs["j"]
    with LocalExecutor().open_session(job, RunContext(workspace=workspace)):
        assert workspace.is_dir()


def test_resolve_shell_builtin_and_template() -> None:
    assert resolve_shell("bash").argv[-1] == "{0}"
    spec = resolve_shell("perl -w {0}", posix=True)
    assert spec.argv == ("perl", "-w", "{0}")


def test_split_command_windows_rules() -> None:
    assert split_command(r'"C:\Program Files\x.exe" -u {0}', posix=False) == [
        r"C:\Program Files\x.exe",
        "-u",
        "{0}",
    ]
