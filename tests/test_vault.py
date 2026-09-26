"""Testes do cofre de secrets (Fernet + banco)."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from orquestrador.bootstrap import build_repository, build_secret_provider
from orquestrador.config import Settings
from orquestrador.db import Database
from orquestrador.db.models import SecretRow
from orquestrador.vault import SecretCipher, SecretError, SecretVault


def new_key() -> str:
    return SecretCipher.generate_key()


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher([new_key()])


@pytest.fixture
def vault(database: Database, cipher: SecretCipher) -> SecretVault:
    return SecretVault(database, cipher)


class TestCipher:
    def test_roundtrip(self, cipher: SecretCipher) -> None:
        token = cipher.encrypt("valor com acentuação ç")
        assert "valor" not in token
        assert cipher.decrypt(token) == "valor com acentuação ç"

    def test_generated_key_is_valid_fernet_key(self) -> None:
        Fernet(new_key())

    def test_requires_at_least_one_key(self) -> None:
        with pytest.raises(SecretError, match="nenhuma chave"):
            SecretCipher([])

    def test_invalid_key(self) -> None:
        with pytest.raises(SecretError, match="chave de criptografia inválida"):
            SecretCipher(["nao-e-uma-chave"])

    def test_from_config_accepts_multiple_keys(self) -> None:
        first, second = new_key(), new_key()
        cipher = SecretCipher.from_config(f" {first} , {second} ,")
        assert cipher.decrypt(SecretCipher([second]).encrypt("x")) == "x"

    def test_wrong_key(self) -> None:
        token = SecretCipher([new_key()]).encrypt("x")
        with pytest.raises(SecretError, match="chave incorreta"):
            SecretCipher([new_key()]).decrypt(token)

    def test_rotation(self) -> None:
        old, new = new_key(), new_key()
        token = SecretCipher([old]).encrypt("valor")
        rotated = SecretCipher([new, old]).rotate(token)
        assert SecretCipher([new]).decrypt(rotated) == "valor"
        with pytest.raises(SecretError):
            SecretCipher([old]).decrypt(rotated)
        with pytest.raises(SecretError):
            SecretCipher([new_key()]).rotate(token)


class TestVault:
    def test_set_get_and_update(self, vault: SecretVault) -> None:
        assert vault.set("TOKEN", "v1") is True
        assert vault.get("TOKEN") == "v1"
        assert vault.set("TOKEN", "v2") is False
        assert vault.get("TOKEN") == "v2"
        assert vault.get("TOKEN", project="api") is None
        assert vault.get("OUTRO") is None

    def test_values_are_encrypted_at_rest(self, vault: SecretVault, database: Database) -> None:
        vault.set("TOKEN", "valor-super-secreto")
        with database.session() as session:
            row = session.scalars(select(SecretRow)).one()
        assert "valor-super-secreto" not in row.ciphertext
        assert row.project == ""

    def test_resolve_precedence(self, vault: SecretVault) -> None:
        vault.set("A", "global-a")
        vault.set("B", "global-b")
        vault.set("B", "api-b", project="api")
        vault.set("C", "api-c", project="api")
        vault.set("C", "web-c", project="web")

        assert vault.resolve("api") == {"A": "global-a", "B": "api-b", "C": "api-c"}
        assert vault.resolve("web") == {"A": "global-a", "B": "global-b", "C": "web-c"}
        assert vault.resolve("outro") == {"A": "global-a", "B": "global-b"}

    def test_list_secrets_never_exposes_values(self, vault: SecretVault) -> None:
        vault.set("GLOBAL", "g")
        vault.set("API_ONLY", "a", project="api")
        vault.set("WEB_ONLY", "w", project="web")

        everything = vault.list_secrets()
        assert [(item.name, item.project) for item in everything] == [
            ("GLOBAL", None),
            ("API_ONLY", "api"),
            ("WEB_ONLY", "web"),
        ]
        assert [item.name for item in vault.list_secrets("api")] == ["GLOBAL", "API_ONLY"]
        assert not hasattr(everything[0], "value")
        assert everything[0].created_at.tzinfo is not None

    def test_delete(self, vault: SecretVault) -> None:
        vault.set("TOKEN", "x", project="api")
        assert vault.delete("TOKEN") is False
        assert vault.delete("TOKEN", project="api") is True
        assert vault.delete("TOKEN", project="api") is False

    @pytest.mark.parametrize("name", ["1TOKEN", "com-hifen", "com espaço", ""])
    def test_invalid_names(self, vault: SecretVault, name: str) -> None:
        with pytest.raises(SecretError, match="nome de secret inválido"):
            vault.set(name, "x")

    def test_rotate_all(self, database: Database) -> None:
        old, new = new_key(), new_key()
        SecretVault(database, SecretCipher([old])).set("A", "1")
        SecretVault(database, SecretCipher([old])).set("B", "2", project="api")

        assert SecretVault(database, SecretCipher([new, old])).rotate() == 2
        assert SecretVault(database, SecretCipher([new])).resolve("api") == {"A": "1", "B": "2"}

    def test_resolve_with_wrong_key(self, database: Database) -> None:
        SecretVault(database, SecretCipher([new_key()])).set("A", "1")
        with pytest.raises(SecretError):
            SecretVault(database, SecretCipher([new_key()])).resolve("api")


def test_secret_provider_from_settings(tmp_path: Path) -> None:
    key = new_key()
    settings = Settings(data_dir=tmp_path, secret_keys=key)
    repository = build_repository(settings)
    assert repository is not None
    SecretVault(repository.database, SecretCipher([key])).set("TOKEN", "do-cofre")

    provider = build_secret_provider(settings, repository)

    assert provider is not None
    assert provider("qualquer") == {"TOKEN": "do-cofre"}
    assert build_secret_provider(Settings(data_dir=tmp_path), repository) is None
    assert build_secret_provider(settings, None) is None
    repository.database.dispose()


def test_invalid_key_in_settings_fails_fast(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, secret_keys="invalida")
    repository = build_repository(settings)
    assert repository is not None
    with pytest.raises(SecretError):
        build_secret_provider(settings, repository)
    repository.database.dispose()
