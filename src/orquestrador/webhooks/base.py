"""Contrato comum aos provedores de webhook."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orquestrador.projects import Provider
from orquestrador.webhooks.events import TriggerEvent, optional_str

Headers = Mapping[str, str]
Payload = dict[str, Any]


class WebhookError(Exception):
    """Erro base no processamento de webhooks."""


class EventIgnored(WebhookError):
    """Evento válido, mas que não deve disparar pipeline (ex.: branch removida).

    Attributes:
        reason: Motivo legível.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class InvalidPayload(WebhookError):
    """Payload sem campos obrigatórios ou em formato inesperado."""


def require_str(value: Any, field_name: str) -> str:
    """Garante uma string não vazia vinda do payload.

    Raises:
        InvalidPayload: Se o valor estiver ausente ou não for string.
    """
    text = optional_str(value)
    if text is None:
        raise InvalidPayload(f"campo obrigatório ausente no payload: {field_name}")
    return text


@dataclass(frozen=True)
class ProviderAdapter:
    """Operações específicas de um provedor, usadas pela rota genérica de webhooks.

    Attributes:
        provider: Provedor atendido.
        repository_name: Extrai o nome do repositório do payload.
        verify: Valida a autenticidade ``(segredo, corpo, cabeçalhos) -> bool``.
        event_name: Lê o tipo de evento dos cabeçalhos.
        delivery_id: Lê o ID da entrega dos cabeçalhos.
        parse_event: Converte ``(evento, payload, delivery_id)`` em ``TriggerEvent``.
        ping_events: Eventos de teste de conectividade (respondidos com ``pong``).
    """

    provider: Provider
    repository_name: Callable[[Payload], str | None]
    verify: Callable[[str, bytes, Headers], bool]
    event_name: Callable[[Headers], str | None]
    delivery_id: Callable[[Headers], str | None]
    parse_event: Callable[[str, Payload, str | None], TriggerEvent]
    ping_events: frozenset[str] = field(default_factory=frozenset)
