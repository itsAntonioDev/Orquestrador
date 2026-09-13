"""Testes do registro de projetos."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orquestrador.projects import (
    Project,
    ProjectRegistry,
    ProjectRegistryError,
    Provider,
    expand_env,
)


def project_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "api",
        "provider": "github",
        "repository": "acme/api",
        "secret": "s3cr3t",
    }
    data.update(overrides)
    return data


def test_load_from_file_with_env_expansion(write_file: Callable[..., Path]) -> None:
    path = write_file(
        """
        projects:
          - name: api
            provider: github
            repository: acme/api
            secret: ${GH_SECRET}
          - name: app
            provider: gitlab
            repository: grupo/app
            secret: ${GL_SECRET:-padrao}
            pipeline: ci/pipeline.yml
            clone_url: https://gitlab.com/grupo/app.git
        """,
        name="projects.yml",
    )
    registry = ProjectRegistry.from_file(path, environ={"GH_SECRET": "vindo-do-env"})

    assert len(registry) == 2
    api = registry.get("api")
    assert api is not None
    assert api.secret.get_secret_value() == "vindo-do-env"
    assert api.pipeline == ".orquestrador.yml"
    assert api.clone_url is None
    app = registry.get("app")
    assert app is not None
    assert app.secret.get_secret_value() == "padrao"
    assert app.pipeline == "ci/pipeline.yml"
    assert [project.name for project in registry] == ["api", "app"]


def test_missing_env_var(write_file: Callable[..., Path]) -> None:
    path = write_file(
        "projects:\n  - {name: a, provider: github, repository: a/b, secret: '${NOPE}'}",
        name="p.yml",
    )
    with pytest.raises(ProjectRegistryError, match="NOPE"):
        ProjectRegistry.from_file(path, environ={})


def test_expand_env_nested_structures() -> None:
    result = expand_env({"a": ["${X}", {"b": "pre-${X}-pos"}], "n": 1}, {"X": "1"})
    assert result == {"a": ["1", {"b": "pre-1-pos"}], "n": 1}


def test_find_is_case_insensitive() -> None:
    registry = ProjectRegistry([Project.model_validate(project_data(repository="Acme/API"))])
    assert registry.find(Provider.GITHUB, "acme/api") is not None
    assert registry.find("github", "/ACME/api/") is not None
    assert registry.find(Provider.GITLAB, "acme/api") is None
    assert registry.find(Provider.GITHUB, "acme/outro") is None
    assert registry.get("inexistente") is None


def test_same_repository_on_different_providers_is_allowed() -> None:
    registry = ProjectRegistry(
        [
            Project.model_validate(project_data()),
            Project.model_validate(project_data(name="api-gl", provider="gitlab")),
        ]
    )
    assert len(registry) == 2


def test_duplicate_names() -> None:
    project = Project.model_validate(project_data())
    other = Project.model_validate(project_data(repository="acme/outro"))
    with pytest.raises(ProjectRegistryError, match="projeto duplicado"):
        ProjectRegistry([project, other])


def test_duplicate_repositories() -> None:
    first = Project.model_validate(project_data())
    second = Project.model_validate(project_data(name="api2", repository="ACME/api"))
    with pytest.raises(ProjectRegistryError, match="mais de uma vez"):
        ProjectRegistry([first, second])


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"provider": "bitbucket"}, "provider"),
        ({"secret": ""}, "secret"),
        ({"name": "nome inválido"}, "name"),
        ({"repository": "  "}, "repository"),
        ({"pipeline": "../fora.yml"}, "pipeline"),
        ({"pipeline": "/etc/passwd"}, "pipeline"),
        ({"pipeline": "C:/pipeline.yml"}, "pipeline"),
        ({"extra": "campo"}, "extra"),
    ],
)
def test_invalid_projects(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ProjectRegistryError, match=fragment):
        ProjectRegistry.from_data({"projects": [project_data(**overrides)]}, environ={})


@pytest.mark.parametrize("data", [None, {}, {"projects": None}, {"projects": []}])
def test_empty_registry(data: Any) -> None:
    assert len(ProjectRegistry.from_data(data, environ={})) == 0


@pytest.mark.parametrize("data", [["lista"], {"projects": "texto"}])
def test_invalid_structure(data: Any) -> None:
    with pytest.raises(ProjectRegistryError):
        ProjectRegistry.from_data(data, environ={})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProjectRegistryError, match="não encontrado"):
        ProjectRegistry.from_file(tmp_path / "nao-existe.yml")


def test_invalid_yaml(write_file: Callable[..., Path]) -> None:
    path = write_file("projects: [unclosed", name="bad.yml")
    with pytest.raises(ProjectRegistryError, match="não foi possível ler"):
        ProjectRegistry.from_file(path)


def test_secret_is_not_exposed_in_repr() -> None:
    project = Project.model_validate(project_data())
    assert "s3cr3t" not in repr(project)
    assert "s3cr3t" not in project.model_dump_json()
