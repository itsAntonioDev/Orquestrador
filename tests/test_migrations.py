"""Testes das migrações Alembic e da configuração do banco."""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from orquestrador.config import Settings
from orquestrador.db import Database
from orquestrador.db.base import Base
from orquestrador.db.migrations import (
    current_revision,
    downgrade_database,
    head_revision,
    upgrade_database,
)

TABLES = {"runs", "job_runs", "step_runs", "log_lines", "secrets"}


def table_names(url: str) -> set[str]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_schema(database_url: str) -> None:
    assert current_revision(database_url) is None

    upgrade_database(database_url)

    assert TABLES | {"alembic_version"} <= table_names(database_url)
    assert current_revision(database_url) == head_revision()
    assert head_revision() is not None


def test_upgrade_is_idempotent(database_url: str) -> None:
    upgrade_database(database_url)
    upgrade_database(database_url)
    assert current_revision(database_url) == head_revision()


def test_models_match_migrations(database_url: str) -> None:
    upgrade_database(database_url)
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


def test_downgrade_removes_tables(database_url: str) -> None:
    upgrade_database(database_url)
    downgrade_database(database_url, "base")
    assert not TABLES & table_names(database_url)
    assert current_revision(database_url) is None


def test_default_database_url_is_sqlite_in_data_dir(tmp_path: Path) -> None:
    expected = f"sqlite:///{(tmp_path / 'orquestrador.db').resolve().as_posix()}"
    assert Settings(data_dir=tmp_path).effective_database_url == expected
    custom = "postgresql+psycopg://orq:senha@db:5432/orquestrador"
    assert Settings(data_dir=tmp_path, database_url=custom).effective_database_url == custom


def test_database_creates_sqlite_directory(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'a' / 'b' / 'x.db').as_posix()}")
    try:
        assert (tmp_path / "a" / "b").is_dir()
    finally:
        database.dispose()


def test_sqlite_pragmas(database: Database) -> None:
    with database.session() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
