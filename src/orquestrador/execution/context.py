"""Contexto de uma execução: metadados do gatilho, workspace e cancelamento."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    """Gera um identificador de execução curto e único."""
    return uuid.uuid4().hex[:12]


@dataclass
class RunContext:
    """Informações sobre *por que* e *onde* um pipeline está rodando.

    Attributes:
        run_id: Identificador único da execução.
        workspace: Diretório de trabalho compartilhado pelos jobs.
        event: Evento que disparou a execução (``push``, ``pull_request``, ``manual``...).
        ref: Referência git completa (ex.: ``refs/heads/main``).
        branch: Nome da branch, se aplicável.
        tag: Nome da tag, se aplicável.
        commit: SHA do commit.
        repository: Nome do repositório (ex.: ``org/projeto``).
        actor: Usuário que disparou a execução.
        base_branch: Branch de destino de um pull/merge request.
        pull_request: Número do pull/merge request.
        env: Variáveis de ambiente que sobrescrevem as do pipeline.
        secrets: Secrets disponíveis para ``${{ secrets.NOME }}`` (nunca exibidos).
        cancel_event: Sinalizado para cancelar a execução em andamento.
    """

    run_id: str = field(default_factory=new_run_id)
    workspace: Path = field(default_factory=Path.cwd)
    event: str = "manual"
    ref: str | None = None
    branch: str | None = None
    tag: str | None = None
    commit: str | None = None
    repository: str | None = None
    actor: str | None = None
    base_branch: str | None = None
    pull_request: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    @property
    def cancelled(self) -> bool:
        """Se o cancelamento foi solicitado."""
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        """Solicita o cancelamento da execução."""
        self.cancel_event.set()

    def builtin_env(self, pipeline_name: str, job_id: str) -> dict[str, str]:
        """Variáveis de ambiente injetadas automaticamente em todo step.

        Args:
            pipeline_name: Nome do pipeline.
            job_id: ID do job em execução.

        Returns:
            Mapeamento com ``CI``, ``ORQ_RUN_ID``, ``ORQ_BRANCH`` etc.
        """
        values = {
            "CI": "true",
            "ORQUESTRADOR": "true",
            "ORQ_RUN_ID": self.run_id,
            "ORQ_PIPELINE": pipeline_name,
            "ORQ_JOB": job_id,
            "ORQ_EVENT": self.event,
            "ORQ_WORKSPACE": str(self.workspace),
            "ORQ_REF": self.ref,
            "ORQ_BRANCH": self.branch,
            "ORQ_TAG": self.tag,
            "ORQ_COMMIT": self.commit,
            "ORQ_REPOSITORY": self.repository,
            "ORQ_ACTOR": self.actor,
            "ORQ_BASE_BRANCH": self.base_branch,
            "ORQ_PULL_REQUEST": None if self.pull_request is None else str(self.pull_request),
        }
        return {key: value for key, value in values.items() if value is not None}

    def condition_variables(self, pipeline_name: str, job_id: str) -> dict[str, Any]:
        """Variáveis disponíveis nas expressões ``if``.

        Args:
            pipeline_name: Nome do pipeline.
            job_id: ID do job avaliado.

        Returns:
            Mapeamento de nomes para valores (ausentes = ``None``).
        """
        return {
            "run_id": self.run_id,
            "pipeline": pipeline_name,
            "job": job_id,
            "event": self.event,
            "ref": self.ref,
            "branch": self.branch,
            "tag": self.tag,
            "commit": self.commit,
            "repository": self.repository,
            "actor": self.actor,
            "base_branch": self.base_branch,
            "pull_request": self.pull_request,
        }
