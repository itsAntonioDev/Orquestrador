"""Conteúdo das notificações: resumo da execução em texto, HTML e blocos do Slack."""

from __future__ import annotations

import html
from typing import Any

from pydantic import BaseModel

from orquestrador.dispatch.models import RunOutcome, RunRequest
from orquestrador.execution.results import Status

STATUS_LABELS = {
    "success": "Sucesso",
    "failure": "Falhou",
    "cancelled": "Cancelado",
    "skipped": "Ignorado",
    "error": "Erro",
}

STATUS_EMOJI = {
    "success": ":white_check_mark:",
    "failure": ":x:",
    "cancelled": ":no_entry_sign:",
    "skipped": ":fast_forward:",
    "error": ":rotating_light:",
}

STATUS_COLORS = {
    "success": "#2b8a3e",
    "failure": "#c92a2a",
    "cancelled": "#7048e8",
    "skipped": "#e67700",
    "error": "#a61e4d",
}


class FailedJob(BaseModel):
    """Job que falhou, com o motivo."""

    name: str
    error: str | None = None


class RunNotification(BaseModel):
    """Resumo de uma execução enviado pelos canais de notificação."""

    run_id: str
    project: str
    status: str
    pipeline: str | None = None
    reason: str | None = None
    repository: str | None = None
    event: str | None = None
    ref: str | None = None
    commit: str | None = None
    actor: str | None = None
    message: str | None = None
    duration: float | None = None
    url: str | None = None
    failed_jobs: list[FailedJob] = []

    @property
    def status_label(self) -> str:
        """Rótulo do status em português."""
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def title(self) -> str:
        """Título curto: ``Falhou: api / ci``."""
        name = f"{self.project} / {self.pipeline}" if self.pipeline else self.project
        return f"{self.status_label}: {name}"


def format_duration(seconds: float | None) -> str:
    """Duração legível (``45s``, ``3m 07s``)."""
    if seconds is None:
        return "-"
    total = round(seconds)
    if total < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def build_notification(
    outcome: RunOutcome, request: RunRequest | None, public_url: str | None = None
) -> RunNotification:
    """Monta a notificação a partir do desfecho (e do pedido, quando disponível)."""
    trigger = request.trigger if request else None
    failed_jobs: list[FailedJob] = []
    if outcome.result is not None:
        for job in outcome.result.jobs.values():
            if job.status in {Status.FAILURE, Status.CANCELLED} and not job.allowed_failure:
                step_error = next(
                    (step.error for step in job.steps if step.status == Status.FAILURE),
                    None,
                )
                failed_jobs.append(FailedJob(name=job.name, error=step_error or job.error))
    return RunNotification(
        run_id=outcome.run_id,
        project=outcome.project,
        status=outcome.final_status,
        pipeline=outcome.result.pipeline if outcome.result else None,
        reason=outcome.reason,
        repository=trigger.repository if trigger else None,
        event=trigger.event if trigger else None,
        ref=(trigger.branch or trigger.tag) if trigger else None,
        commit=trigger.commit if trigger else None,
        actor=trigger.actor if trigger else None,
        message=trigger.message if trigger else None,
        duration=outcome.result.duration if outcome.result else None,
        url=f"{public_url.rstrip('/')}/runs/{outcome.run_id}" if public_url else None,
        failed_jobs=failed_jobs,
    )


def _details(notification: RunNotification) -> list[tuple[str, str]]:
    pairs = [
        ("Repositório", notification.repository),
        ("Evento", notification.event),
        ("Ref", notification.ref),
        ("Commit", (notification.commit or "")[:10] or None),
        ("Autor", notification.actor),
        ("Duração", format_duration(notification.duration) if notification.duration else None),
        ("Execução", notification.run_id),
    ]
    return [(label, value) for label, value in pairs if value]


def render_text(notification: RunNotification) -> str:
    """Corpo em texto puro."""
    lines = [notification.title, ""]
    if notification.message:
        lines += [notification.message, ""]
    lines += [f"{label}: {value}" for label, value in _details(notification)]
    if notification.reason:
        lines += ["", f"Motivo: {notification.reason}"]
    if notification.failed_jobs:
        lines += ["", "Jobs com falha:"]
        lines += [f"  - {job.name}: {job.error or 'falhou'}" for job in notification.failed_jobs]
    if notification.url:
        lines += ["", f"Detalhes: {notification.url}"]
    return "\n".join(lines) + "\n"


def render_html(notification: RunNotification) -> str:
    """Corpo em HTML (todo conteúdo dinâmico é escapado)."""
    escape = html.escape
    color = STATUS_COLORS.get(notification.status, "#495057")
    rows = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#666'>{escape(label)}</td>"
        f"<td>{escape(value)}</td></tr>"
        for label, value in _details(notification)
    )
    parts = [
        "<div style='font-family:sans-serif;font-size:14px'>",
        f"<h2 style='color:{color};margin:0 0 8px'>{escape(notification.title)}</h2>",
    ]
    if notification.message:
        parts.append(f"<p style='color:#444'>{escape(notification.message)}</p>")
    parts.append(f"<table>{rows}</table>")
    if notification.reason:
        parts.append(f"<p><strong>Motivo:</strong> {escape(notification.reason)}</p>")
    if notification.failed_jobs:
        items = "".join(
            f"<li><strong>{escape(job.name)}</strong>: {escape(job.error or 'falhou')}</li>"
            for job in notification.failed_jobs
        )
        parts.append(f"<p>Jobs com falha:</p><ul>{items}</ul>")
    if notification.url:
        url = escape(notification.url, quote=True)
        parts.append(f"<p><a href='{url}'>Ver execução no dashboard</a></p>")
    parts.append("</div>")
    return "".join(parts)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_payload(
    notification: RunNotification, *, channel: str | None = None, username: str | None = None
) -> dict[str, Any]:
    """Payload de *Incoming Webhook* com Block Kit e texto de fallback."""
    emoji = STATUS_EMOJI.get(notification.status, ":information_source:")
    header = f"{emoji} *{_slack_escape(notification.title)}*"
    if notification.message:
        header += f"\n{_slack_escape(notification.message)}"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{label}*\n{_slack_escape(value)}"}
                for label, value in _details(notification)
            ],
        },
    ]
    if notification.reason:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Motivo: {_slack_escape(notification.reason)}"}
                ],
            }
        )
    if notification.failed_jobs:
        failed = "\n".join(
            f"• *{_slack_escape(job.name)}*: {_slack_escape(job.error or 'falhou')}"
            for job in notification.failed_jobs
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": failed}})
    if notification.url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Ver execução"},
                        "url": notification.url,
                    }
                ],
            }
        )
    payload: dict[str, Any] = {"text": notification.title, "blocks": blocks}
    if channel:
        payload["channel"] = channel
    if username:
        payload["username"] = username
    return payload
