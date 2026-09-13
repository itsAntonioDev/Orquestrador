"""Registro de projetos: quais repositórios o orquestrador aceita e como validá-los.

Exemplo de ``projects.yml``::

    projects:
      - name: api
        provider: github
        repository: acme/api
        secret: ${GITHUB_WEBHOOK_SECRET}
        pipeline: .orquestrador.yml

Referências ``${VAR}`` (ou ``${VAR:-padrão}``) são expandidas a partir do
ambiente, para que segredos não precisem ficar no arquivo.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from orquestrador.notifications.models import NotificationConfig

PROJECT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"

_ENV_REFERENCE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


class Provider(StrEnum):
    """Provedores de repositório suportados."""

    GITHUB = "github"
    GITLAB = "gitlab"


class ProjectRegistryError(Exception):
    """Erro ao carregar ou montar o registro de projetos."""


class Project(BaseModel):
    """Um repositório configurado no orquestrador.

    Attributes:
        name: Identificador do projeto.
        provider: ``github`` ou ``gitlab``.
        repository: Nome completo do repositório (``org/repo`` ou ``grupo/sub/repo``).
        secret: Segredo do webhook (HMAC no GitHub, token no GitLab).
        pipeline: Caminho do arquivo de pipeline dentro do repositório.
        clone_url: URL de clone; se omitida, usa a URL informada no webhook.
        notifications: Canais (Slack, e-mail) avisados ao fim das execuções.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=PROJECT_NAME_PATTERN, max_length=100)
    provider: Provider
    repository: str = Field(min_length=1)
    secret: SecretStr
    pipeline: str = ".orquestrador.yml"
    clone_url: str | None = None
    notifications: NotificationConfig | None = None

    @field_validator("repository")
    @classmethod
    def _normalize_repository(cls, value: str) -> str:
        normalized = value.strip().strip("/")
        if not normalized:
            raise ValueError("repository não pode ser vazio")
        return normalized

    @field_validator("secret")
    @classmethod
    def _secret_not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("secret não pode ser vazio")
        return value

    @field_validator("pipeline")
    @classmethod
    def _pipeline_is_relative(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value.strip() or path.is_absolute() or ".." in path.parts or ":" in value:
            raise ValueError("pipeline deve ser um caminho relativo dentro do repositório")
        return str(path)


def expand_env(value: Any, environ: Mapping[str, str]) -> Any:
    """Expande ``${VAR}`` e ``${VAR:-padrão}`` recursivamente em strings, listas e dicts.

    Args:
        value: Estrutura a expandir.
        environ: Variáveis de ambiente disponíveis.

    Returns:
        A estrutura com as referências substituídas.

    Raises:
        ProjectRegistryError: Se uma variável sem padrão não estiver definida.
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name in environ:
                return environ[name]
            if match.group("default") is not None:
                return match.group("default")
            raise ProjectRegistryError(f"variável de ambiente não definida: {name}")

        return _ENV_REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {key: expand_env(item, environ) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item, environ) for item in value]
    return value


class ProjectRegistry:
    """Coleção imutável de projetos, indexada por nome e por repositório."""

    def __init__(self, projects: Iterable[Project] = ()) -> None:
        """Cria o registro.

        Args:
            projects: Projetos configurados.

        Raises:
            ProjectRegistryError: Se houver nomes ou repositórios duplicados.
        """
        self._by_name: dict[str, Project] = {}
        self._by_repository: dict[tuple[Provider, str], Project] = {}
        for project in projects:
            if project.name in self._by_name:
                raise ProjectRegistryError(f"projeto duplicado: {project.name}")
            key = (project.provider, project.repository.lower())
            if key in self._by_repository:
                raise ProjectRegistryError(
                    "repositório configurado mais de uma vez: "
                    f"{project.provider}:{project.repository}"
                )
            self._by_name[project.name] = project
            self._by_repository[key] = project

    @classmethod
    def from_data(
        cls,
        data: Any,
        *,
        environ: Mapping[str, str] | None = None,
        source: str = "<dados>",
    ) -> ProjectRegistry:
        """Monta o registro a partir de uma estrutura ``{"projects": [...]}``.

        Args:
            data: Estrutura desserializada.
            environ: Ambiente para expandir ``${VAR}`` (padrão: ``os.environ``).
            source: Origem, usada nas mensagens de erro.

        Returns:
            O registro montado.

        Raises:
            ProjectRegistryError: Se a estrutura ou algum projeto for inválido.
        """
        environment = os.environ if environ is None else environ
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ProjectRegistryError(f"{source}: esperado um mapeamento com a chave 'projects'")
        raw_projects = data.get("projects") or []
        if not isinstance(raw_projects, list):
            raise ProjectRegistryError(f"{source}: 'projects' deve ser uma lista")

        projects: list[Project] = []
        for position, raw in enumerate(raw_projects, start=1):
            try:
                projects.append(Project.model_validate(expand_env(raw, environment)))
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in error['loc']) or 'projeto'}: {error['msg']}"
                    for error in exc.errors()
                )
                raise ProjectRegistryError(
                    f"{source}: projeto #{position} inválido: {problems}"
                ) from exc
            except ProjectRegistryError as exc:
                raise ProjectRegistryError(f"{source}: projeto #{position}: {exc}") from exc
        return cls(projects)

    @classmethod
    def from_file(
        cls, path: str | Path, *, environ: Mapping[str, str] | None = None
    ) -> ProjectRegistry:
        """Carrega o registro de um arquivo YAML.

        Args:
            path: Caminho do arquivo.
            environ: Ambiente para expandir ``${VAR}``.

        Returns:
            O registro carregado.

        Raises:
            ProjectRegistryError: Se o arquivo não existir ou for inválido.
        """
        file_path = Path(path)
        try:
            data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProjectRegistryError(f"arquivo de projetos não encontrado: {file_path}") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise ProjectRegistryError(f"não foi possível ler {file_path}: {exc}") from exc
        return cls.from_data(data, environ=environ, source=str(file_path))

    def get(self, name: str) -> Project | None:
        """Busca um projeto pelo nome."""
        return self._by_name.get(name)

    def find(self, provider: Provider | str, repository: str) -> Project | None:
        """Busca um projeto pelo provedor e nome do repositório (sem diferenciar maiúsculas)."""
        key = (Provider(provider), repository.strip().strip("/").lower())
        return self._by_repository.get(key)

    def __iter__(self) -> Iterator[Project]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)
