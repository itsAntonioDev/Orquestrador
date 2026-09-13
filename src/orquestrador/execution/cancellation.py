"""Sinais de cancelamento combináveis."""

from __future__ import annotations

from typing import Protocol


class CancelSignal(Protocol):
    """Qualquer objeto que informe se o cancelamento foi pedido (ex.: ``threading.Event``)."""

    def is_set(self) -> bool:
        """Se o cancelamento foi solicitado."""
        ...


class AnyCancelSignal:
    """Sinal ativo quando qualquer um dos sinais combinados estiver ativo.

    Usado em grupos paralelos: um step é interrompido se a execução inteira
    for cancelada *ou* se outro step do grupo falhar com ``fail-fast``.
    """

    def __init__(self, *signals: CancelSignal) -> None:
        """Combina os sinais informados."""
        self._signals = signals

    def is_set(self) -> bool:
        """Se algum dos sinais está ativo."""
        return any(signal.is_set() for signal in self._signals)
