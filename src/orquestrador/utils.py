"""Utilitários genéricos."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def remove_tree(path: Path) -> None:
    """Remove um diretório recursivamente, inclusive arquivos somente-leitura.

    No Windows os objetos do ``.git`` são somente-leitura e ``shutil.rmtree``
    falharia sem ajustar as permissões. Falhas são registradas, não propagadas:
    limpeza de workspace não deve derrubar uma execução.

    Args:
        path: Diretório a remover (ignorado se não existir).
    """
    if not path.exists():
        return

    def make_writable_and_retry(func: Callable[[str], Any], target: str, _exc: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError:
        logger.warning("não foi possível remover %s", path, exc_info=True)
