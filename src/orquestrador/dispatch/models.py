"""Mensagens trocadas entre quem recebe o evento (API) e quem executa (worker)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from orquestrador.execution.context import new_run_id
from orquestrador.execution.results import PipelineResult, utcnow
from orquestrador.webhooks.events import TriggerEvent

RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class RunRequest(BaseModel):
    """Pedido de execução de um pipeline. Serializável em JSON (vai para a fila).

    Attributes:
        run_id: Identificador da execução (também nomeia o workspace).
        project: Nome do projeto no registro.
        trigger: Evento que motivou a execução.
        requested_at: Momento do pedido.
    """

    run_id: str = Field(default_factory=new_run_id, pattern=RUN_ID_PATTERN)
    project: str
    trigger: TriggerEvent
    requested_at: datetime = Field(default_factory=utcnow)


class RunOutcome(BaseModel):
    """Desfecho do processamento de um ``RunRequest``.

    Attributes:
        run_id: Identificador da execução.
        project: Nome do projeto.
        status: ``completed`` (pipeline rodou, com sucesso ou não), ``skipped``
            (gatilhos não correspondem) ou ``error`` (checkout/pipeline inválido).
        reason: Explicação para ``skipped``/``error``.
        result: Resultado do pipeline, quando executado.
    """

    run_id: str
    project: str
    status: Literal["completed", "skipped", "error"]
    reason: str | None = None
    result: PipelineResult | None = None

    @property
    def final_status(self) -> str:
        """Status final: o do pipeline quando executado; senão ``skipped`` ou ``error``."""
        if self.status == "completed" and self.result is not None:
            return self.result.status.value
        return "skipped" if self.status == "skipped" else "error"
