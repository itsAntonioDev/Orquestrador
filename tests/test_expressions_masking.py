"""Testes da interpolação ``${{ }}`` e do mascaramento de secrets."""

from __future__ import annotations

import pytest

from orquestrador.execution.cancellation import AnyCancelSignal
from orquestrador.execution.masking import MASK, SecretMasker
from orquestrador.pipeline import ExpressionError, interpolate, pipeline_secret_references
from orquestrador.pipeline.expressions import secret_references
from tests.conftest import make_pipeline

SECRETS = {"TOKEN": "tok-123456", "SENHA": "p@ss w0rd"}
ENV = {"REGIAO": "sa-east-1"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sem expressões", "sem expressões"),
        ("${{ secrets.TOKEN }}", "tok-123456"),
        ("Bearer ${{secrets.TOKEN}}", "Bearer tok-123456"),
        ("${{ env.REGIAO }}/${{ secrets.SENHA }}", "sa-east-1/p@ss w0rd"),
        ("${{ env.AUSENTE }}", ""),
        ("echo $HOME ${HOME} {{ nada }}", "echo $HOME ${HOME} {{ nada }}"),
    ],
)
def test_interpolate(text: str, expected: str) -> None:
    assert interpolate(text, secrets=SECRETS, env=ENV) == expected


def test_missing_secret() -> None:
    with pytest.raises(ExpressionError, match="secret não definido: OUTRO"):
        interpolate("${{ secrets.OUTRO }}", secrets=SECRETS, env=ENV)


@pytest.mark.parametrize(
    "text", ["${{ github.token }}", "${{ secrets['TOKEN'] }}", "${{ }}", "${{ secrets.1X }}"]
)
def test_unsupported_expressions(text: str) -> None:
    with pytest.raises(ExpressionError, match="expressão não suportada"):
        interpolate(text, secrets=SECRETS, env=ENV)


def test_secret_references() -> None:
    assert secret_references("${{ secrets.A }} ${{ env.B }} ${{secrets.C}}") == {"A", "C"}
    pipeline = make_pipeline(
        {
            "j": {
                "env": {"X": "${{ secrets.JOB }}"},
                "steps": [
                    {"run": "deploy ${{ secrets.RUN }}", "env": {"Y": "${{ secrets.STEP }}"}}
                ],
            }
        },
        env={"Z": "${{ secrets.GLOBAL }}"},
    )
    assert pipeline_secret_references(pipeline) == {"JOB", "RUN", "STEP", "GLOBAL"}


def test_masker_replaces_secret_values() -> None:
    masker = SecretMasker(["tok-123456", "abc"])
    assert masker.mask("token=tok-123456 e tok-123456") == f"token={MASK} e {MASK}"
    assert masker.mask("abc é curto demais para mascarar") == "abc é curto demais para mascarar"


def test_masker_multiline_and_longest_first() -> None:
    masker = SecretMasker(["-----BEGIN KEY-----\nlinha-secreta-1\n-----END KEY-----", "segredo"])
    assert masker.mask("linha-secreta-1") == MASK
    assert masker.mask("segredo-maior") == f"{MASK}-maior"
    masker = SecretMasker(["abcd", "abcdefgh"])
    assert masker.mask("abcdefgh") == MASK


def test_masker_without_secrets() -> None:
    masker = SecretMasker([])
    assert masker.mask("nada a esconder") == "nada a esconder"
    assert masker.mask("") == ""


def test_any_cancel_signal() -> None:
    import threading

    first, second = threading.Event(), threading.Event()
    signal = AnyCancelSignal(first, second)
    assert not signal.is_set()
    second.set()
    assert signal.is_set()
