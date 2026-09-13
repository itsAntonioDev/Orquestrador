"""Evento normalizado que dispara um pipeline, independente do provedor."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from orquestrador.projects import Provider

EventName = Literal["push", "pull_request"]


class TriggerEvent(BaseModel):
    """Um evento de repositório já normalizado (GitHub e GitLab viram este formato).

    Attributes:
        provider: Provedor de origem.
        event: ``push`` (branch ou tag) ou ``pull_request`` (inclui merge requests).
        repository: Nome completo do repositório.
        clone_url: URL de clone informada pelo provedor.
        ref: Referência git completa.
        branch: Branch do push, ou branch de origem do PR.
        tag: Tag do push, se for um push de tag.
        commit: SHA do commit a executar.
        actor: Usuário que causou o evento.
        base_branch: Branch de destino do PR.
        pull_request: Número do PR/MR.
        message: Mensagem do commit ou título do PR.
        delivery_id: ID da entrega do webhook (para rastreio).
    """

    provider: Provider
    event: EventName
    repository: str
    clone_url: str | None = None
    ref: str | None = None
    branch: str | None = None
    tag: str | None = None
    commit: str
    actor: str | None = None
    base_branch: str | None = None
    pull_request: int | None = None
    message: str | None = None
    delivery_id: str | None = None


def split_ref(ref: str) -> tuple[str | None, str | None]:
    """Extrai branch ou tag de uma referência git.

    Args:
        ref: Ex.: ``refs/heads/main`` ou ``refs/tags/v1.0``.

    Returns:
        Tupla ``(branch, tag)``; no máximo um dos dois é preenchido.
    """
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/"), None
    if ref.startswith("refs/tags/"):
        return None, ref.removeprefix("refs/tags/")
    return None, None


def dig(data: Any, *keys: str) -> Any:
    """Acessa chaves aninhadas com segurança; retorna ``None`` se algo faltar."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def optional_str(value: Any) -> str | None:
    """Retorna o valor se for uma string não vazia; caso contrário ``None``."""
    return value if isinstance(value, str) and value else None


def optional_int(value: Any) -> int | None:
    """Retorna o valor se for um inteiro (não booleano); caso contrário ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
