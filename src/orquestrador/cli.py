"""Interface de linha de comando: ``orquestrador run pipeline.yml``."""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from orquestrador import __version__
from orquestrador.config import WorkerClass
from orquestrador.console import ConsoleObserver
from orquestrador.execution import PipelineRunner, RunContext, Status
from orquestrador.executors import ExecutorKind, build_executor
from orquestrador.pipeline import (
    Pipeline,
    PipelineError,
    PipelineValidationError,
    load_pipeline,
    pipeline_secret_references,
)
from orquestrador.pipeline.models import ENV_NAME_PATTERN

#: Códigos de saída da CLI.
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_INVALID = 2
EXIT_CANCELLED = 130

app = typer.Typer(
    name="orquestrador",
    help="Orquestrador de CI/CD self-hosted com pipelines definidos em YAML.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def parse_env_assignments(values: list[str] | None, option: str = "--env") -> dict[str, str]:
    """Converte opções ``NOME=valor`` num dicionário.

    Args:
        values: Valores recebidos na opção.
        option: Nome da opção, usado na mensagem de erro.

    Returns:
        Mapeamento de variáveis.

    Raises:
        typer.BadParameter: Se algum item não estiver no formato ``NOME=valor``.
    """
    env: dict[str, str] = {}
    for item in values or []:
        name, separator, value = item.partition("=")
        if not separator or not ENV_NAME_PATTERN.match(name):
            raise typer.BadParameter(
                f"use o formato NOME=valor (recebido {item!r})", param_hint=option
            )
        env[name] = value
    return env


def _load_or_exit(path: Path, console: Console) -> Pipeline:
    """Carrega o pipeline ou encerra a CLI com código 2 exibindo os erros."""
    try:
        return load_pipeline(path)
    except PipelineValidationError as exc:
        console.print(Text(f"Pipeline inválido: {exc.source}", style="bold red"))
        for error in exc.errors:
            console.print(Text(f"  - {error}", style="red"), soft_wrap=True)
        raise typer.Exit(EXIT_INVALID) from exc
    except PipelineError as exc:
        console.print(Text(str(exc), style="bold red"), soft_wrap=True)
        raise typer.Exit(EXIT_INVALID) from exc


@app.command()
def run(
    pipeline_file: Annotated[Path, typer.Argument(help="Arquivo YAML do pipeline.")],
    env: Annotated[
        list[str] | None,
        typer.Option("--env", "-e", help="Variável de ambiente NOME=valor (repetível)."),
    ] = None,
    event: Annotated[str, typer.Option(help="Evento simulado (push, pull_request...).")] = "manual",
    branch: Annotated[str | None, typer.Option(help="Branch simulada.")] = None,
    tag: Annotated[str | None, typer.Option(help="Tag simulada.")] = None,
    commit: Annotated[str | None, typer.Option(help="SHA do commit simulado.")] = None,
    workspace: Annotated[
        Path | None, typer.Option(help="Diretório de trabalho (padrão: diretório atual).")
    ] = None,
    parallel: Annotated[
        int, typer.Option("--parallel", "-p", min=1, help="Máximo de jobs simultâneos.")
    ] = 1,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Oculta a saída dos comandos.")
    ] = False,
    report: Annotated[
        Path | None, typer.Option(help="Grava o resultado da execução em JSON.")
    ] = None,
    executor: Annotated[
        ExecutorKind,
        typer.Option("--executor", "-x", help="Onde rodar os jobs: local ou docker."),
    ] = ExecutorKind.LOCAL,
    image: Annotated[
        str, typer.Option(help="Imagem padrão para jobs sem 'image' (executor docker).")
    ] = "python:3.11-slim",
    secret: Annotated[
        list[str] | None,
        typer.Option(
            "--secret", "-s", help="Secret NOME=valor para ${{ secrets.NOME }} (mascarado)."
        ),
    ] = None,
) -> None:
    """Executa um pipeline localmente (no host ou em containers Docker)."""
    console = Console()
    overrides = parse_env_assignments(env)
    secrets = parse_env_assignments(secret, option="--secret")
    pipeline = _load_or_exit(pipeline_file, console)

    context = RunContext(
        workspace=(workspace or Path.cwd()).resolve(),
        event=event,
        branch=branch,
        tag=tag,
        commit=commit,
        ref=f"refs/tags/{tag}" if tag else (f"refs/heads/{branch}" if branch else None),
        env=overrides,
        secrets=secrets,
    )
    docker_options = {"default_image": image} if executor is ExecutorKind.DOCKER else {}
    runner = PipelineRunner(
        build_executor(executor, **docker_options),
        observers=[ConsoleObserver(console, show_output=not quiet)],
        max_parallel_jobs=parallel,
    )

    in_main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = (
        signal.signal(signal.SIGINT, lambda *_: context.cancel()) if in_main_thread else None
    )
    try:
        result = runner.run(pipeline, context)
    finally:
        if in_main_thread:
            signal.signal(signal.SIGINT, previous_handler)

    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    if result.status == Status.SUCCESS:
        raise typer.Exit(EXIT_SUCCESS)
    raise typer.Exit(EXIT_CANCELLED if result.status == Status.CANCELLED else EXIT_FAILURE)


@app.command()
def validate(
    pipeline_file: Annotated[Path, typer.Argument(help="Arquivo YAML do pipeline.")],
) -> None:
    """Valida um pipeline e mostra a ordem de execução dos jobs."""
    console = Console()
    pipeline = _load_or_exit(pipeline_file, console)
    console.print(Text.assemble(("Pipeline válido: ", "bold green"), (pipeline.name, "bold")))
    if pipeline.triggers:
        console.print(Text(f"Gatilhos: {', '.join(pipeline.triggers)}"))
    for number, stage in enumerate(pipeline.execution_stages(), start=1):
        jobs = ", ".join(f"{job.id} ({len(job.steps)} steps)" for job in stage)
        console.print(Text(f"Estágio {number}: {jobs}"), soft_wrap=True)
    references = pipeline_secret_references(pipeline)
    if references:
        console.print(Text(f"Secrets referenciados: {', '.join(sorted(references))}"))


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Endereço de escuta.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Porta HTTP.")] = 8000,
    projects: Annotated[
        Path | None, typer.Option(help="Arquivo de projetos (padrão: ORQ_PROJECTS_FILE).")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option(help="Diretório de dados (padrão: ORQ_DATA_DIR).")
    ] = None,
    log_level: Annotated[str, typer.Option(help="Nível de log (debug, info...).")] = "info",
) -> None:
    """Inicia a API que recebe webhooks do GitHub/GitLab."""
    import uvicorn

    from orquestrador.api import create_app
    from orquestrador.config import Settings
    from orquestrador.projects import ProjectRegistryError

    console = Console()
    overrides: dict[str, Any] = {}
    if projects is not None:
        overrides["projects_file"] = projects
    if data_dir is not None:
        overrides["data_dir"] = data_dir
    settings = Settings(**overrides)

    logging.basicConfig(
        level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        application = create_app(settings)
    except ProjectRegistryError as exc:
        console.print(Text(str(exc), style="bold red"), soft_wrap=True)
        raise typer.Exit(EXIT_INVALID) from exc
    uvicorn.run(application, host=host, port=port, log_level=log_level.lower())


@app.command()
def worker(
    queue: Annotated[
        list[str] | None,
        typer.Option("--queue", "-Q", help="Fila(s) consumidas (padrão: ORQ_QUEUE_NAME)."),
    ] = None,
    burst: Annotated[bool, typer.Option(help="Processa o que está na fila e encerra.")] = False,
    name: Annotated[str | None, typer.Option(help="Nome do worker.")] = None,
    worker_class: Annotated[
        WorkerClass | None,
        typer.Option(help="auto, fork ou simple (padrão: ORQ_WORKER_CLASS)."),
    ] = None,
    log_level: Annotated[str, typer.Option(help="Nível de log (debug, info...).")] = "info",
) -> None:
    """Inicia um worker que executa os pipelines enfileirados no Redis."""
    from redis.exceptions import RedisError

    from orquestrador.config import Settings
    from orquestrador.dispatch import rq_queue
    from orquestrador.scm import redact_credentials

    console = Console()
    settings = Settings()
    logging.basicConfig(
        level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    connection = rq_queue.redis_connection(settings.redis_url)
    try:
        connection.ping()
    except RedisError as exc:
        console.print(
            Text(
                f"Redis indisponível em {redact_credentials(settings.redis_url)}: {exc}",
                style="bold red",
            ),
            soft_wrap=True,
        )
        raise typer.Exit(EXIT_INVALID) from exc

    rq_worker = rq_queue.create_worker(
        connection,
        queue or [settings.queue_name],
        worker_class=worker_class or settings.worker_class,
        name=name,
    )
    rq_worker.work(burst=burst, logging_level=log_level.upper())


db_app = typer.Typer(help="Banco de dados do histórico: migrações.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Revisão alvo.")] = "head",
) -> None:
    """Aplica as migrações do banco (ORQ_DATABASE_URL)."""
    from orquestrador.config import Settings
    from orquestrador.db.migrations import upgrade_database
    from orquestrador.scm import redact_credentials

    settings = Settings()
    upgrade_database(settings.effective_database_url, revision)
    typer.echo(
        f"banco atualizado para '{revision}': {redact_credentials(settings.effective_database_url)}"
    )


@db_app.command("current")
def db_current() -> None:
    """Mostra a revisão aplicada no banco e a mais recente disponível."""
    from orquestrador.config import Settings
    from orquestrador.db.migrations import current_revision, head_revision

    settings = Settings()
    typer.echo(f"aplicada:  {current_revision(settings.effective_database_url) or '(nenhuma)'}")
    typer.echo(f"disponível: {head_revision()}")


@app.command()
def history(
    project: Annotated[str | None, typer.Option(help="Filtra por projeto.")] = None,
    status: Annotated[
        str | None, typer.Option(help="Filtra por status (success, failure...).")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Quantidade de execuções.")] = 20,
) -> None:
    """Lista as últimas execuções registradas no banco."""
    from rich.table import Table
    from sqlalchemy.exc import SQLAlchemyError

    from orquestrador.config import Settings
    from orquestrador.db import Database, RunRepository, RunStatus

    console = Console()
    try:
        status_filter = RunStatus(status) if status else None
    except ValueError as exc:
        valid = ", ".join(item.value for item in RunStatus)
        raise typer.BadParameter(f"use um de: {valid}", param_hint="--status") from exc

    settings = Settings()
    database = Database(settings.effective_database_url)
    try:
        page = RunRepository(database).list_runs(project=project, status=status_filter, limit=limit)
    except SQLAlchemyError as exc:
        console.print(
            Text(
                f"não foi possível consultar o histórico ({exc.__class__.__name__}). "
                "Rode `orquestrador db upgrade`.",
                style="bold red",
            ),
            soft_wrap=True,
        )
        raise typer.Exit(EXIT_INVALID) from exc
    finally:
        database.dispose()

    if not page.items:
        console.print("Nenhuma execução registrada.")
        return
    table = Table(header_style="bold")
    for column in (
        "Run",
        "Projeto",
        "Evento",
        "Ref",
        "Commit",
        "Status",
        "Duração",
        "Criada em (UTC)",
    ):
        table.add_column(column)
    for run in page.items:
        table.add_row(
            run.run_id,
            run.project,
            run.event,
            run.branch or run.tag or "-",
            run.commit[:10],
            run.status.value,
            f"{run.duration:.1f}s" if run.duration is not None else "-",
            run.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)
    console.print(f"{len(page.items)} de {page.total} execuções")


secrets_app = typer.Typer(help="Cofre de secrets criptografados.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")


def _open_vault(console: Console) -> Any:
    """Abre o cofre com ``ORQ_SECRET_KEYS`` e ``ORQ_DATABASE_URL``
    (encerra com código 2 se faltar).
    """
    from orquestrador.config import Settings
    from orquestrador.db import Database
    from orquestrador.db.migrations import upgrade_database
    from orquestrador.vault import SecretCipher, SecretError, SecretVault

    settings = Settings()
    if settings.secret_keys is None:
        console.print(
            Text(
                "defina ORQ_SECRET_KEYS (gere uma chave com `orquestrador secrets generate-key`)",
                style="bold red",
            ),
            soft_wrap=True,
        )
        raise typer.Exit(EXIT_INVALID)
    try:
        cipher = SecretCipher.from_config(settings.secret_keys.get_secret_value())
    except SecretError as exc:
        console.print(Text(str(exc), style="bold red"), soft_wrap=True)
        raise typer.Exit(EXIT_INVALID) from exc
    url = settings.effective_database_url
    if settings.database_auto_migrate:
        upgrade_database(url)
    return SecretVault(Database(url), cipher)


@secrets_app.command("generate-key")
def secrets_generate_key() -> None:
    """Gera uma chave nova para ORQ_SECRET_KEYS."""
    from orquestrador.vault import SecretCipher

    typer.echo(SecretCipher.generate_key())


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Nome do secret (ex.: DEPLOY_TOKEN).")],
    project: Annotated[
        str | None, typer.Option(help="Projeto (padrão: secret global).")
    ] = None,
    value: Annotated[
        str | None, typer.Option(help="Valor; se omitido, é lido da entrada padrão.")
    ] = None,
) -> None:
    """Cria ou atualiza um secret."""
    import sys

    from orquestrador.vault import SecretError

    console = Console()
    if value is None:
        value = (
            typer.prompt("Valor", hide_input=True)
            if sys.stdin.isatty()
            else sys.stdin.read().rstrip("\r\n")
        )
    if not value:
        raise typer.BadParameter("o valor não pode ser vazio", param_hint="--value")
    vault = _open_vault(console)
    try:
        created = vault.set(name, value, project=project)
    except SecretError as exc:
        console.print(Text(str(exc), style="bold red"), soft_wrap=True)
        raise typer.Exit(EXIT_INVALID) from exc
    finally:
        vault.database.dispose()
    scope = f"projeto {project}" if project else "global"
    typer.echo(f"secret '{name}' {'criado' if created else 'atualizado'} ({scope})")


@secrets_app.command("list")
def secrets_list(
    project: Annotated[
        str | None, typer.Option(help="Mostra só os secrets visíveis para este projeto.")
    ] = None,
) -> None:
    """Lista nomes e escopos dos secrets (os valores nunca são exibidos)."""
    from rich.table import Table

    console = Console()
    vault = _open_vault(console)
    try:
        items = vault.list_secrets(project)
    finally:
        vault.database.dispose()
    if not items:
        console.print("Nenhum secret cadastrado.")
        return
    table = Table(header_style="bold")
    for column in ("Nome", "Escopo", "Atualizado em (UTC)"):
        table.add_column(column)
    for item in items:
        table.add_row(
            item.name,
            f"projeto {item.project}" if item.project else "global",
            item.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)


@secrets_app.command("delete")
def secrets_delete(
    name: Annotated[str, typer.Argument(help="Nome do secret.")],
    project: Annotated[str | None, typer.Option(help="Projeto (padrão: global).")] = None,
) -> None:
    """Remove um secret."""
    console = Console()
    vault = _open_vault(console)
    try:
        removed = vault.delete(name, project=project)
    finally:
        vault.database.dispose()
    if not removed:
        console.print(Text(f"secret '{name}' não encontrado", style="bold red"))
        raise typer.Exit(EXIT_FAILURE)
    typer.echo(f"secret '{name}' removido")


@secrets_app.command("rotate")
def secrets_rotate() -> None:
    """Recifra todos os secrets com a primeira chave de ORQ_SECRET_KEYS."""
    from orquestrador.vault import SecretError

    console = Console()
    vault = _open_vault(console)
    try:
        count = vault.rotate()
    except SecretError as exc:
        console.print(Text(str(exc), style="bold red"), soft_wrap=True)
        raise typer.Exit(EXIT_INVALID) from exc
    finally:
        vault.database.dispose()
    typer.echo(f"{count} secret(s) recriptografado(s) com a chave primária")


@app.command()
def version() -> None:
    """Mostra a versão instalada."""
    typer.echo(f"orquestrador {__version__}")


def main() -> None:
    """Ponto de entrada para ``python -m orquestrador``."""
    app()
