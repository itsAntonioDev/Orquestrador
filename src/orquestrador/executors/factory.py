"""Criação do executor a partir da configuração."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from orquestrador.executors.base import Executor
from orquestrador.executors.local import LocalExecutor

if TYPE_CHECKING:
    from orquestrador.config import Settings


class ExecutorKind(StrEnum):
    """Tipos de executor disponíveis."""

    LOCAL = "local"
    DOCKER = "docker"


def build_executor(kind: ExecutorKind | str, **docker_options: Any) -> Executor:
    """Instancia um executor pelo tipo.

    Args:
        kind: ``local`` ou ``docker``.
        **docker_options: Argumentos repassados ao ``DockerExecutor``.

    Returns:
        O executor.

    Raises:
        ValueError: Se o tipo for desconhecido.
    """
    executor_kind = ExecutorKind(kind)
    if executor_kind is ExecutorKind.DOCKER:
        from orquestrador.executors.docker_executor import DockerExecutor

        return DockerExecutor(**docker_options)
    return LocalExecutor()


def workspace_path_mapper(local_root: Path, host_root: str) -> Any:
    """Cria a função que traduz workspaces locais para o caminho visto pelo daemon Docker.

    Quando o worker roda num container e fala com o Docker do host, o diretório
    ``/data/workspaces/<run>`` do worker corresponde a ``<host_root>/<run>`` no host.

    Args:
        local_root: Diretório de workspaces visto pelo orquestrador.
        host_root: O mesmo diretório visto pelo daemon Docker.

    Returns:
        Função ``Path -> str``.
    """
    resolved_root = local_root.resolve()

    def mapper(path: Path) -> str:
        try:
            relative = path.resolve().relative_to(resolved_root)
        except ValueError:
            return str(path)
        return str(PurePosixPath(host_root) / relative.as_posix())

    return mapper


def create_executor(settings: Settings) -> Executor:
    """Cria o executor configurado em ``settings`` (``ORQ_EXECUTOR``)."""
    if settings.executor is not ExecutorKind.DOCKER:
        return build_executor(ExecutorKind.LOCAL)
    mapper = (
        workspace_path_mapper(settings.workspaces_dir, settings.docker_host_workspaces_dir)
        if settings.docker_host_workspaces_dir
        else None
    )
    return build_executor(
        ExecutorKind.DOCKER,
        default_image=settings.docker_default_image,
        pull_policy=settings.docker_pull_policy,
        network=settings.docker_network,
        memory_limit=settings.docker_memory_limit,
        cpus=settings.docker_cpus,
        allow_bind_mounts=settings.docker_allow_bind_mounts,
        allowed_bind_paths=settings.docker_allowed_bind_paths,
        host_path_mapper=mapper,
    )
