"""Testes do ``DockerExecutor`` usando um cliente Docker falso."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import APIError, DockerException, NotFound

from orquestrador.execution import PipelineRunner, RunContext, Status
from orquestrador.executors import (
    CommandRequest,
    CommandResult,
    DockerExecutor,
    DockerExecutorError,
)
from orquestrador.executors.base import EXIT_CANCELLED, EXIT_TIMEOUT
from orquestrador.executors.docker_executor import (
    CONTAINER_WORKSPACE,
    SCRIPTS_DIR,
    DockerJobSession,
    LineSplitter,
    build_tar,
    container_name,
)
from orquestrador.pipeline import Job
from tests.conftest import make_pipeline
from tests.fake_docker import Chunk, FakeDockerClient, echo_script, hang_until_killed


def make_job(**fields: Any) -> Job:
    return make_pipeline({"build": {"steps": [{"run": "echo"}], **fields}}).jobs["build"]


def open_session(
    client: FakeDockerClient, tmp_path: Path, job: Job | None = None, **options: Any
) -> DockerJobSession:
    executor = DockerExecutor(client, **options)  # type: ignore[arg-type]
    context = RunContext(run_id="run1", workspace=tmp_path)
    return executor.open_session(job or make_job(), context)


class Collector:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, stream: str, text: str) -> None:
        with self.lock:
            self.lines.append((stream, text))

    def texts(self, stream: str) -> list[str]:
        return [text for name, text in self.lines if name == stream]


def run_step(
    session: DockerJobSession, command: str = "echo oi", **kwargs: Any
) -> tuple[CommandResult, Collector]:
    collector = Collector()
    return session.run(CommandRequest(command=command, **kwargs), collector), collector


class TestContainerLifecycle:
    def test_creates_container_with_workspace_mounted(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        session = open_session(client, tmp_path)

        container = client.last_container
        assert session.container is container
        assert container.image == "python:3.11-slim"
        options = container.options
        assert options["entrypoint"] == ["tail", "-f", "/dev/null"]
        assert options["detach"] is True
        assert options["working_dir"] == CONTAINER_WORKSPACE
        assert options["volumes"] == [f"{tmp_path.resolve()}:{CONTAINER_WORKSPACE}:rw"]
        assert options["labels"] == {"orquestrador.run_id": "run1", "orquestrador.job": "build"}
        assert options["name"].startswith("orq-run1-build-")
        assert "network" not in options and "mem_limit" not in options

    def test_job_image_and_resource_limits(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        open_session(
            client,
            tmp_path,
            make_job(image="alpine:3.20"),
            network="ci-net",
            memory_limit="1g",
            cpus=1.5,
        )
        container = client.last_container
        assert container.image == "alpine:3.20"
        assert container.options["network"] == "ci-net"
        assert container.options["mem_limit"] == "1g"
        assert container.options["nano_cpus"] == 1_500_000_000

    def test_close_removes_container(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        with open_session(client, tmp_path):
            pass
        assert client.last_container.removed

    @pytest.mark.parametrize("error", [NotFound("sumiu"), APIError("falhou")])
    def test_close_tolerates_errors(self, tmp_path: Path, error: Exception) -> None:
        client = FakeDockerClient()
        session = open_session(client, tmp_path)
        client.last_container.remove_error = error
        session.close()

    def test_container_creation_failure(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        client.containers.run_error = APIError("sem espaço")
        with pytest.raises(DockerExecutorError, match="falha ao criar container"):
            open_session(client, tmp_path)

    def test_lazy_client_connection_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> None:
            raise DockerException("daemon fora do ar")

        monkeypatch.setattr(docker, "from_env", broken)
        executor = DockerExecutor()
        with pytest.raises(DockerExecutorError, match="não foi possível conectar"):
            executor.open_session(make_job(), RunContext(workspace=tmp_path))

    def test_container_name_is_sanitized(self) -> None:
        name = container_name("run 1", "job/ção")
        assert name.startswith("orq-run-1-job--")
        assert all(char.isalnum() or char in "_.-" for char in name)


class TestImagePull:
    def test_if_not_present_skips_existing_image(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        open_session(client, tmp_path)
        assert client.images.pulled == []

    def test_if_not_present_pulls_missing_image(self, tmp_path: Path) -> None:
        client = FakeDockerClient(images=())
        open_session(client, tmp_path, make_job(image="node:20"))
        assert client.images.pulled == [("node", "20")]

    def test_untagged_image_pulls_latest(self, tmp_path: Path) -> None:
        client = FakeDockerClient(images=())
        open_session(client, tmp_path, make_job(image="ubuntu"))
        assert client.images.pulled == [("ubuntu", "latest")]

    def test_always_pulls(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        open_session(client, tmp_path, pull_policy="always")
        assert client.images.pulled == [("python", "3.11-slim")]

    def test_never_fails_when_missing(self, tmp_path: Path) -> None:
        client = FakeDockerClient(images=())
        with pytest.raises(DockerExecutorError, match="pull desativado"):
            open_session(client, tmp_path, pull_policy="never")


class TestVolumes:
    def bindings(self, tmp_path: Path, volumes: list[Any], **options: Any) -> list[str]:
        client = FakeDockerClient()
        open_session(client, tmp_path, make_job(volumes=volumes), **options)
        volumes_option: list[str] = client.last_container.options["volumes"]
        return volumes_option[1:]

    def test_named_volume(self, tmp_path: Path) -> None:
        assert self.bindings(tmp_path, ["pip-cache:/root/.cache/pip"]) == [
            "pip-cache:/root/.cache/pip:rw"
        ]

    def test_relative_bind_inside_workspace(self, tmp_path: Path) -> None:
        result = self.bindings(
            tmp_path, [{"source": "./dados", "target": "/dados", "read-only": True}]
        )
        assert result == [f"{(tmp_path / 'dados').resolve()}:/dados:ro"]
        assert (tmp_path / "dados").is_dir()

    def test_relative_bind_escaping_workspace(self, tmp_path: Path) -> None:
        with pytest.raises(DockerExecutorError, match="fora do workspace"):
            self.bindings(tmp_path, ["../fora:/fora"])

    def test_absolute_bind_denied_by_default(self, tmp_path: Path) -> None:
        with pytest.raises(DockerExecutorError, match="não permitido"):
            self.bindings(tmp_path, ["/var/run/docker.sock:/var/run/docker.sock"])

    def test_absolute_bind_allowed(self, tmp_path: Path) -> None:
        result = self.bindings(tmp_path, ["/srv/cache:/cache:ro"], allow_bind_mounts=True)
        assert result == ["/srv/cache:/cache:ro"]

    def test_absolute_bind_allowlist(self, tmp_path: Path) -> None:
        options = {"allow_bind_mounts": True, "allowed_bind_paths": ["/srv"]}
        assert self.bindings(tmp_path, ["/srv/a/../b:/b"], **options) == ["/srv/b:/b:rw"]
        with pytest.raises(DockerExecutorError, match="fora da lista"):
            self.bindings(tmp_path, ["/srv/../etc:/etc2"], **options)

    def test_workspace_target_is_reserved(self, tmp_path: Path) -> None:
        with pytest.raises(DockerExecutorError, match="reservado"):
            self.bindings(tmp_path, ["cache:/workspace"])

    def test_host_path_mapper(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        open_session(client, tmp_path, host_path_mapper=lambda path: "/host/ws")
        assert client.last_container.options["volumes"] == [f"/host/ws:{CONTAINER_WORKSPACE}:rw"]


class TestRunStep:
    def test_streams_output_and_returns_exit_code(self, tmp_path: Path) -> None:
        def behaviour(info: dict[str, Any], client: FakeDockerClient) -> Iterator[Chunk]:
            yield (b"hel", None)
            yield (b"lo\nwor", b"err")
            yield (None, b"o\r\n")
            yield (b"ld", None)
            info["exit_code"] = 3

        session = open_session(FakeDockerClient(behaviour), tmp_path)
        result, out = run_step(session)
        assert result.exit_code == 3
        assert out.texts("stdout") == ["hello", "world"]
        assert out.texts("stderr") == ["erro"]

    def test_utf8_characters_split_across_chunks(self, tmp_path: Path) -> None:
        encoded = "olá, ação\n".encode()

        def behaviour(info: dict[str, Any], client: FakeDockerClient) -> Iterator[Chunk]:
            for index in range(len(encoded)):
                yield (encoded[index : index + 1], None)

        session = open_session(FakeDockerClient(behaviour), tmp_path)
        _, out = run_step(session)
        assert out.texts("stdout") == ["olá, ação"]

    def test_script_upload_and_command(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        session = open_session(client, tmp_path)
        (tmp_path / "src").mkdir()

        result, _ = run_step(session, "echo a\necho b", env={"FOO": "bar"}, working_directory="src")

        assert result.ok
        info = client.last_exec
        cmd = info["cmd"]
        assert cmd[:3] == ["/bin/sh", "-c", 'echo $$ > "$1" && shift && exec "$@"']
        assert cmd[4].startswith(f"{SCRIPTS_DIR}/step-") and cmd[4].endswith(".pid")
        assert cmd[5:8] == ["sh", "-e", cmd[-1]]
        assert client.last_container.files[cmd[-1]] == "echo a\necho b\n"
        assert info["environment"] == {"FOO": "bar", "ORQ_WORKSPACE": CONTAINER_WORKSPACE}
        assert info["workdir"] == f"{CONTAINER_WORKSPACE}/src"
        assert info["tty"] is False

    def test_python_and_custom_shells(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        session = open_session(client, tmp_path)
        run_step(session, "print(1)", shell="python")
        assert client.last_exec["cmd"][5:7] == ["python", "-u"]
        assert client.last_exec["cmd"][-1].endswith(".py")
        run_step(session, "puts 1", shell="ruby {0}")
        assert client.last_exec["cmd"][5] == "ruby"

    def test_missing_working_directory(self, tmp_path: Path) -> None:
        session = open_session(FakeDockerClient(), tmp_path)
        result, _ = run_step(session, working_directory="nao-existe")
        assert result.error is not None and "não encontrado" in result.error

    def test_working_directory_cannot_escape(self, tmp_path: Path) -> None:
        session = open_session(FakeDockerClient(), tmp_path / "ws")
        result, _ = run_step(session, working_directory="..")
        assert result.error is not None

    def test_unknown_shell(self, tmp_path: Path) -> None:
        session = open_session(FakeDockerClient(), tmp_path)
        result, _ = run_step(session, shell="fish-que-nao-existe")
        assert result.error is not None and "shell desconhecido" in result.error

    def test_exec_create_failure(self, tmp_path: Path) -> None:
        client = FakeDockerClient()
        session = open_session(client, tmp_path)
        client.api.create_error = APIError("container parado")
        result, _ = run_step(session)
        assert result.exit_code is None
        assert result.error is not None and "falha ao executar" in result.error

    def test_timeout_kills_process_tree_in_container(self, tmp_path: Path) -> None:
        client = FakeDockerClient(hang_until_killed)
        session = open_session(client, tmp_path)
        started = time.monotonic()

        result, out = run_step(session, "sleep 60", timeout=0.3)

        assert time.monotonic() - started < 5
        assert result.timed_out
        assert result.exit_code == EXIT_TIMEOUT
        assert out.texts("stdout") == ["iniciou"]
        kill_command = client.last_container.exec_runs[0]
        assert kill_command[:2] == ["/bin/sh", "-c"]
        assert "kill_tree" in kill_command[2]
        assert kill_command[3] == client.last_exec["cmd"][4]

    def test_cancel(self, tmp_path: Path) -> None:
        client = FakeDockerClient(hang_until_killed)
        session = open_session(client, tmp_path)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        result, _ = run_step(session, "sleep 60", cancel_event=cancel)
        assert result.cancelled
        assert result.exit_code == EXIT_CANCELLED


def test_runner_with_docker_executor(tmp_path: Path) -> None:
    client = FakeDockerClient(echo_script)
    pipeline = make_pipeline(
        {
            "build": {
                "image": "alpine:3.20",
                "steps": [
                    {"name": "ok", "run": "compilando"},
                    {"name": "quebra", "run": "exit 2"},
                    {"name": "limpeza", "if": "always()", "run": "limpando"},
                ],
            }
        }
    )
    runner = PipelineRunner(DockerExecutor(client))  # type: ignore[arg-type]
    result = runner.run(pipeline, RunContext(workspace=tmp_path))

    job = result.jobs["build"]
    assert [step.status for step in job.steps] == [Status.SUCCESS, Status.FAILURE, Status.SUCCESS]
    assert job.steps[0].stdout == "compilando"
    assert job.steps[1].exit_code == 2
    assert job.steps[2].stdout == "limpando"
    assert client.last_container.image == "alpine:3.20"
    assert client.last_container.removed
    assert len(client.containers.created) == 1


def test_runner_reports_container_failure(tmp_path: Path) -> None:
    client = FakeDockerClient()
    client.containers.run_error = APIError("imagem corrompida")
    pipeline = make_pipeline({"build": {"steps": [{"run": "x"}]}})
    result = PipelineRunner(DockerExecutor(client)).run(  # type: ignore[arg-type]
        pipeline, RunContext(workspace=tmp_path)
    )
    assert result.jobs["build"].status == Status.FAILURE
    assert "falha ao preparar o ambiente do job" in (result.jobs["build"].error or "")


def test_line_splitter() -> None:
    splitter = LineSplitter()
    assert splitter.feed(b"a\nb") == ["a"]
    assert splitter.feed(b"c\r\n\n") == ["bc", ""]
    assert splitter.feed("é".encode()[:1]) == []
    assert splitter.flush() == ["\ufffd"]
    assert splitter.flush() == []


def test_build_tar_roundtrip() -> None:
    import io
    import tarfile

    data = build_tar({"dir/a.sh": "echo á"})
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.getmember("dir/a.sh")
        extracted = tar.extractfile(member)
        assert extracted is not None
        assert extracted.read().decode() == "echo á"
