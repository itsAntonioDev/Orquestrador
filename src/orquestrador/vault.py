"""Cofre de secrets criptografados com Fernet e armazenados no banco.

* **Criptografia**: Fernet (AES-128-CBC + HMAC-SHA256, com timestamp).
* **Rotação**: ``ORQ_SECRET_KEYS="nova,antiga"`` — a primeira chave cifra,
  todas decifram; ``orquestrador secrets rotate`` recifra tudo com a nova.
* **Escopo**: secrets globais (valem para todos os projetos) e secrets de
  projeto, que sobrescrevem os globais de mesmo nome.
* Os valores nunca são listados; só nomes, escopo e datas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select

from orquestrador.db.models import SecretRow
from orquestrador.db.session import Database
from orquestrador.execution.results import utcnow
from orquestrador.pipeline.models import ENV_NAME_PATTERN

#: Valor de ``project`` usado para secrets globais.
GLOBAL_SCOPE = ""


class SecretError(Exception):
    """Chave inválida, secret inválido ou impossível de decifrar."""


class SecretCipher:
    """Cifra e decifra valores com uma ou mais chaves Fernet."""

    def __init__(self, keys: Sequence[str | bytes]) -> None:
        """Cria o cifrador.

        Args:
            keys: Chaves Fernet (base64 url-safe de 32 bytes); a primeira cifra.

        Raises:
            SecretError: Se nenhuma chave for informada ou alguma for inválida.
        """
        if not keys:
            raise SecretError("nenhuma chave de criptografia configurada (ORQ_SECRET_KEYS)")
        try:
            self._fernet = MultiFernet([Fernet(key) for key in keys])
        except (ValueError, TypeError) as exc:
            raise SecretError(
                "chave de criptografia inválida (gere com `orquestrador secrets generate-key`)"
            ) from exc

    @classmethod
    def from_config(cls, value: str) -> SecretCipher:
        """Cria o cifrador a partir de chaves separadas por vírgula."""
        return cls([key.strip() for key in value.split(",") if key.strip()])

    @staticmethod
    def generate_key() -> str:
        """Gera uma nova chave Fernet."""
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, plaintext: str) -> str:
        """Cifra um valor."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decifra um valor.

        Raises:
            SecretError: Se nenhuma das chaves conseguir decifrar.
        """
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretError("não foi possível decifrar o secret (chave incorreta?)") from exc

    def rotate(self, token: str) -> str:
        """Recifra um valor com a chave primária."""
        try:
            return self._fernet.rotate(token.encode("ascii")).decode("ascii")
        except InvalidToken as exc:
            raise SecretError("não foi possível decifrar o secret (chave incorreta?)") from exc


@dataclass(frozen=True)
class SecretInfo:
    """Metadados de um secret (sem o valor)."""

    name: str
    project: str | None
    created_at: datetime
    updated_at: datetime


def validate_secret_name(name: str) -> str:
    """Valida o nome de um secret (mesmas regras de variáveis de ambiente).

    Raises:
        SecretError: Se o nome for inválido.
    """
    if not ENV_NAME_PATTERN.match(name):
        raise SecretError(f"nome de secret inválido: {name!r} (use letras, números e _)")
    return name


def _scope(project: str | None) -> str:
    return project or GLOBAL_SCOPE


class SecretVault:
    """Armazena secrets cifrados no banco."""

    def __init__(self, database: Database, cipher: SecretCipher) -> None:
        """Cria o cofre.

        Args:
            database: Banco com a tabela ``secrets``.
            cipher: Cifrador configurado com as chaves.
        """
        self.database = database
        self.cipher = cipher

    def set(self, name: str, value: str, *, project: str | None = None) -> bool:
        """Cria ou atualiza um secret.

        Returns:
            ``True`` se foi criado, ``False`` se foi atualizado.
        """
        validate_secret_name(name)
        now = utcnow()
        with self.database.transaction() as session:
            row = session.scalars(
                select(SecretRow).where(
                    SecretRow.project == _scope(project), SecretRow.name == name
                )
            ).first()
            if row is None:
                session.add(
                    SecretRow(
                        project=_scope(project),
                        name=name,
                        ciphertext=self.cipher.encrypt(value),
                        created_at=now,
                        updated_at=now,
                    )
                )
                return True
            row.ciphertext = self.cipher.encrypt(value)
            row.updated_at = now
            return False

    def get(self, name: str, *, project: str | None = None) -> str | None:
        """Valor decifrado de um secret de um escopo específico (sem herança)."""
        with self.database.session() as session:
            row = session.scalars(
                select(SecretRow).where(
                    SecretRow.project == _scope(project), SecretRow.name == name
                )
            ).first()
            return None if row is None else self.cipher.decrypt(row.ciphertext)

    def delete(self, name: str, *, project: str | None = None) -> bool:
        """Remove um secret. Retorna ``True`` se existia."""
        with self.database.transaction() as session:
            row = session.scalars(
                select(SecretRow).where(
                    SecretRow.project == _scope(project), SecretRow.name == name
                )
            ).first()
            if row is None:
                return False
            session.delete(row)
            return True

    def list_secrets(self, project: str | None = None) -> list[SecretInfo]:
        """Metadados dos secrets: todos, ou os visíveis para um projeto (globais + do projeto)."""
        statement = select(SecretRow).order_by(SecretRow.project, SecretRow.name)
        if project is not None:
            statement = statement.where(SecretRow.project.in_([GLOBAL_SCOPE, project]))
        with self.database.session() as session:
            return [
                SecretInfo(
                    name=row.name,
                    project=row.project or None,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in session.scalars(statement)
            ]

    def resolve(self, project: str) -> dict[str, str]:
        """Secrets disponíveis para um projeto; os do projeto sobrescrevem os globais."""
        with self.database.session() as session:
            rows = session.scalars(
                select(SecretRow).where(SecretRow.project.in_([GLOBAL_SCOPE, project]))
            ).all()
        resolved: dict[str, str] = {}
        for row in sorted(rows, key=lambda item: item.project != GLOBAL_SCOPE):
            resolved[row.name] = self.cipher.decrypt(row.ciphertext)
        return resolved

    def rotate(self) -> int:
        """Recifra todos os secrets com a chave primária. Retorna a quantidade."""
        with self.database.transaction() as session:
            rows = session.scalars(select(SecretRow)).all()
            for row in rows:
                row.ciphertext = self.cipher.rotate(row.ciphertext)
                row.updated_at = utcnow()
            return len(rows)
