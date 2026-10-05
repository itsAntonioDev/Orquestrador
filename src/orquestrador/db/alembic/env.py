"""Ambiente do Alembic: usa os metadados dos modelos do orquestrador."""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from orquestrador.db import models  # noqa: F401 - register tables in metadata
from orquestrador.db.base import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Renderiza ``UTCDateTime`` como ``sa.DateTime(timezone=True)`` nas migrações."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(**options: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
        compare_type=True,
        **options,
    )


def run_migrations_offline() -> None:
    """Gera o SQL sem conectar ao banco."""
    _configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Aplica as migrações conectado ao banco."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with engine.connect() as new_connection:
            _run_with_connection(new_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
