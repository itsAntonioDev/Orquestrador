"""Testes da fábrica de executores e das opções de volume do pipeline."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import docker
import pytest
from typer.testing import CliRunner

from orquestrador.cli import app
from orquestrador.config import Settings
from orquestrador.executors import (
    DockerExecutor,
    ExecutorKind,
    LocalExecutor,
    build_executor,
    create_executor,
)
from orquestrador.executors.factory import workspace_path_mapper
from orquestrador.pipeline import PipelineValidationError, VolumeMount, parse_pipeline_data
from tests.fake_docker import FakeDockerClient, echo_script


def test_default_is_local_executor(tmp_path: Path) -> None:
    assert isinstance(create_executor(Settings(data_dir=tmp_path)), LocalExecutor)


def test_docker_executor_from_settings(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        executor="docker",
        docker_default_image="node:20",
        docker_pull_policy="never",
        docker_network="rede",
        docker_memory_limit="512m",
        docker_cpus=2,
        docker_allow_bind_mounts=True,
        docker_allowed_bind_paths=["/srv"],
        docker_host_workspaces_dir="/host/workspaces",
    )
    executor = create_executor(settings)
    assert isinstance(executor, DockerExecutor)
    assert executor.default_image == "node:20"
    assert executor.pull_policy == "never"
    assert executor.network == "rede"
    assert executor.memory_limit == "512m"
    assert executor.cpus == 2
    assert executor.allow_bind_mounts is True
    workspace = settings.workspaces_dir / "run42"
    assert executor.host_path_mapper(workspace) == "/host/workspaces/run42"


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORQ_EXECUTOR", "docker")
    monkeypatch.setenv("ORQ_DOCKER_ALLOWED_BIND_PATHS", '["/a", "/b"]')
    settings = Settings()
    assert settings.executor is ExecutorKind.DOCKER
    assert settings.docker_allowed_bind_paths == ["/a", "/b"]


def test_workspace_path_mapper_outside_root(tmp_path: Path) -> None:
    mapper = workspace_path_mapper(tmp_path / "workspaces", "/host")
    assert mapper(tmp_path / "workspaces" / "a" / "b") == "/host/a/b"
    outside = tmp_path / "outro"
    assert mapper(outside) == str(outside)


def test_build_executor_invalid_kind() -> None:
    with pytest.raises(ValueError):
        build_executor("kubernetes")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("cache:/cache", ("cache", "/cache", False, True)),
        ("./dados:/dados:ro", ("./dados", "/dados", True, False)),
        ("/srv/x:/x:rw", ("/srv/x", "/x", False, False)),
        (r"C:\dados:/dados", (r"C:\dados", "/dados", False, False)),
        ("sub/dir:/a/../b/", ("sub/dir", "/b", False, False)),
        ({"source": "v", "target": "/v", "read-only": True}, ("v", "/v", True, True)),
    ],
)
def test_volume_mount_parsing(spec: Any, expected: tuple[str, str, bool, bool]) -> None:
    volume = VolumeMount.model_validate(spec)
    assert (volume.source, volume.target, volume.read_only, volume.is_named) == expected


@pytest.mark.parametrize(
    ("volumes", "fragment"),
    [
        (["sem-destino"], "volume inválido"),
        (["origem:relativo"], "volume inválido"),
        (["a:/x:rwx"], "volume inválido"),
        (["a:/"], "não pode ser '/'"),
        (["a:/x", "b:/x/"], "destino de volume repetido"),
    ],
)
def test_invalid_volumes(volumes: list[str], fragment: str) -> None:
    with pytest.raises(PipelineValidationError) as info:
        parse_pipeline_data(
            {"name": "x", "jobs": {"j": {"volumes": volumes, "steps": [{"run": "a"}]}}}
        )
    assert any(fragment in error for error in info.value.errors)


def test_pipeline_image_is_default_for_jobs() -> None:
    pipeline = parse_pipeline_data(
        {
            "name": "x",
            "image": "python:3.12",
            "jobs": {
                "a": {"steps": [{"run": "a"}]},
                "b": {"image": "node:20", "steps": [{"run": "b"}]},
            },
        }
    )
    assert pipeline.jobs["a"].image == "python:3.12"
    assert pipeline.jobs["b"].image == "node:20"


def test_cli_run_with_docker_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_file: Callable[..., Path]
) -> None:
    client = FakeDockerClient(echo_script)
    monkeypatch.setattr(docker, "from_env", lambda: client)
    path = write_file("name: d\njobs:\n  j:\n    steps:\n      - run: dentro do container\n")

    result = CliRunner().invoke(
        app,
        [
            "run",
            str(path),
            "--executor",
            "docker",
            "--image",
            "alpine:3.20",
            "--workspace",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dentro do container" in result.output
    assert client.last_container.image == "alpine:3.20"


def test_cli_rejects_unknown_executor(write_file: Callable[..., Path]) -> None:
    path = write_file("name: d\njobs: {j: {steps: [{run: a}]}}")
    result = CliRunner().invoke(app, ["run", str(path), "--executor", "vm"])
    assert result.exit_code == 2
