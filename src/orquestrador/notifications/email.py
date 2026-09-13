"""Notificações por e-mail (SMTP com STARTTLS ou TLS implícito)."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from orquestrador.notifications.base import NotificationError, Notifier
from orquestrador.notifications.message import RunNotification, render_html, render_text


@dataclass(frozen=True)
class SmtpSettings:
    """Servidor SMTP.

    Attributes:
        host: Endereço do servidor.
        port: Porta (587 para STARTTLS, 465 para TLS implícito).
        username: Usuário (``None`` = sem autenticação).
        password: Senha.
        sender: Remetente.
        starttls: Negocia TLS com STARTTLS.
        use_ssl: Conecta direto com TLS (``SMTP_SSL``).
        timeout: Tempo limite de conexão/envio.
    """

    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    sender: str = "orquestrador@localhost"
    starttls: bool = True
    use_ssl: bool = False
    timeout: float = 10.0


SmtpFactory = Callable[..., Any]


class EmailNotifier(Notifier):
    """Envia a notificação por e-mail em texto e HTML."""

    name = "email"

    def __init__(
        self,
        smtp: SmtpSettings,
        recipients: Sequence[str],
        *,
        subject_prefix: str = "[CI]",
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        """Cria o notificador.

        Args:
            smtp: Servidor SMTP.
            recipients: Destinatários.
            subject_prefix: Prefixo do assunto.
            smtp_factory: Construtor do cliente SMTP (injetável em testes).
        """
        self.smtp = smtp
        self.recipients = list(recipients)
        self.subject_prefix = subject_prefix
        self.smtp_factory = smtp_factory

    def build_message(self, notification: RunNotification) -> EmailMessage:
        """Monta o e-mail multipart (texto + HTML)."""
        message = EmailMessage()
        ref = f" ({notification.ref})" if notification.ref else ""
        message["Subject"] = (
            f"{self.subject_prefix} {notification.title}{ref} #{notification.run_id}".strip()
        )
        message["From"] = self.smtp.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(render_text(notification))
        message.add_alternative(render_html(notification), subtype="html")
        return message

    def send(self, notification: RunNotification) -> None:
        """Conecta ao SMTP, autentica (se configurado) e envia."""
        message = self.build_message(notification)
        factory = self.smtp_factory or (smtplib.SMTP_SSL if self.smtp.use_ssl else smtplib.SMTP)
        options: dict[str, Any] = {"timeout": self.smtp.timeout}
        if self.smtp.use_ssl:
            options["context"] = ssl.create_default_context()
        try:
            with factory(self.smtp.host, self.smtp.port, **options) as client:
                if self.smtp.starttls and not self.smtp.use_ssl:
                    client.starttls(context=ssl.create_default_context())
                if self.smtp.username:
                    client.login(self.smtp.username, self.smtp.password or "")
                client.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(
                f"falha ao enviar e-mail ({exc.__class__.__name__}: {exc})"
            ) from exc
