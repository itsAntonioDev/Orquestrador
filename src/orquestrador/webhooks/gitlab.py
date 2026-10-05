"""Webhooks do GitLab: validação do token secreto e normalização de eventos.

Referência: https://docs.gitlab.com/user/project/integrations/webhooks/
"""

from __future__ import annotations

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

TOKEN_HEADER = "X-Gitlab-Token"
EVENT_HEADER = "X-Gitlab-Event"
DELIVERY_HEADER = "X-Gitlab-Event-UUID"

#: "Null" SHA used by GitLab when a branch/tag is deleted.
NULL_SHA = "0" * 40

PUSH_EVENTS = frozenset({"Push Hook", "Tag Push Hook"})
MERGE_REQUEST_EVENT = "Merge Request Hook"
MERGE_REQUEST_ACTIONS = frozenset({"open", "reopen", "update"})


def verify_token(secret: str, token: str | None) -> bool:
    """Compara o ``X-Gitlab-Token`` com o segredo em tempo constante."""
    if not secret or not token:
        return False
    return hmac.compare_digest(secret.encode("utf-8"), token.encode("utf-8"))


def repository_name(payload: Payload) -> str | None:
    """Caminho completo do projeto (``grupo/subgrupo/repo``)."""
    return optional_str(dig(payload, "project", "path_with_namespace"))


def parse_event(event_name: str, payload: Payload, delivery_id: str | None = None) -> TriggerEvent:
    """Converte um evento do GitLab em ``TriggerEvent``.

    Raises:
        EventIgnored: Evento não suportado ou que não deve disparar pipeline.
        InvalidPayload: Payload sem os campos necessários.
    """
    if event_name in PUSH_EVENTS:
        return _parse_push(payload, delivery_id)
    if event_name == MERGE_REQUEST_EVENT:
        return _parse_merge_request(payload, delivery_id)
    raise EventIgnored(f"evento '{event_name}' não suportado")


def _parse_push(payload: Payload, delivery_id: str | None) -> TriggerEvent:
    ref = require_str(payload.get("ref"), "ref")
    commit = optional_str(payload.get("checkout_sha")) or optional_str(payload.get("after"))
    if commit is None or commit == NULL_SHA or payload.get("after") == NULL_SHA:
        raise EventIgnored("push de remoção de branch/tag")
    branch, tag = split_ref(ref)
    commits = payload.get("commits")
    message = (
        optional_str(commits[-1].get("message"))
        if isinstance(commits, list) and commits and isinstance(commits[-1], dict)
        else None
    )
    return TriggerEvent(
        provider=Provider.GITLAB,
        event="push",
        repository=require_str(repository_name(payload), "project.path_with_namespace"),
        clone_url=optional_str(dig(payload, "project", "git_http_url")),
        ref=ref,
        branch=branch,
        tag=tag,
        commit=commit,
        actor=optional_str(payload.get("user_username")) or optional_str(payload.get("user_name")),
        message=message,
        delivery_id=delivery_id,
    )


def _parse_merge_request(payload: Payload, delivery_id: str | None) -> TriggerEvent:
    attributes = payload.get("object_attributes")
    if not isinstance(attributes, dict):
        raise InvalidPayload("campo obrigatório ausente no payload: object_attributes")
    action = attributes.get("action")
    if action not in MERGE_REQUEST_ACTIONS:
        raise EventIgnored(f"ação de merge request '{action}' ignorada")
    if action == "update" and not attributes.get("oldrev"):
        raise EventIgnored("merge request atualizado sem novos commits")
    number = optional_int(attributes.get("iid"))
    return TriggerEvent(
        provider=Provider.GITLAB,
        event="pull_request",
        repository=require_str(repository_name(payload), "project.path_with_namespace"),
        clone_url=optional_str(dig(attributes, "source", "git_http_url"))
        or optional_str(dig(payload, "project", "git_http_url")),
        ref=f"refs/merge-requests/{number}/head" if number is not None else None,
        branch=optional_str(attributes.get("source_branch")),
        commit=require_str(
            dig(attributes, "last_commit", "id"), "object_attributes.last_commit.id"
        ),
        actor=optional_str(dig(payload, "user", "username")),
        base_branch=optional_str(attributes.get("target_branch")),
        pull_request=number,
        message=optional_str(attributes.get("title")),
        delivery_id=delivery_id,
    )


ADAPTER = ProviderAdapter(
    provider=Provider.GITLAB,
    repository_name=repository_name,
    verify=lambda secret, _body, headers: verify_token(secret, headers.get(TOKEN_HEADER)),
    event_name=lambda headers: headers.get(EVENT_HEADER),
    delivery_id=lambda headers: headers.get(DELIVERY_HEADER),
    parse_event=parse_event,
)
