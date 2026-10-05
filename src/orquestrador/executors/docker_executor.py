"""Executor Docker: cada job roda num container próprio e isolado.

Ciclo de vida de um job:

1. Garante a imagem (política de pull) e cria um container de longa duração
   (``tail -f /dev/null``) com o workspace montado em ``/workspace``.
2. Para cada step, envia o script via ``put_archive`` e o executa com
   ``docker exec``, transmitindo stdout/stderr linha a linha.
3. Timeout/cancelamento matam a árvore de processos do step *dentro* do
   container, preservando-o para steps seguintes (``if: always()``).
4. Ao fim do job o container é removido.

Requisitos da imagem: ``/bin/sh`` e ``cut`` (presentes em Debian, Alpine,
Ubuntu etc.; imagens distroless/scratch não são suportadas).
"""

from __future__ import annotations

import codecs
import io
import logging
import ntpath
import posixpath
import re
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal

from orquestrador.executors.base import (
    EXIT_CANCELLED,
    EXIT_TIMEOUT,
    CommandRequest,
    CommandResult,
    Executor,
    JobSession,
    OutputCallback,
    StreamName,
)
from orquestrador.executors.shells import build_argv, render_script, resolve_shell

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container

    from orquestrador.execution.context import RunContext
    from orquestrador.pipeline.models import Job

logger = logging.getLogger(__name__)

PullPolicy = Literal["always", "if-not-present", "never"]

#: Where the workspace is mounted inside the container.
CONTAINER_WORKSPACE = "/workspace"
#: Where step scripts are written inside the container.
SCRIPTS_DIR = "/tmp/orquestrador"

_POLL_INTERVAL = 0.05
_KILL_GRACE = 5.0
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

#: Recursively kill the process whose PID is in file ``$0`` and its descendants,
#: by traversing ``/proc`` (does not depend on ``pkill``/``procps``).
KILL_TREE_SCRIPT = (
    "kill_tree() { for p in /proc/[0-9]*; do "
    'ppid=$(cut -d" " -f4 "$p/stat" 2>/dev/null); '
    '[ "$ppid" = "$1" ] && kill_tree "${p#/proc/}"; '
    'done; kill -9 "$1" 2>/dev/null; }; '
    'pid=$(cat "$0" 2>/dev/null) && [ -n "$pid" ] && kill_tree "$pid"; true'
)

#: Write the step PID to a file and replace the shell with the actual command.
PID_WRAPPER_SCRIPT = 'echo $$ > "$1" && shift && exec "$@"'


class DockerExecutorError(Exception):
    """Falha de infraestrutura Docker (daemon, imagem, container, volumes)."""


class LineSplitter:
    """Converte fragmentos de bytes em linhas de texto UTF-8.

    Lida com linhas e caracteres multibyte divididos entre fragmentos.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        """Adiciona um fragmento e retorna as linhas completas."""
        self._buffer += self._decoder.decode(chunk)
        *lines, self._buffer = self._buffer.split("\n")
        return [line.removesuffix("\r") for line in lines]

    def flush(self) -> list[str]:
        """Retorna o conteúdo restante (última linha sem quebra)."""
        self._buffer += self._decoder.decode(b"", final=True)
        rest, self._buffer = self._buffer, ""
        return [rest.removesuffix("\r")] if rest else []


def container_name(run_id: str, job_id: str) -> str:
    """Gera um nome de container único e válido para o Docker."""
    raw = f"orq-{run_id}-{job_id}-{uuid.uuid4().hex[:6]}"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)


def normalize_host_path(path: str) -> PurePath:
    """Normaliza um caminho absoluto do host (POSIX ou Windows) sem acessar o disco."""
    if _WINDOWS_DRIVE.match(path):
        return PureWindowsPath(ntpath.normpath(path))
    return PurePosixPath(posixpath.normpath(path))


def is_absolute_host_path(path: str) -> bool:
    """Se a origem de um volume é um caminho absoluto do host."""
    return path.startswith("/") or bool(_WINDOWS_DRIVE.match(path))


def build_tar(files: dict[str, str]) -> bytes:
    """Empacota arquivos de texto num tar em memória (para ``put_archive``).

    Args:
        files: Mapeamento caminho relativo -> conteúdo.

    Returns:
        Bytes do arquivo tar.
    """
    buffer = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            info.mtime = now
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class DockerExecutor(Executor):
    """Executa cada job num container Docker isolado."""

    name = "docker"

    def __init__(
        self,
        client: DockerClient | None = None,
        *,
        default_image: str = "python:3.11-slim",
        pull_policy: PullPolicy = "if-not-present",
        network: str | None = None,
        memory_limit: str | None = None,
        cpus: float | None = None,
        allow_bind_mounts: bool = False,
        allowed_bind_paths: Sequence[str] = (),
        host_path_mapper: Callable[[Path], str] | None = None,
        default_shell: str = "sh",
    ) -> None:
        """Cria o executor.

        Args:
            client: Cliente Docker (padrão: ``docker.from_env()`` na primeira utilização).
            default_image: Imagem para jobs sem ``image``.
            pull_policy: ``always``, ``if-not-present`` ou ``never``.
            network: Rede Docker dos containers (``None`` = padrão do daemon).
            memory_limit: Limite de memória (ex.: ``"2g"``).
            cpus: Limite de CPUs (ex.: ``1.5``).
            allow_bind_mounts: Permite montar caminhos absolutos do host.
            allowed_bind_paths: Se não vazio, restringe os bind mounts a esses prefixos.
            host_path_mapper: Traduz caminhos locais para caminhos vistos pelo daemon
                (necessário quando o orquestrador roda num container usando o
                Docker do host).
            default_shell: Shell padrão dos steps dentro do container.
        """
        self._client = client
        self._client_lock = threading.Lock()
        self.default_image = default_image
        self.pull_policy = pull_policy
        self.network = network
        self.memory_limit = memory_limit
        self.cpus = cpus
        self.allow_bind_mounts = allow_bind_mounts
        self.allowed_bind_paths = [normalize_host_path(path) for path in allowed_bind_paths]
        self.host_path_mapper = host_path_mapper or (lambda path: str(path))
        self.default_shell = default_shell

    @property
    def client(self) -> DockerClient:
        """Cliente Docker, conectado sob demanda.

        Raises:
            DockerExecutorError: Se o daemon não estiver acessível.
        """
        with self._client_lock:
            if self._client is None:
                import docker
                from docker.errors import DockerException

                try:
                    self._client = docker.from_env()
                except DockerException as exc:
                    raise DockerExecutorError(
                        f"não foi possível conectar ao Docker: {exc}"
                    ) from exc
            return self._client

    def open_session(self, job: Job, context: RunContext) -> DockerJobSession:
        """Garante a imagem, cria o container do job e retorna a sessão."""
        from docker.errors import DockerException

        image = job.image or self.default_image
        workspace = Path(context.workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        volumes = self.volume_bindings(job, workspace)

        client = self.client
        try:
            self.ensure_image(image)
            options: dict[str, Any] = {
                "entrypoint": ["tail", "-f", "/dev/null"],
                "detach": True,
                "name": container_name(context.run_id, job.id),
                "labels": {
                    "orquestrador.run_id": context.run_id,
                    "orquestrador.job": job.id,
                },
                "working_dir": CONTAINER_WORKSPACE,
                "volumes": volumes,
            }
            if self.network:
                options["network"] = self.network
            if self.memory_limit:
                options["mem_limit"] = self.memory_limit
            if self.cpus:
                options["nano_cpus"] = int(self.cpus * 1_000_000_000)
            container = client.containers.run(image, **options)
        except DockerException as exc:
            raise DockerExecutorError(f"falha ao criar container ({image}): {exc}") from exc

        logger.info(
            "[%s] container %s criado para o job %s", context.run_id, container.name, job.id
        )
        return DockerJobSession(
            client, container, workspace=workspace, default_shell=self.default_shell
        )

    def ensure_image(self, image: str) -> None:
        """Aplica a política de pull para a imagem.

        Raises:
            DockerExecutorError: Com política ``never`` e imagem ausente.
        """
        from docker.errors import ImageNotFound
        from docker.utils import parse_repository_tag

        client = self.client
        if self.pull_policy != "always":
            try:
                client.images.get(image)
                return
            except ImageNotFound as exc:
                if self.pull_policy == "never":
                    raise DockerExecutorError(
                        f"imagem não encontrada localmente (pull desativado): {image}"
                    ) from exc
        repository, tag = parse_repository_tag(image)
        logger.info("baixando imagem %s", image)
        client.images.pull(repository, tag=tag or "latest")

    def volume_bindings(self, job: Job, workspace: Path) -> list[str]:
        """Monta a lista de binds ``origem:destino:modo`` do container.

        Raises:
            DockerExecutorError: Se algum volume violar as regras de segurança.
        """
        bindings = [f"{self.host_path_mapper(workspace)}:{CONTAINER_WORKSPACE}:rw"]
        for volume in job.volumes:
            if volume.target == CONTAINER_WORKSPACE:
                raise DockerExecutorError(f"o destino {CONTAINER_WORKSPACE} é reservado")
            mode = "ro" if volume.read_only else "rw"
            if volume.is_named:
                source = volume.source
            elif is_absolute_host_path(volume.source):
                source = self._checked_bind_source(volume.source)
            else:
                resolved = (workspace / volume.source).resolve()
                if not resolved.is_relative_to(workspace):
                    raise DockerExecutorError(
                        f"volume relativo aponta para fora do workspace: {volume.source}"
                    )
                resolved.mkdir(parents=True, exist_ok=True)
                source = self.host_path_mapper(resolved)
            bindings.append(f"{source}:{volume.target}:{mode}")
        return bindings

    def _checked_bind_source(self, source: str) -> str:
        if not self.allow_bind_mounts:
            raise DockerExecutorError(
                f"bind mount de caminho do host não permitido: {source} "
                "(habilite ORQ_DOCKER_ALLOW_BIND_MOUNTS)"
            )
        normalized = normalize_host_path(source)
        if self.allowed_bind_paths and not any(
            normalized.is_relative_to(allowed) for allowed in self.allowed_bind_paths
        ):
            raise DockerExecutorError(f"caminho do host fora da lista permitida: {source}")
        return str(normalized)


class DockerJobSession(JobSession):
    """Sessão de um job dentro de um container em execução."""

    def __init__(
        self,
        client: DockerClient,
        container: Container,
        *,
        workspace: Path,
        default_shell: str = "sh",
    ) -> None:
        """Cria a sessão.

        Args:
            client: Cliente Docker.
            container: Container do job, já em execução.
            workspace: Workspace local montado em ``/workspace``.
            default_shell: Shell padrão dos steps.
        """
        self.client = client
        self.container = container
        self.workspace = workspace
        self.default_shell = default_shell

    def run(self, request: CommandRequest, on_output: OutputCallback) -> CommandResult:
        """Envia o script do step ao container e o executa via ``docker exec``."""
        from docker.errors import DockerException

        try:
            spec = resolve_shell(request.shell or self.default_shell, posix=True)
        except ValueError as exc:
            return CommandResult(exit_code=None, error=str(exc))

        workdir = self._container_workdir(request.working_directory)
        if workdir is None:
            return CommandResult(
                exit_code=None,
                error=f"diretório de trabalho não encontrado: {request.working_directory}",
            )

        step_id = uuid.uuid4().hex[:12]
        script_name = f"step-{step_id}{spec.extension}"
        pid_file = f"{SCRIPTS_DIR}/step-{step_id}.pid"
        try:
            uploaded = self.container.put_archive(
                "/tmp",
                build_tar({f"orquestrador/{script_name}": render_script(request.command, spec)}),
            )
        except DockerException as exc:
            return CommandResult(
                exit_code=None, error=f"falha ao enviar o script ao container: {exc}"
            )
        if uploaded is False:
            return CommandResult(exit_code=None, error="falha ao enviar o script ao container")

        command = [
            "/bin/sh",
            "-c",
            PID_WRAPPER_SCRIPT,
            "orquestrador",
            pid_file,
            *build_argv(spec, f"{SCRIPTS_DIR}/{script_name}"),
        ]
        environment = {**request.env, "ORQ_WORKSPACE": CONTAINER_WORKSPACE}
        try:
            exec_id = self.client.api.exec_create(
                self.container.id,
                command,
                environment=environment,
                workdir=workdir,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
            )["Id"]
            stream = self.client.api.exec_start(exec_id, stream=True, demux=True)
        except DockerException as exc:
            return CommandResult(exit_code=None, error=f"falha ao executar no container: {exc}")

        finished = threading.Event()
        pump = threading.Thread(target=self._pump, args=(stream, on_output, finished), daemon=True)
        pump.start()

        deadline = time.monotonic() + request.timeout if request.timeout is not None else None
        timed_out = cancelled = False
        while not finished.wait(_POLL_INTERVAL):
            if request.cancel_event is not None and request.cancel_event.is_set():
                cancelled = True
            elif deadline is not None and time.monotonic() >= deadline:
                timed_out = True
            else:
                continue
            self._kill(pid_file)
            if not finished.wait(_KILL_GRACE):
                logger.warning("a saída do step não terminou após matar o processo")
            break
        pump.join(timeout=_KILL_GRACE)

        if timed_out:
            return CommandResult(exit_code=EXIT_TIMEOUT, timed_out=True)
        if cancelled:
            return CommandResult(exit_code=EXIT_CANCELLED, cancelled=True)
        try:
            exit_code = self.client.api.exec_inspect(exec_id).get("ExitCode")
        except DockerException as exc:
            return CommandResult(exit_code=None, error=f"falha ao consultar o resultado: {exc}")
        if exit_code is None:
            return CommandResult(exit_code=None, error="o Docker não informou o código de saída")
        return CommandResult(exit_code=int(exit_code))

    def _container_workdir(self, working_directory: str | None) -> str | None:
        """Traduz ``working-directory`` para o caminho no container (ou ``None`` se inválido)."""
        if not working_directory:
            return CONTAINER_WORKSPACE
        local = (self.workspace / working_directory).resolve()
        if not local.is_relative_to(self.workspace) or not local.is_dir():
            return None
        relative = local.relative_to(self.workspace).as_posix()
        return CONTAINER_WORKSPACE if relative == "." else f"{CONTAINER_WORKSPACE}/{relative}"

    @staticmethod
    def _pump(
        stream: Iterable[tuple[bytes | None, bytes | None]],
        on_output: OutputCallback,
        finished: threading.Event,
    ) -> None:
        """Consome o stream demultiplexado do ``exec`` e emite linhas."""
        splitters: dict[StreamName, LineSplitter] = {
            "stdout": LineSplitter(),
            "stderr": LineSplitter(),
        }
        try:
            for stdout_chunk, stderr_chunk in stream:
                for name, chunk in (("stdout", stdout_chunk), ("stderr", stderr_chunk)):
                    if chunk:
                        for line in splitters[name].feed(chunk):  # type: ignore[index]
                            on_output(name, line)  # type: ignore[arg-type]
        except Exception:
            logger.debug("stream do exec encerrado com erro", exc_info=True)
        finally:
            for stream_name, splitter in splitters.items():
                for line in splitter.flush():
                    on_output(stream_name, line)
            finished.set()

    def _kill(self, pid_file: str) -> None:
        """Mata a árvore de processos do step dentro do container."""
        from docker.errors import DockerException

        try:
            self.container.exec_run(["/bin/sh", "-c", KILL_TREE_SCRIPT, pid_file])
        except DockerException:
            logger.warning("falha ao interromper o step no container", exc_info=True)

    def close(self) -> None:
        """Remove o container do job (ignora se já não existir)."""
        from docker.errors import DockerException, NotFound

        try:
            self.container.remove(force=True)
        except NotFound:
            pass
        except DockerException:
            logger.warning("falha ao remover o container %s", self.container.id, exc_info=True)
