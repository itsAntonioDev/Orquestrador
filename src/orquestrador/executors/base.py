"""Contrato dos executores de jobs.

Um ``Executor`` sabe *onde* os comandos rodam (máquina local, container
Docker, ...). Para cada job ele abre uma ``JobSession`` — que concentra o
estado daquele ambiente (diretório temporário, container em execução) — e a
sessão executa os comandos dos steps um a um.

Separar executor (fábrica, sem estado por job) de sessão (estado por job)
torna seguro rodar vários jobs em paralelo com a mesma instância de executor.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Literal, Self

if TYPE_CHECKING:
    from orquestrador.execution.context import RunContext
    from orquestrador.pipeline.models import Job

StreamName = Literal["stdout", "stderr", "system"]

#: Callback chamado a cada linha de saída: ``(stream, texto)``.
OutputCallback = Callable[[StreamName, str], None]

#: Código de saída quando o comando excede o tempo limite (convenção do ``timeout(1)``).
EXIT_TIMEOUT = 124
#: Código de saída quando o executável não é encontrado (convenção do shell).
EXIT_NOT_FOUND = 127
#: Código de saída quando a execução é cancelada (128 + SIGINT).
EXIT_CANCELLED = 130


@dataclass(frozen=True)
class CommandRequest:
    """Pedido de execução de um comando.

    Attributes:
        command: Script a executar.
        env: Variáveis de ambiente do pipeline/job/step já mescladas.
        shell: Shell a usar; ``None`` = padrão do executor.
        working_directory: Diretório relativo ao workspace.
        timeout: Tempo limite em segundos.
        cancel_event: Evento que, quando sinalizado, interrompe o comando.
    """

    command: str
    env: Mapping[str, str] = field(default_factory=dict)
    shell: str | None = None
    working_directory: str | None = None
    timeout: float | None = None
    cancel_event: threading.Event | None = None


@dataclass(frozen=True)
class CommandResult:
    """Resultado bruto de um comando.

    Attributes:
        exit_code: Código de saída (``None`` se nem chegou a executar).
        timed_out: Se o tempo limite foi atingido.
        cancelled: Se foi cancelado.
        error: Erro de infraestrutura (shell inexistente, diretório inválido...).
    """

    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Se o comando terminou normalmente com código zero."""
        return self.exit_code == 0 and not self.timed_out and not self.cancelled and not self.error


class JobSession(ABC):
    """Ambiente de execução de um único job. Use como context manager."""

    @abstractmethod
    def run(self, request: CommandRequest, on_output: OutputCallback) -> CommandResult:
        """Executa um comando e transmite a saída linha a linha.

        Args:
            request: O comando e seus parâmetros.
            on_output: Chamado para cada linha de saída.

        Returns:
            O resultado do comando.
        """

    def close(self) -> None:  # noqa: B027 - implementação padrão intencionalmente vazia
        """Libera os recursos da sessão (padrão: nada a fazer)."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class Executor(ABC):
    """Fábrica de sessões de execução.

    Attributes:
        name: Nome curto do executor (``local``, ``docker``...).
    """

    name: ClassVar[str] = "base"

    @abstractmethod
    def open_session(self, job: Job, context: RunContext) -> JobSession:
        """Prepara o ambiente de um job.

        Args:
            job: O job que será executado.
            context: Contexto da execução.

        Returns:
            Uma sessão pronta para executar os steps do job.

        Raises:
            Exception: Se o ambiente não puder ser preparado; o runner marca
                o job como falho.
        """
