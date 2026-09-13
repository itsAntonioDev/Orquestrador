"""Contrato dos canais de notificação."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from orquestrador.notifications.message import RunNotification


class NotificationError(Exception):
    """Falha ao entregar uma notificação."""


class Notifier(ABC):
    """Um canal de notificação (Slack, e-mail...)."""

    name: ClassVar[str] = "notifier"

    @abstractmethod
    def send(self, notification: RunNotification) -> None:
        """Entrega a notificação.

        Raises:
            NotificationError: Se a entrega falhar.
        """
