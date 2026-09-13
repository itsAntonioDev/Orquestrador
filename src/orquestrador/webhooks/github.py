"""Webhooks do GitHub: validação HMAC-SHA256 e normalização de eventos.

Referência: https://docs.github.com/webhooks/using-webhooks/validating-webhook-deliveries
"""

from __future__ import annotations

import hashlib
import hmac

from orquestrador.projects import Provider
from orquestrador.webhooks.base import (
    EventIgnored,
    InvalidPayload,
    Payload,
    ProviderAdapter,
    require_str,
)
from orquestrador.webhooks.events import TriggerEvent, dig, optional_int, optional_str, split_ref

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

#: Ações de pull request que representam código novo a validar.
PULL_REQUEST_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def compute_signature(secret: str, body: bytes) -> str:
    """Calcula o cabeçalho ``X-Hub-Signature-256`` esperado para um corpo."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Verifica a assinatura em tempo constante.

    Args:
        secret: Segredo configurado no webhook.
        body: Corpo bruto da requisição (exatamente como recebido).
        signature: Valor do cabeçalho ``X-Hub-Signature-256``.

    Returns:
        ``True`` se a assinatura for válida.
    """
    if not secret or not signature:
        return False
    return hmac.compare_digest(compute_signature(secret, body), signature.strip())


def repository_name(payload: Payload) -> str | None:
    """Nome completo do repositório (``org/repo``)."""
    return optional_str(dig(payload, "repository", "full_name"))


def parse_event(event_name: str, payload: Payload, delivery_id: str | None = None) -> TriggerEvent:
    """Converte um evento do GitHub em ``TriggerEvent``.

    Raises:
        EventIgnored: Evento não suportado ou que não deve disparar pipeline.
        InvalidPayload: Payload sem os campos necessários.
    """
    if event_name == "push":
        return _parse_push(payload, delivery_id)
    if event_name == "pull_request":
        return _parse_pull_request(payload, delivery_id)
    raise EventIgnored(f"evento '{event_name}' não suportado")


def _parse_push(payload: Payload, delivery_id: str | None) -> TriggerEvent:
    if payload.get("deleted"):
        raise EventIgnored("push de remoção de branch/tag")
    ref = require_str(payload.get("ref"), "ref")
    branch, tag = split_ref(ref)
    return TriggerEvent(
        provider=Provider.GITHUB,
        event="push",
        repository=require_str(repository_name(payload), "repository.full_name"),
        clone_url=optional_str(dig(payload, "repository", "clone_url")),
        ref=ref,
        branch=branch,
        tag=tag,
        commit=require_str(payload.get("after"), "after"),
        actor=optional_str(dig(payload, "sender", "login"))
        or optional_str(dig(payload, "pusher", "name")),
        message=optional_str(dig(payload, "head_commit", "message")),
        delivery_id=delivery_id,
    )


def _parse_pull_request(payload: Payload, delivery_id: str | None) -> TriggerEvent:
    action = payload.get("action")
    if action not in PULL_REQUEST_ACTIONS:
        raise EventIgnored(f"ação de pull request '{action}' ignorada")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise InvalidPayload("campo obrigatório ausente no payload: pull_request")
    number = optional_int(payload.get("number")) or optional_int(pull_request.get("number"))
    return TriggerEvent(
        provider=Provider.GITHUB,
        event="pull_request",
        repository=require_str(repository_name(payload), "repository.full_name"),
        clone_url=optional_str(dig(pull_request, "head", "repo", "clone_url"))
        or optional_str(dig(payload, "repository", "clone_url")),
        ref=f"refs/pull/{number}/head" if number is not None else None,
        branch=optional_str(dig(pull_request, "head", "ref")),
        commit=require_str(dig(pull_request, "head", "sha"), "pull_request.head.sha"),
        actor=optional_str(dig(payload, "sender", "login")),
        base_branch=optional_str(dig(pull_request, "base", "ref")),
        pull_request=number,
        message=optional_str(pull_request.get("title")),
        delivery_id=delivery_id,
    )


ADAPTER = ProviderAdapter(
    provider=Provider.GITHUB,
    repository_name=repository_name,
    verify=lambda secret, body, headers: verify_signature(
        secret, body, headers.get(SIGNATURE_HEADER)
    ),
    event_name=lambda headers: headers.get(EVENT_HEADER),
    delivery_id=lambda headers: headers.get(DELIVERY_HEADER),
    parse_event=parse_event,
    ping_events=frozenset({"ping"}),
)
