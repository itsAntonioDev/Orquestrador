"""Decide se um evento corresponde aos gatilhos (``on``) de um pipeline."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import lru_cache

from orquestrador.pipeline.models import TriggerFilter
from orquestrador.webhooks.events import TriggerEvent


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Converte um glob de branch/tag em regex.

    ``*`` casa qualquer sequência sem ``/``; ``**`` casa qualquer sequência;
    ``?`` casa um caractere diferente de ``/``.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


def matches_patterns(value: str | None, patterns: Sequence[str]) -> bool:
    """Avalia uma lista de globs em ordem; padrões com ``!`` excluem.

    Exemplo: ``["release/**", "!release/beta"]`` aceita ``release/1.0`` e
    rejeita ``release/beta``.

    Args:
        value: Nome da branch/tag.
        patterns: Padrões avaliados em ordem (o último que casar decide).

    Returns:
        ``True`` se o valor for aceito.
    """
    if value is None:
        return False
    matched = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        body = pattern[1:] if negated else pattern
        if glob_to_regex(body).match(value):
            matched = not negated
    return matched


def event_matches_triggers(triggers: Mapping[str, TriggerFilter], event: TriggerEvent) -> bool:
    """Verifica se o evento deve disparar o pipeline.

    Regras:

    * Pipeline sem ``on`` aceita qualquer ``push`` e ``pull_request``.
    * ``push``: ``branches`` filtra pushes de branch e ``tags`` filtra pushes de
      tag. Com apenas um dos filtros, o outro tipo de push é recusado; sem
      filtros, ambos são aceitos.
    * ``pull_request``: ``branches`` filtra a branch de **destino**.

    Args:
        triggers: Gatilhos declarados no pipeline.
        event: Evento recebido.

    Returns:
        ``True`` se o pipeline deve rodar.
    """
    if not triggers:
        return True
    trigger = triggers.get(event.event)
    if trigger is None:
        return False

    if event.event == "push":
        if event.tag is not None:
            return (
                matches_patterns(event.tag, trigger.tags) if trigger.tags else not trigger.branches
            )
        if trigger.branches:
            return matches_patterns(event.branch, trigger.branches)
        return not trigger.tags

    if trigger.branches:
        return matches_patterns(event.base_branch, trigger.branches)
    return True
