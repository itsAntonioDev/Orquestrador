"""Mascaramento de secrets na saída dos steps e nas mensagens de erro."""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Texto que substitui um secret.
MASK = "***"


class SecretMasker:
    """Substitui ocorrências de valores secretos por ``***``.

    Secrets multi-linha são mascarados linha a linha (a saída chega assim).
    Fragmentos com menos de ``min_length`` caracteres não são mascarados, para
    não corromper logs com substituições de valores triviais como ``1``.
    """

    def __init__(self, secrets: Iterable[str], *, min_length: int = 4) -> None:
        """Cria o mascarador.

        Args:
            secrets: Valores a ocultar.
            min_length: Tamanho mínimo de um fragmento para ser mascarado.
        """
        fragments = {
            line for value in secrets for line in value.splitlines() if len(line) >= min_length
        }
        ordered = sorted(fragments, key=len, reverse=True)
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def mask(self, text: str) -> str:
        """Retorna o texto com os secrets ocultados."""
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(MASK, text)
