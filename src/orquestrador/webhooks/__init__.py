"""Recebimento de webhooks: validação de autenticidade e normalização de eventos."""

from orquestrador.projects import Provider
from orquestrador.webhooks import github, gitlab
from orquestrador.webhooks.base import (
    EventIgnored,
    InvalidPayload,
    ProviderAdapter,
    WebhookError,
)
from orquestrador.webhooks.events import TriggerEvent
from orquestrador.webhooks.matching import event_matches_triggers, matches_patterns

#: Adapter for each supported provider.
ADAPTERS: dict[Provider, ProviderAdapter] = {
    Provider.GITHUB: github.ADAPTER,
    Provider.GITLAB: gitlab.ADAPTER,
}

__all__ = [
    "ADAPTERS",
    "EventIgnored",
    "InvalidPayload",
    "ProviderAdapter",
    "TriggerEvent",
    "WebhookError",
    "event_matches_triggers",
    "matches_patterns",
]
