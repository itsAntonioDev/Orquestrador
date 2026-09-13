"""Cliente Docker falso para testar o ``DockerExecutor`` sem daemon."""

from __future__ import annotations

import io
import tarfile
import threading
from collections.abc import Callable, Iterator
from typing import Any

from docker.errors import ImageNotFound

Chunk = tuple[bytes | None, bytes | None]
#: Comportamento de um ``exec``: gerador que recebe (info do exec, cliente) e produz fragmentos.
ExecBehaviour = Callable[[dict[str, Any], "FakeDockerClient"], Iterator[Chunk]]


def silent(info: dict[str, Any], client: FakeDockerClient) -> Iterator[Chunk]:
    """Não produz saída e termina com código 0."""
    yield from ()


def echo_script(info: dict[str, Any], client: FakeDockerClient) -> Iterator[Chunk]:
    """Devolve o conteúdo do script como stdout; ``exit N`` no script define o código."""
    script_path = info["cmd"][-1]
    content = client.last_container.files[script_path]
    for line in content.splitlines():
        if line.startswith("exit "):
            info["exit_code"] = int(line.split()[1])
            return
        yield (f"{line}\n".encode(), None)


def hang_until_killed(info: dict[str, Any], client: FakeDockerClient) -> Iterator[Chunk]:
    """Bloqueia até o executor rodar o script de kill no container."""
    yield (b"iniciou\n", None)
    client.killed.wait(10)
    info["exit_code"] = 137


class FakeContainer:
    def __init__(self, client: FakeDockerClient, image: str, options: dict[str, Any]) -> None:
        self.client = client
        self.image = image
        self.options = options
        self.id = f"container-{len(client.containers.created)}"
        self.name = options.get("name", self.id)
        self.files: dict[str, str] = {}
        self.exec_runs: list[list[str]] = []
        self.removed = False
        self.remove_error: Exception | None = None

    def put_archive(self, path: str, data: bytes) -> bool:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                assert extracted is not None
                self.files[f"{path}/{member.name}"] = extracted.read().decode("utf-8")
                assert member.mode == 0o755
        return True

    def exec_run(self, cmd: list[str], **kwargs: Any) -> tuple[int, bytes]:
        self.exec_runs.append(cmd)
        self.client.killed.set()
        return 0, b""

    def remove(self, force: bool = False) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.removed = True


class FakeImages:
    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.pulled: list[tuple[str, str | None]] = []

    def get(self, name: str) -> object:
        if name not in self.present:
            raise ImageNotFound(f"imagem {name} ausente")
        return object()

    def pull(self, repository: str, tag: str | None = None) -> object:
        self.pulled.append((repository, tag))
        self.present.add(f"{repository}:{tag}")
        return object()


class FakeContainers:
    def __init__(self, client: FakeDockerClient) -> None:
        self.client = client
        self.created: list[FakeContainer] = []
        self.run_error: Exception | None = None

    def run(self, image: str, **options: Any) -> FakeContainer:
        if self.run_error is not None:
            raise self.run_error
        container = FakeContainer(self.client, image, options)
        self.created.append(container)
        return container


class FakeApi:
    def __init__(self, client: FakeDockerClient) -> None:
        self.client = client
        self.execs: dict[str, dict[str, Any]] = {}
        self.create_error: Exception | None = None

    def exec_create(self, container: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        if self.create_error is not None:
            raise self.create_error
        exec_id = f"exec-{len(self.execs)}"
        self.execs[exec_id] = {"container": container, "cmd": cmd, **kwargs}
        return {"Id": exec_id}

    def exec_start(self, exec_id: str, stream: bool, demux: bool) -> Iterator[Chunk]:
        assert stream and demux
        return self.client.behaviour(self.execs[exec_id], self.client)

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": self.execs[exec_id].get("exit_code", 0)}


class FakeDockerClient:
    """Imita o subconjunto do ``docker.DockerClient`` usado pelo executor."""

    def __init__(
        self,
        behaviour: ExecBehaviour = silent,
        images: tuple[str, ...] = ("python:3.11-slim", "alpine:3.20"),
    ) -> None:
        self.behaviour = behaviour
        self.images = FakeImages(set(images))
        self.containers = FakeContainers(self)
        self.api = FakeApi(self)
        self.killed = threading.Event()

    @property
    def last_container(self) -> FakeContainer:
        return self.containers.created[-1]

    @property
    def last_exec(self) -> dict[str, Any]:
        return list(self.api.execs.values())[-1]
