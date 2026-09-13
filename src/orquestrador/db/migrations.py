"""Migrações do esquema com Alembic, executáveis sem ``alembic.ini``."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from orquestrador.db.session import ensure_sqlite_directory

#: Diretório com ``env.py`` e ``versions/`` (distribuído junto com o pacote).
MIGRATIONS_DIR = Path(__file__).parent / "alembic"


def alembic_config(url: str) -> Config:
    """Configuração do Alembic apontando para o banco informado."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_database(url: str, revision: str = "head") -> None:
    """Aplica as migrações até ``revision`` (idempotente)."""
    ensure_sqlite_directory(url)
    command.upgrade(alembic_config(url), revision)


def downgrade_database(url: str, revision: str) -> None:
    """Reverte as migrações até ``revision`` (ex.: ``base``)."""
    command.downgrade(alembic_config(url), revision)


def head_revision() -> str | None:
    """Revisão mais recente disponível no código."""
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def current_revision(url: str) -> str | None:
    """Revisão aplicada no banco (``None`` se nunca migrado)."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
