"""Conexão com o banco: engine, sessões e ajustes específicos do SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def is_sqlite(url: str) -> bool:
    """Se a URL aponta para SQLite."""
    return make_url(url).get_backend_name() == "sqlite"


def ensure_sqlite_directory(url: str) -> None:
    """Cria o diretório do arquivo SQLite, se necessário."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Liga chaves estrangeiras (cascata) e WAL (leituras concorrentes com escrita)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


class Database:
    """Engine + fábrica de sessões.

    Attributes:
        url: URL SQLAlchemy (ex.: ``postgresql+psycopg://user:senha@host/db``).
        engine: Engine compartilhada (thread-safe).
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        """Cria a conexão (preguiçosa: nada é aberto até a primeira consulta).

        Args:
            url: URL do banco.
            echo: Registra o SQL executado no log.
        """
        self.url = url
        options: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
        if is_sqlite(url):
            ensure_sqlite_directory(url)
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(url, **options)
        if is_sqlite(url):
            event.listen(self.engine, "connect", _configure_sqlite)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        """Nova sessão para leitura (use como context manager)."""
        return self._sessions()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Sessão com transação: commit ao sair, rollback em caso de exceção."""
        with self._sessions() as session, session.begin():
            yield session

    def dispose(self) -> None:
        """Fecha as conexões do pool."""
        self.engine.dispose()
