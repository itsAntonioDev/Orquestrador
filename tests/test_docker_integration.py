"""Testes de integração com um daemon Docker real (pulados se indisponível)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from orquestrador.execution import PipelineRunner, RunContext, Status
from orquestrador.executors import DockerExecutor
from tests.conftest import make_pipeline

IMAGE = "alpine:3.20"


def _docker_client() -> object | None:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return client
    except Exception:
        return None


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(_docker_client() is None, reason="daemon Docker indisponível"),
]


@pytest.fixture(scope="module")
def executor() -> Iterator[DockerExecutor]:
    yield DockerExecutor(default_image=IMAGE)


def test_real_container_pipeline(executor: DockerExecutor, tmp_path: Path) -> None:
    (tmp_path / "entrada.txt").write_text("do host", encoding="utf-8")
    pipeline = make_pipeline(
        {
            "build": {
                "env": {"NOME": "container"},
                "steps": [
                    {"name": "le", "run": 'cat entrada.txt; echo "ola $NOME"; pwd'},
                    {"name": "escreve", "run": "echo gerado > saida.txt"},
                    {"name": "falha", "run": "echo antes; exit 7", "continue-on-error": True},
                    {"name": "timeout", "run": "sleep 60", "timeout": 1, "continue-on-error": True},
                    {"name": "depois do timeout", "run": "echo container vivo"},
                ],
            }
        }
    )
    result = PipelineRunner(executor).run(pipeline, RunContext(workspace=tmp_path))
    steps = result.jobs["build"].steps

    assert steps[0].stdout.splitlines() == ["do host", "ola container", "/workspace"]
    assert (tmp_path / "saida.txt").read_text().strip() == "gerado"
    assert steps[2].exit_code == 7
    assert steps[3].error is not None and "tempo limite" in steps[3].error
    assert steps[4].stdout == "container vivo"
    assert result.status == Status.SUCCESS
