"""Notificações no Slack via *Incoming Webhooks*."""

from __future__ import annotations

import httpx

from orquestrador.notifications.base import NotificationError, Notifier
from orquestrador.notifications.message import RunNotification, build_slack_payload


class SlackNotifier(Notifier):
    """Envia a notificação para um webhook do Slack."""

    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        *,
        channel: str | None = None,
        username: str | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        """Cria o notificador.

        Args:
            webhook_url: URL do webhook (nunca aparece em mensagens de erro).
            channel: Canal alternativo.
            username: Nome exibido.
            timeout: Tempo limite da requisição.
            client: Cliente HTTP (injetável em testes).
        """
        self.webhook_url = webhook_url
        self.channel = channel
        self.username = username
        self.timeout = timeout
        self.client = client

    def send(self, notification: RunNotification) -> None:
        """Faz o POST do payload; qualquer status fora de 2xx é erro."""
        payload = build_slack_payload(notification, channel=self.channel, username=self.username)
        try:
            if self.client is not None:
                response = self.client.post(self.webhook_url, json=payload, timeout=self.timeout)
            else:
                with httpx.Client() as client:
                    response = client.post(self.webhook_url, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise NotificationError(f"falha ao enviar para o Slack ({exc.__class__.__name__})") from exc
        if not response.is_success:
            raise NotificationError(
                f"o Slack respondeu {response.status_code}: {response.text[:200]}"
            )
