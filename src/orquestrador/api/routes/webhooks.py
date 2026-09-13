"""Rota que recebe webhooks do GitHub e do GitLab.

Ordem de processamento (a assinatura é validada antes de qualquer ação):

1. Lê o corpo bruto com limite de tamanho (413) e faz o parse do JSON (400).
2. Identifica o repositório e o projeto configurado (404).
3. Valida assinatura/token com o segredo do projeto (401).
4. ``ping`` -> ``pong``; eventos irrelevantes -> ``ignored`` (200).
5. Enfileira a execução no dispatcher e responde ``queued`` (202).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from orquestrador.api.deps import get_app_settings, get_dispatcher, get_registry
from orquestrador.config import Settings
from orquestrador.dispatch import Dispatcher, RunRequest
from orquestrador.projects import ProjectRegistry, Provider
from orquestrador.webhooks import ADAPTERS, EventIgnored, InvalidPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookResponse(BaseModel):
    """Resposta a um webhook.

    Attributes:
        status: ``queued`` (execução agendada), ``ignored`` ou ``pong``.
        project: Projeto correspondente.
        run_id: ID da execução agendada.
        reason: Motivo, quando ignorado.
    """

    status: Literal["queued", "ignored", "pong"]
    project: str | None = None
    run_id: str | None = None
    reason: str | None = None


class ErrorResponse(BaseModel):
    """Formato padrão de erro do FastAPI."""

    detail: str


async def read_json_body(request: Request, limit: int) -> tuple[bytes, dict[str, Any]]:
    """Lê o corpo respeitando o limite de tamanho e faz o parse do JSON.

    Args:
        request: Requisição recebida.
        limit: Tamanho máximo em bytes.

    Returns:
        Tupla ``(corpo bruto, payload)``.

    Raises:
        HTTPException: 413 se exceder o limite; 400 se não for um objeto JSON.
    """
    too_large = HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, f"corpo excede o limite de {limit} bytes"
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise too_large
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "corpo não é um JSON válido") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "o payload deve ser um objeto JSON")
    return bytes(body), payload


@router.post(
    "/{provider}",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": WebhookResponse, "description": "Evento ignorado ou ping"},
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def receive_webhook(
    provider: Provider,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
    registry: Annotated[ProjectRegistry, Depends(get_registry)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> WebhookResponse:
    """Recebe um webhook, valida sua autenticidade e agenda a execução do pipeline."""
    adapter = ADAPTERS[provider]
    body, payload = await read_json_body(request, settings.max_webhook_body_bytes)

    repository = adapter.repository_name(payload)
    if repository is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "não foi possível identificar o repositório no payload"
        )
    project = registry.find(provider, repository)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"nenhum projeto configurado para {provider}:{repository}"
        )
    if not adapter.verify(project.secret.get_secret_value(), body, request.headers):
        logger.warning("assinatura inválida em webhook de %s:%s", provider, repository)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "assinatura do webhook inválida")

    event_name = adapter.event_name(request.headers)
    if not event_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cabeçalho com o tipo de evento ausente")
    if event_name in adapter.ping_events:
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(status="pong", project=project.name)

    try:
        trigger = adapter.parse_event(event_name, payload, adapter.delivery_id(request.headers))
    except EventIgnored as exc:
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(status="ignored", project=project.name, reason=exc.reason)
    except InvalidPayload as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    run_request = RunRequest(project=project.name, trigger=trigger)
    try:
        run_id = await run_in_threadpool(dispatcher.dispatch, run_request)
    except Exception as exc:
        logger.exception("falha ao agendar execução do projeto %s", project.name)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "não foi possível agendar a execução"
        ) from exc

    logger.info("[%s] execução agendada para %s (%s)", run_id, project.name, event_name)
    return WebhookResponse(status="queued", project=project.name, run_id=run_id)
