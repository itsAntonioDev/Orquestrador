"""Ganchos do ciclo de vida de uma execução (fila -> início -> fim).

Integrações que precisam acompanhar execuções inteiras — persistência,
notificações, métricas — implementam ``RunListener``. Falhas num listener
são registradas em log e nunca interrompem a execução.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from orquestrador.dispatch.dispatchers import Dispatcher
from orquestrador.dispatch.models import RunOutcome, RunRequest
from orquestrador.execution.events import RunObserver

logger = logging.getLogger(__name__)


class RunListener:
    """Recebe as fases de uma execução. Todos os métodos são opcionais."""

    def run_queued(self, request: RunRequest) -> None:
        """A execução foi aceita e será entregue ao dispatcher."""

    def run_dispatch_failed(self, request: RunRequest, error: Exception) -> None:
        """O dispatcher não conseguiu agendar a execução."""

    def run_started(self, request: RunRequest) -> None:
        """Um worker começou a processar a execução."""

    def run_observers(self, request: RunRequest) -> list[RunObserver]:
        """Observadores a anexar ao runner desta execução."""
        return []

    def run_finished(self, outcome: RunOutcome) -> None:
        """A execução terminou (com sucesso, falha, ``skipped`` ou erro)."""


def notify_listeners(listeners: Sequence[RunListener], method: str, *args: Any) -> None:
    """Chama ``method`` em cada listener, isolando exceções."""
    for listener in listeners:
        try:
            getattr(listener, method)(*args)
        except Exception:
            logger.exception("listener %r falhou em %s", listener, method)


def collect_observers(listeners: Sequence[RunListener], request: RunRequest) -> list[RunObserver]:
    """Reúne os observadores fornecidos pelos listeners, isolando exceções."""
    observers: list[RunObserver] = []
    for listener in listeners:
        try:
            observers.extend(listener.run_observers(request))
        except Exception:
            logger.exception("listener %r falhou ao criar observadores", listener)
    return observers


class ListeningDispatcher(Dispatcher):
    """Decora um dispatcher notificando listeners antes e depois de agendar."""

    def __init__(self, inner: Dispatcher, listeners: Sequence[RunListener]) -> None:
        """Cria o decorador.

        Args:
            inner: Dispatcher real (threads, RQ...).
            listeners: Listeners notificados.
        """
        self.inner = inner
        self.listeners = list(listeners)

    def dispatch(self, request: RunRequest) -> str:
        """Notifica ``run_queued``, agenda e, em caso de erro, ``run_dispatch_failed``."""
        notify_listeners(self.listeners, "run_queued", request)
        try:
            return self.inner.dispatch(request)
        except Exception as exc:
            notify_listeners(self.listeners, "run_dispatch_failed", request, exc)
            raise

    def stats(self) -> dict[str, Any]:
        """Métricas do dispatcher decorado."""
        return self.inner.stats()

    def shutdown(self, *, wait: bool = True) -> None:
        """Encerra o dispatcher decorado."""
        self.inner.shutdown(wait=wait)
