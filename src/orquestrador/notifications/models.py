"""Configuração de notificações de um projeto (seção ``notifications`` do projects.yml)."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotifyWhen(StrEnum):
    """Desfechos que podem gerar notificação."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    ERROR = "error"


class SlackConfig(BaseModel):
    """Canal Slack via *Incoming Webhook*.

    Attributes:
        webhook_url: URL do webhook (tratada como segredo).
        channel: Canal que sobrescreve o padrão do webhook (opcional).
        username: Nome exibido pelo bot (opcional).
    """

    model_config = ConfigDict(extra="forbid")

    webhook_url: SecretStr
    channel: str | None = None
    username: str | None = None

    @field_validator("webhook_url")
    @classmethod
    def _https_only(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("https://"):
            raise ValueError("webhook_url deve usar https://")
        return value


class EmailConfig(BaseModel):
    """Destinatários de e-mail (o servidor SMTP vem de ``ORQ_SMTP_*``).

    Attributes:
        to: Destinatários.
        subject_prefix: Prefixo do assunto.
    """

    model_config = ConfigDict(extra="forbid")

    to: list[str] = Field(min_length=1)
    subject_prefix: str = "[CI]"

    @field_validator("to")
    @classmethod
    def _valid_addresses(cls, value: list[str]) -> list[str]:
        invalid = [address for address in value if not _EMAIL.match(address)]
        if invalid:
            raise ValueError(f"e-mail inválido: {', '.join(invalid)}")
        return value


class NotificationConfig(BaseModel):
    """Quando e para onde notificar.

    Attributes:
        when: Desfechos que geram notificação (padrão: ``failure`` e ``error``).
        slack: Configuração do Slack.
        email: Configuração de e-mail.
    """

    model_config = ConfigDict(extra="forbid")

    when: list[NotifyWhen] = Field(default_factory=lambda: [NotifyWhen.FAILURE, NotifyWhen.ERROR])
    slack: SlackConfig | None = None
    email: EmailConfig | None = None

    def should_notify(self, status: str) -> bool:
        """Se o desfecho informado deve gerar notificação."""
        return status in {item.value for item in self.when}
