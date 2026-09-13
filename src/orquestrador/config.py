"""Configuração do servidor, lida de variáveis de ambiente com prefixo ``ORQ_``."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from typing import TYPE_CHECKING

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from orquestrador.executors.factory import ExecutorKind

if TYPE_CHECKING:
    from orquestrador.notifications.email import SmtpSettings


class DispatcherKind(StrEnum):
    """Como a API entrega execuções: pool de threads local ou fila Redis."""

    THREAD = "thread"
    RQ = "rq"


class WorkerClass(StrEnum):
    """Estratégia do worker RQ."""

    AUTO = "auto"
    FORK = "fork"
    SIMPLE = "simple"


class Settings(BaseSettings):
    """Configurações do orquestrador (servidor, workers e CLI).

    Todas podem ser definidas por variável de ambiente, ex.:
    ``ORQ_PROJECTS_FILE=/etc/orquestrador/projects.yml``.

    Attributes:
        projects_file: Arquivo YAML com os projetos (repositórios) configurados.
        data_dir: Diretório de dados (workspaces e relatórios).
        max_parallel_jobs: Jobs simultâneos dentro de uma execução.
        max_concurrent_runs: Execuções simultâneas no dispatcher em threads.
        max_webhook_body_bytes: Tamanho máximo aceito para o corpo de um webhook.
        keep_workspaces: Mantém o workspace após a execução (útil para depuração).
        git_executable: Executável do git usado no checkout.
        checkout_timeout: Tempo limite do checkout em segundos.
        executor: ``local`` (subprocess no host) ou ``docker`` (container por job).
        docker_default_image: Imagem usada por jobs sem ``image``.
        docker_pull_policy: ``always``, ``if-not-present`` ou ``never``.
        docker_network: Rede Docker dos containers dos jobs.
        docker_memory_limit: Limite de memória por container (ex.: ``2g``).
        docker_cpus: Limite de CPUs por container.
        docker_allow_bind_mounts: Permite volumes com caminhos absolutos do host.
        docker_allowed_bind_paths: Prefixos permitidos para bind mounts (JSON).
        docker_host_workspaces_dir: Caminho dos workspaces visto pelo daemon Docker,
            quando o orquestrador roda num container usando o Docker do host.
        dispatcher: ``thread`` (execução no processo da API) ou ``rq`` (fila Redis).
        redis_url: URL do Redis usado pela fila.
        queue_name: Nome da fila RQ.
        run_timeout: Tempo máximo de uma execução no worker (segundos).
        result_ttl: Retenção do resultado no Redis (segundos).
        failure_ttl: Retenção de execuções com erro no Redis (segundos).
        worker_class: ``auto``, ``fork`` (processo filho por execução) ou ``simple``.
        persistence_enabled: Grava o histórico de execuções no banco.
        database_url: URL SQLAlchemy (padrão: SQLite em ``data_dir``; em produção,
            ``postgresql+psycopg://usuario:senha@host:5432/orquestrador``).
        database_auto_migrate: Aplica as migrações ao iniciar a API/worker.
        database_echo: Registra o SQL executado no log.
        max_log_lines_per_step: Linhas de log armazenadas por step.
        live_poll_interval: Intervalo (s) com que o WebSocket do dashboard consulta o banco.
        secret_keys: Chaves Fernet separadas por vírgula; a primeira cifra, todas decifram.
        secrets_for_pull_requests: Entrega secrets a execuções de pull/merge requests.
        public_url: URL pública do dashboard, usada nos links das notificações.
        smtp_host: Servidor SMTP (sem ele, notificações por e-mail ficam desativadas).
        smtp_port: Porta SMTP.
        smtp_username: Usuário SMTP.
        smtp_password: Senha SMTP.
        smtp_sender: Remetente dos e-mails.
        smtp_starttls: Usa STARTTLS.
        smtp_ssl: Usa SMTP sobre TLS implícito (porta 465).
        notification_timeout: Tempo limite de envio de cada notificação (s).
    """

    model_config = SettingsConfigDict(env_prefix="ORQ_", env_file=".env", extra="ignore")

    projects_file: Path = Path("projects.yml")
    data_dir: Path = Path(".orquestrador")
    max_parallel_jobs: int = Field(default=2, ge=1)
    max_concurrent_runs: int = Field(default=2, ge=1)
    max_webhook_body_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    keep_workspaces: bool = False
    git_executable: str = "git"
    checkout_timeout: float = Field(default=600, gt=0)

    executor: ExecutorKind = ExecutorKind.LOCAL
    docker_default_image: str = "python:3.11-slim"
    docker_pull_policy: Literal["always", "if-not-present", "never"] = "if-not-present"
    docker_network: str | None = None
    docker_memory_limit: str | None = None
    docker_cpus: float | None = Field(default=None, gt=0)
    docker_allow_bind_mounts: bool = False
    docker_allowed_bind_paths: list[str] = Field(default_factory=list)
    docker_host_workspaces_dir: str | None = None

    dispatcher: DispatcherKind = DispatcherKind.THREAD
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = Field(default="orquestrador", min_length=1)
    run_timeout: int = Field(default=3 * 3600, ge=60)
    result_ttl: int = Field(default=24 * 3600, ge=0)
    failure_ttl: int = Field(default=7 * 24 * 3600, ge=0)
    worker_class: WorkerClass = WorkerClass.AUTO

    persistence_enabled: bool = True
    database_url: str | None = None
    database_auto_migrate: bool = True
    database_echo: bool = False
    max_log_lines_per_step: int = Field(default=20_000, ge=1)
    live_poll_interval: float = Field(default=0.5, gt=0)

    secret_keys: SecretStr | None = None
    secrets_for_pull_requests: bool = False

    public_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str = "orquestrador@localhost"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    notification_timeout: float = Field(default=10.0, gt=0)

    @property
    def smtp_settings(self) -> SmtpSettings | None:
        """Configuração SMTP, ou ``None`` se ``ORQ_SMTP_HOST`` não foi definido."""
        if not self.smtp_host:
            return None
        from orquestrador.notifications.email import SmtpSettings

        return SmtpSettings(
            host=self.smtp_host,
            port=self.smtp_port,
            username=self.smtp_username,
            password=self.smtp_password.get_secret_value() if self.smtp_password else None,
            sender=self.smtp_sender,
            starttls=self.smtp_starttls,
            use_ssl=self.smtp_ssl,
            timeout=self.notification_timeout,
        )

    @property
    def effective_database_url(self) -> str:
        """``database_url`` ou, se ausente, um SQLite em ``<data_dir>/orquestrador.db``."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'orquestrador.db').resolve().as_posix()}"

    @property
    def workspaces_dir(self) -> Path:
        """Diretório onde cada execução ganha um workspace próprio."""
        return self.data_dir / "workspaces"

    @property
    def reports_dir(self) -> Path:
        """Diretório com o resultado JSON de cada execução."""
        return self.data_dir / "runs"


@lru_cache
def get_settings() -> Settings:
    """Retorna as configurações carregadas do ambiente (com cache)."""
    return Settings()
