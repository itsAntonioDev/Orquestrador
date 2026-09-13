"""Testes do ``RunService`` com checkout git real e execução local."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orquestrador.config import Settings
from orquestrador.dispatch import RunRequest, RunService
from orquestrador.execution import JobResult, RunObserver, Status
from orquestrador.pipeline import Job
from orquestrador.projects import Project, ProjectRegistry, Provider
from orquestrador.webhooks import TriggerEvent
from tests.conftest import GitRepoFactory, requires_git

pytestmark = requires_git

PIPELINE = """
name: ci
on:
  push:
    branches: [main]
jobs:
  build:
    steps:
      - name: le arquivo
        shell: python
        run: |
          import os, pathlib
          print(pathlib.Path("app.txt").read_text(), os.environ["ORQ_BRANCH"])
"""

FAILING_PIPELINE = """
name: ci
jobs:
  build:
    steps:
      - shell: python
        run: import sys; sys.exit(3)
"""


def make_service(tmp_path: Path, clone_url: str | None, **project_fields: Any) -> RunService:
    settings = Settings(data_dir=tmp_path / "data", keep_workspaces=False)
    project = Project(
        name="app",
        provider=Provider.GITHUB,
        repository="acme/app",
        secret="segredo",
        clone_url=clone_url,
        **project_fields,
    )
    return RunService(settings, ProjectRegistry([project]))


def make_request(
    commit: str, *, branch: str = "main", clone_url: str | None = None, project: str = "app"
) -> RunRequest:
    return RunRequest(
        project=project,
        trigger=TriggerEvent(
            provider=Provider.GITHUB,
            event="push",
            repository="acme/app",
            ref=f"refs/heads/{branch}",
            branch=branch,
            commit=commit,
            clone_url=clone_url,
        ),
    )


def read_report(tmp_path: Path, run_id: str) -> dict[str, Any]:
    report = tmp_path / "data" / "runs" / f"{run_id}.json"
    data: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
    return data


def test_successful_run(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "hello"})
    service = make_service(tmp_path, clone_url=str(repo))
    request = make_request(sha)

    outcome = service.execute(request)

    assert outcome.status == "completed", outcome.reason
    assert outcome.result is not None
    assert outcome.result.status == Status.SUCCESS
    assert outcome.result.run_id == request.run_id
    assert outcome.result.jobs["build"].steps[0].stdout == "hello main"
    assert read_report(tmp_path, request.run_id)["result"]["status"] == "success"
    assert not (tmp_path / "data" / "workspaces" / request.run_id).exists()


def test_failing_pipeline_is_completed_with_failure(
    git_repo: GitRepoFactory, tmp_path: Path
) -> None:
    repo, sha = git_repo({".orquestrador.yml": FAILING_PIPELINE})
    outcome = make_service(tmp_path, clone_url=str(repo)).execute(make_request(sha))
    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.status == Status.FAILURE


def test_skipped_when_trigger_does_not_match(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "x"})
    request = make_request(sha, branch="dev")
    outcome = make_service(tmp_path, clone_url=str(repo)).execute(request)
    assert outcome.status == "skipped"
    assert outcome.result is None
    assert "gatilhos" in (outcome.reason or "")
    assert read_report(tmp_path, request.run_id)["status"] == "skipped"


def test_custom_pipeline_path(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({"ci/pipeline.yml": PIPELINE, "app.txt": "custom"})
    service = make_service(tmp_path, clone_url=str(repo), pipeline="ci/pipeline.yml")
    outcome = service.execute(make_request(sha))
    assert outcome.result is not None
    assert outcome.result.jobs["build"].steps[0].stdout == "custom main"


def test_missing_pipeline_file(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({"README.md": "sem pipeline"})
    outcome = make_service(tmp_path, clone_url=str(repo)).execute(make_request(sha))
    assert outcome.status == "error"
    assert outcome.reason == "pipeline não encontrado no repositório: .orquestrador.yml"


def test_invalid_pipeline(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": "name: sem-jobs\n"})
    outcome = make_service(tmp_path, clone_url=str(repo)).execute(make_request(sha))
    assert outcome.status == "error"
    assert outcome.reason == "pipeline inválido: jobs: campo obrigatório"


def test_checkout_failure(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, _ = git_repo({".orquestrador.yml": PIPELINE})
    request = make_request("f" * 40)
    outcome = make_service(tmp_path, clone_url=str(repo)).execute(request)
    assert outcome.status == "error"
    assert (outcome.reason or "").startswith("falha no checkout")
    assert not (tmp_path / "data" / "workspaces" / request.run_id).exists()


def test_unknown_project(tmp_path: Path) -> None:
    request = make_request("a" * 40, project="fantasma")
    outcome = make_service(tmp_path, clone_url=None).execute(request)
    assert outcome.status == "error"
    assert outcome.reason == "projeto 'fantasma' não configurado"
    assert read_report(tmp_path, request.run_id)["status"] == "error"


def test_missing_clone_url(tmp_path: Path) -> None:
    outcome = make_service(tmp_path, clone_url=None).execute(make_request("a" * 40))
    assert outcome.status == "error"
    assert "URL de clone" in (outcome.reason or "")


def test_uses_clone_url_from_event(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "evento"})
    service = make_service(tmp_path, clone_url=None)
    outcome = service.execute(make_request(sha, clone_url=str(repo)))
    assert outcome.status == "completed", outcome.reason


def test_keep_workspaces(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "x"})
    service = make_service(tmp_path, clone_url=str(repo))
    service.settings = Settings(data_dir=tmp_path / "data", keep_workspaces=True)
    request = make_request(sha)
    service.execute(request)
    assert (tmp_path / "data" / "workspaces" / request.run_id / "app.txt").exists()


def test_observer_factory_receives_events(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": PIPELINE, "app.txt": "x"})
    finished: list[tuple[str, str, Status]] = []

    class Recorder(RunObserver):
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def on_job_end(self, job: Job, result: JobResult) -> None:
            finished.append((self.run_id, job.id, result.status))

    service = make_service(tmp_path, clone_url=str(repo))
    service.observer_factory = lambda request: [Recorder(request.run_id)]
    request = make_request(sha)
    service.execute(request)
    assert finished == [(request.run_id, "build", Status.SUCCESS)]
