"""Interpolação de expressões ``${{ secrets.NOME }}`` e ``${{ env.NOME }}``.

Aplicada aos valores de ``env`` e ao conteúdo de ``run`` imediatamente antes
de executar um step. Secrets só chegam ao step se forem referenciados
explicitamente (princípio do menor privilégio) e seus valores são mascarados
nos logs pelo ``SecretMasker``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orquestrador.pipeline.models import Pipeline

_EXPRESSION = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
_REFERENCE = re.compile(r"^(?P<scope>secrets|env)\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")


class ExpressionError(ValueError):
    """Expressão não suportada ou secret inexistente."""


def interpolate(text: str, *, secrets: Mapping[str, str], env: Mapping[str, str]) -> str:
    """Substitui as expressões ``${{ ... }}`` de um texto.

    Args:
        text: Texto com expressões.
        secrets: Secrets disponíveis para a execução.
        env: Variáveis de ambiente (antes da interpolação).

    Returns:
        O texto com as expressões substituídas (``env`` ausente vira vazio).

    Raises:
        ExpressionError: Se a expressão não for suportada ou o secret não existir.
    """
    if "${{" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        reference = _REFERENCE.match(body)
        if reference is None:
            raise ExpressionError(
                f"expressão não suportada: ${{{{ {body} }}}} (use secrets.NOME ou env.NOME)"
            )
        name = reference.group("name")
        if reference.group("scope") == "secrets":
            if name not in secrets:
                raise ExpressionError(f"secret não definido: {name}")
            return secrets[name]
        return env.get(name, "")

    return _EXPRESSION.sub(replace, text)


def secret_references(text: str) -> set[str]:
    """Nomes de secrets referenciados num texto."""
    names: set[str] = set()
    for match in _EXPRESSION.finditer(text):
        reference = _REFERENCE.match(match.group("body").strip())
        if reference is not None and reference.group("scope") == "secrets":
            names.add(reference.group("name"))
    return names


def pipeline_secret_references(pipeline: Pipeline) -> set[str]:
    """Todos os secrets referenciados por um pipeline (env e comandos)."""
    texts = list(pipeline.env.values())
    for job in pipeline.jobs.values():
        texts.extend(job.env.values())
        for step in job.steps:
            texts.extend(step.env.values())
            texts.append(step.run)
    names: set[str] = set()
    for text in texts:
        names |= secret_references(text)
    return names
