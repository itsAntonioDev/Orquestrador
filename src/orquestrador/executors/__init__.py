"""Executores: onde e como os comandos dos steps são executados."""

from orquestrador.executors.base import (
    CommandRequest,
    CommandResult,
    Executor,
    JobSession,
    OutputCallback,
)
from orquestrador.executors.docker_executor import DockerExecutor, DockerExecutorError
from orquestrador.executors.factory import ExecutorKind, build_executor, create_executor
from orquestrador.executors.local import LocalExecutor

__all__ = [
    "CommandRequest",
    "CommandResult",
    "DockerExecutor",
    "DockerExecutorError",
    "Executor",
    "ExecutorKind",
    "JobSession",
    "LocalExecutor",
    "OutputCallback",
    "build_executor",
    "create_executor",
]
