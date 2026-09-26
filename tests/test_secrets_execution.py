"""Testes de secrets no runner, no RunService e na CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from orquestrador.cli import app
from orquestrador.config import Settings
from orquestrador.dispatch import RunRequest, RunService
from orquestrador.execution import PipelineRunner, RunContext, Status
from orquestrador.execution.masking import MASK
from orquestrador.executors import CommandResult, LocalExecutor
from orquestrador.projects import Project, ProjectRegistry, Provider
from orquestrador.webhooks import TriggerEvent
from tests.conftest import GitRepoFactory, make_pipeline, py, requires_git
from tests.test_runner import FakeExecutor, RecordingObserver

TOKEN = "tok-super-secreto-123"


def run_with_secrets(pipeline: object, tmp_path: Path, **kwargs: object) -> object:
    context = RunContext(workspace=tmp_path, secrets={"DEPLOY_TOKEN": TOKEN})
    return PipelineRunner(LocalExecutor(), **kwargs).run(pipeline, context)  # type: ignore[arg-type]


def test_secret_is_injected_and_masked(tmp_path: Path) -> None:
    code = (
        "import os, sys\n"
        "print('tamanho', len(os.environ['TOKEN']))\n"
        "print('env:', os.environ['TOKEN'])\n"
        "print('direto: ${{ secrets.DEPLOY_TOKEN }}', file=sys.stderr)"
    )
    pipeline = make_pipeline(
        {"deploy": {"env": {"TOKEN": "${{ secrets.DEPLOY_TOKEN }}"}, "steps": [py(code)]}}
    )
    observer = RecordingObserver()

    result = run_with_secrets(pipeline, tmp_path, observers=[observer])

    step = result.jobs["deploy"].steps[0]  # type: ignore[attr-defined]
    assert step.status == Status.SUCCESS
    assert step.stdout.splitlines() == [f"tamanho {len(TOKEN)}", f"env: {MASK}"]
    assert step.stderr == f"direto: {MASK}"
    assert TOKEN not in step.output
    assert all(TOKEN not in str(event) for event in observer.events)


def test_missing_secret_fails_step(tmp_path: Path) -> None:
    pipeline = make_pipeline(
        {
            "j": {
                "steps": [
                    py("print('${{ secrets.NAO_EXISTE }}')", name="usa"),
                    py("print('depois')", name="depois"),
                ]
            }
        }
    )
    job = run_with_secrets(pipeline, tmp_path).jobs["j"]  # type: ignore[attr-defined]
    assert [step.status for step in job.steps] == [Status.FAILURE, Status.SKIPPED]
    assert job.steps[0].error == "expressão inválida: secret não definido: NAO_EXISTE"


def test_skipped_step_does_not_need_its_secrets(tmp_path: Path) -> None:
    pipeline = make_pipeline(
        {"j": {"steps": [py("print('${{ secrets.NAO_EXISTE }}')", **{"if": "false"})]}}
    )
    job = run_with_secrets(pipeline, tmp_path).jobs["j"]  # type: ignore[attr-defined]
    assert job.steps[0].status == Status.SKIPPED
    assert job.status == Status.SUCCESS


def test_executor_errors_are_masked(tmp_path: Path) -> None:
    executor = FakeExecutor(
        lambda request, on_output: CommandResult(exit_code=None, error=f"falhou com {TOKEN}")
    )
    pipeline = make_pipeline({"j": {"steps": [{"run": "deploy"}]}})
    context = RunContext(workspace=tmp_path, secrets={"DEPLOY_TOKEN": TOKEN})
    step = PipelineRunner(executor).run(pipeline, context).jobs["j"].steps[0]
    assert step.error == f"falhou com {MASK}"


def test_secrets_are_not_in_context_repr() -> None:
    assert TOKEN not in repr(RunContext(secrets={"DEPLOY_TOKEN": TOKEN}))


SERVICE_PIPELINE = """
name: ci
jobs:
  deploy:
    env:
      TOKEN: ${{ secrets.TOKEN }}
    steps:
      - shell: python
        run: import os; print(len(os.environ['TOKEN']), os.environ['TOKEN'])
"""


def service_request(sha: str, event: str = "push") -> RunRequest:
    trigger = TriggerEvent(
        provider=Provider.GITHUB,
        event=event,  # type: ignore[arg-type]
        repository="acme/app",
        branch="feature" if event == "pull_request" else "main",
        base_branch="main" if event == "pull_request" else None,
        commit=sha,
    )
    return RunRequest(project="app", trigger=trigger)


def make_service(repo: Path, tmp_path: Path, provided: list[str], **settings: object) -> RunService:
    def provider(project: str) -> dict[str, str]:
        provided.append(project)
        return {"TOKEN": "segredo-do-cofre"}

    registry = ProjectRegistry(
        [
            Project(
                name="app",
                provider=Provider.GITHUB,
                repository="acme/app",
                secret="s",
                clone_url=str(repo),
            )
        ]
    )
    return RunService(
        Settings(data_dir=tmp_path / "data", **settings),  # type: ignore[arg-type]
        registry,
        secret_provider=provider,
    )


@requires_git
def test_service_injects_project_secrets(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": SERVICE_PIPELINE})
    provided: list[str] = []

    outcome = make_service(repo, tmp_path, provided).execute(service_request(sha))

    assert provided == ["app"]
    assert outcome.result is not None
    assert outcome.result.jobs["deploy"].steps[0].stdout == f"16 {MASK}"


@requires_git
def test_pull_requests_do_not_receive_secrets_by_default(
    git_repo: GitRepoFactory, tmp_path: Path
) -> None:
    repo, sha = git_repo({".orquestrador.yml": SERVICE_PIPELINE})
    provided: list[str] = []

    outcome = make_service(repo, tmp_path, provided).execute(service_request(sha, "pull_request"))

    assert provided == []
    assert outcome.result is not None
    step = outcome.result.jobs["deploy"].steps[0]
    assert step.status == Status.FAILURE
    assert step.error == "expressão inválida: secret não definido: TOKEN"


@requires_git
def test_pull_requests_receive_secrets_when_allowed(
    git_repo: GitRepoFactory, tmp_path: Path
) -> None:
    repo, sha = git_repo({".orquestrador.yml": SERVICE_PIPELINE})
    provided: list[str] = []
    service = make_service(repo, tmp_path, provided, secrets_for_pull_requests=True)

    outcome = service.execute(service_request(sha, "pull_request"))

    assert provided == ["app"]
    assert outcome.result is not None and outcome.result.status == Status.SUCCESS


@requires_git
def test_secret_provider_failure(git_repo: GitRepoFactory, tmp_path: Path) -> None:
    repo, sha = git_repo({".orquestrador.yml": SERVICE_PIPELINE})
    service = make_service(repo, tmp_path, [])

    def broken(project: str) -> dict[str, str]:
        raise RuntimeError("chave errada")

    service.secret_provider = broken
    outcome = service.execute(service_request(sha))
    assert outcome.status == "error"
    assert outcome.reason == "falha ao carregar os secrets: RuntimeError"


CLI_PIPELINE = """
name: cli
jobs:
  j:
    env:
      TOKEN: ${{ secrets.TOKEN }}
    steps:
      - shell: python
        run: import os; print('token=' + os.environ['TOKEN'])
"""


def test_cli_run_with_secret_option(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.yml"
    path.write_text(CLI_PIPELINE, encoding="utf-8")
    result = CliRunner().invoke(
        app, ["run", str(path), "--workspace", str(tmp_path), "--secret", "TOKEN=tok-local-123"]
    )
    assert result.exit_code == 0, result.output
    assert "tok-local-123" not in result.output
    assert f"token={MASK}" in result.output


def test_cli_run_invalid_secret_option(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.yml"
    path.write_text(CLI_PIPELINE, encoding="utf-8")
    result = CliRunner().invoke(app, ["run", str(path), "--secret", "sem-igual"])
    assert result.exit_code == 2
    assert "--secret" in result.output


def test_cli_validate_lists_secret_references(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.yml"
    path.write_text(CLI_PIPELINE, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", str(path)])
    assert result.exit_code == 0
    assert "Secrets referenciados: TOKEN" in result.output


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch, database_url: str) -> str:
    from orquestrador.vault import SecretCipher

    key = SecretCipher.generate_key()
    monkeypatch.setenv("ORQ_DATABASE_URL", database_url)
    monkeypatch.setenv("ORQ_SECRET_KEYS", key)
    return key


class TestSecretsCli:
    cli = CliRunner()

    def test_generate_key(self) -> None:
        from cryptography.fernet import Fernet

        result = self.cli.invoke(app, ["secrets", "generate-key"])
        assert result.exit_code == 0
        Fernet(result.output.strip())

    def test_set_list_delete(self, secret_env: str) -> None:
        created = self.cli.invoke(app, ["secrets", "set", "DEPLOY_TOKEN", "--value", "valor-1"])
        assert created.exit_code == 0, created.output
        assert "secret 'DEPLOY_TOKEN' criado (global)" in created.output

        from_stdin = self.cli.invoke(
            app, ["secrets", "set", "DB_PASS", "--project", "api"], input="senha-do-banco\n"
        )
        assert from_stdin.exit_code == 0, from_stdin.output
        assert "criado (projeto api)" in from_stdin.output

        updated = self.cli.invoke(app, ["secrets", "set", "DEPLOY_TOKEN", "--value", "valor-2"])
        assert "atualizado (global)" in updated.output

        listing = self.cli.invoke(app, ["secrets", "list"])
        assert listing.exit_code == 0
        assert "DEPLOY_TOKEN" in listing.output and "DB_PASS" in listing.output
        assert "projeto api" in listing.output
        assert "valor-2" not in listing.output and "senha-do-banco" not in listing.output

        scoped = self.cli.invoke(app, ["secrets", "list", "--project", "web"])
        assert "DEPLOY_TOKEN" in scoped.output and "DB_PASS" not in scoped.output

        assert (
            self.cli.invoke(app, ["secrets", "delete", "DB_PASS", "--project", "api"]).exit_code
            == 0
        )
        missing = self.cli.invoke(app, ["secrets", "delete", "DB_PASS", "--project", "api"])
        assert missing.exit_code == 1
        assert "não encontrado" in missing.output

    def test_value_read_from_stdin_is_stored(self, secret_env: str, database_url: str) -> None:
        from orquestrador.db import Database
        from orquestrador.vault import SecretCipher, SecretVault

        self.cli.invoke(app, ["secrets", "set", "SENHA"], input="minha senha\r\n")
        database = Database(database_url)
        try:
            assert SecretVault(database, SecretCipher([secret_env])).get("SENHA") == "minha senha"
        finally:
            database.dispose()

    def test_empty_list(self, secret_env: str) -> None:
        assert "Nenhum secret cadastrado" in self.cli.invoke(app, ["secrets", "list"]).output

    def test_invalid_name(self, secret_env: str) -> None:
        result = self.cli.invoke(app, ["secrets", "set", "nome-ruim", "--value", "x"])
        assert result.exit_code == 2
        assert "nome de secret inválido" in result.output

    def test_empty_value(self, secret_env: str) -> None:
        result = self.cli.invoke(app, ["secrets", "set", "TOKEN"], input="\n")
        assert result.exit_code == 2

    def test_rotate(self, secret_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from orquestrador.vault import SecretCipher

        self.cli.invoke(app, ["secrets", "set", "TOKEN", "--value", "x"])
        new = SecretCipher.generate_key()
        monkeypatch.setenv("ORQ_SECRET_KEYS", f"{new},{secret_env}")
        rotated = self.cli.invoke(app, ["secrets", "rotate"])
        assert rotated.exit_code == 0, rotated.output
        assert "1 secret(s) recriptografado(s)" in rotated.output

        monkeypatch.setenv("ORQ_SECRET_KEYS", new)
        again = self.cli.invoke(app, ["secrets", "rotate"])
        assert again.exit_code == 0, again.output

    def test_rotate_with_wrong_key(self, secret_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from orquestrador.vault import SecretCipher

        self.cli.invoke(app, ["secrets", "set", "TOKEN", "--value", "x"])
        monkeypatch.setenv("ORQ_SECRET_KEYS", SecretCipher.generate_key())
        assert self.cli.invoke(app, ["secrets", "rotate"]).exit_code == 2

    def test_missing_key(self, monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
        monkeypatch.setenv("ORQ_DATABASE_URL", database_url)
        monkeypatch.delenv("ORQ_SECRET_KEYS", raising=False)
        result = self.cli.invoke(app, ["secrets", "list"])
        assert result.exit_code == 2
        assert "ORQ_SECRET_KEYS" in result.output

    def test_invalid_key(self, monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
        monkeypatch.setenv("ORQ_DATABASE_URL", database_url)
        monkeypatch.setenv("ORQ_SECRET_KEYS", "chave-invalida")
        result = self.cli.invoke(app, ["secrets", "list"])
        assert result.exit_code == 2
        assert "inválida" in result.output
