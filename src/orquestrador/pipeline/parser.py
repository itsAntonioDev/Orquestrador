"""Leitura de arquivos YAML de pipeline e conversão em modelos validados."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from orquestrador.pipeline.models import Pipeline


class PipelineError(Exception):
    """Erro genérico ao carregar um pipeline (ex.: arquivo inexistente)."""


class PipelineValidationError(PipelineError):
    """O conteúdo do pipeline é inválido (YAML malformado ou esquema violado).

    Attributes:
        errors: Lista de mensagens, uma por problema encontrado.
        source: Origem do pipeline (caminho do arquivo ou ``<string>``).
    """

    def __init__(self, errors: list[str], source: str = "<string>") -> None:
        self.errors = errors
        self.source = source
        details = "\n".join(f"  - {error}" for error in errors)
        super().__init__(f"pipeline inválido ({source}):\n{details}")


class _PipelineLoader(yaml.SafeLoader):
    """``SafeLoader`` com booleanos no estilo YAML 1.2 e detecção de chaves duplicadas.

    No YAML 1.1 (padrão do PyYAML) ``on``, ``off``, ``yes`` e ``no`` viram
    booleanos — o que transformaria a chave ``on:`` do pipeline em ``True``.
    Aqui apenas ``true``/``false`` são booleanos.
    """


_BOOL_TAG = "tag:yaml.org,2002:bool"
_MERGE_TAG = "tag:yaml.org,2002:merge"

_PipelineLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag != _BOOL_TAG]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_PipelineLoader.add_implicit_resolver(
    _BOOL_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_mapping_without_duplicates(
    loader: _PipelineLoader, node: MappingNode
) -> dict[Any, Any]:
    """Constrói um mapeamento YAML rejeitando chaves repetidas.

    Args:
        loader: Loader em uso.
        node: Nó de mapeamento a construir.

    Returns:
        O dicionário construído.

    Raises:
        ConstructorError: Se uma chave aparecer mais de uma vez.
    """
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        if key_node.tag == _MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise ConstructorError(
                "ao construir um mapeamento",
                node.start_mark,
                f"chave duplicada: {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_PipelineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)

#: Tradução das mensagens mais comuns do Pydantic.
_MESSAGES: dict[str, str] = {
    "missing": "campo obrigatório",
    "extra_forbidden": "campo desconhecido",
    "string_type": "deve ser um texto",
    "list_type": "deve ser uma lista",
    "dict_type": "deve ser um mapeamento",
    "model_type": "deve ser um mapeamento",
    "bool_parsing": "deve ser true ou false",
    "bool_type": "deve ser true ou false",
    "float_parsing": "deve ser um número",
    "float_type": "deve ser um número",
    "greater_than": "deve ser maior que {gt:g}",
    "too_short": "deve conter ao menos {min_length} item(ns)",
    "string_too_short": "não pode ser vazio",
}


def _format_validation_errors(exc: ValidationError) -> list[str]:
    """Converte um ``ValidationError`` em mensagens legíveis ``caminho: problema``.

    Args:
        exc: Erro levantado pelo Pydantic.

    Returns:
        Lista de mensagens formatadas.
    """
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        template = _MESSAGES.get(error["type"])
        if template is not None:
            try:
                message = template.format(**(error.get("ctx") or {}))
            except (KeyError, IndexError):
                message = error["msg"]
        else:
            message = error["msg"].removeprefix("Value error, ")
        messages.append(f"{location}: {message}" if location else message)
    return messages


def parse_pipeline_data(data: Any, source: str = "<string>") -> Pipeline:
    """Valida uma estrutura Python (já desserializada) como pipeline.

    Args:
        data: Estrutura vinda de YAML/JSON.
        source: Origem, usada nas mensagens de erro.

    Returns:
        O pipeline validado.

    Raises:
        PipelineValidationError: Se a estrutura não respeitar o esquema.
    """
    if not isinstance(data, dict):
        raise PipelineValidationError(["o documento deve ser um mapeamento (chave: valor)"], source)
    try:
        return Pipeline.model_validate(data)
    except ValidationError as exc:
        raise PipelineValidationError(_format_validation_errors(exc), source) from exc


def parse_pipeline(text: str, source: str = "<string>") -> Pipeline:
    """Faz o parse de um texto YAML e valida o pipeline.

    Args:
        text: Conteúdo YAML.
        source: Origem, usada nas mensagens de erro.

    Returns:
        O pipeline validado.

    Raises:
        PipelineValidationError: Se o YAML for malformado ou o pipeline inválido.
    """
    try:
        data = yaml.load(text, Loader=_PipelineLoader)
    except yaml.YAMLError as exc:
        raise PipelineValidationError([f"YAML inválido: {exc}"], source) from exc
    if data is None:
        raise PipelineValidationError(["o documento está vazio"], source)
    return parse_pipeline_data(data, source)


def load_pipeline(path: str | Path) -> Pipeline:
    """Carrega e valida um pipeline a partir de um arquivo.

    Args:
        path: Caminho do arquivo YAML.

    Returns:
        O pipeline validado.

    Raises:
        PipelineError: Se o arquivo não puder ser lido.
        PipelineValidationError: Se o conteúdo for inválido.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PipelineError(f"arquivo de pipeline não encontrado: {file_path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PipelineError(f"não foi possível ler {file_path}: {exc}") from exc
    return parse_pipeline(text, source=str(file_path))
