"""Formatação de valores exibidos no dashboard (usada como filtros Jinja2)."""

from __future__ import annotations

from datetime import UTC, datetime

from orquestrador.db.schemas import RunStatus

#: Rótulos em português de cada status.
STATUS_LABELS: dict[str, str] = {
    RunStatus.PENDING.value: "Pendente",
    RunStatus.QUEUED.value: "Na fila",
    RunStatus.RUNNING.value: "Executando",
    RunStatus.SUCCESS.value: "Sucesso",
    RunStatus.FAILURE.value: "Falhou",
    RunStatus.SKIPPED.value: "Ignorado",
    RunStatus.CANCELLED.value: "Cancelado",
    RunStatus.ERROR.value: "Erro",
}

#: Status que fazem sentido como filtro da listagem (execuções nunca ficam "pending").
FILTERABLE_STATUSES: list[RunStatus] = [
    status for status in RunStatus if status != RunStatus.PENDING
]


def status_label(status: RunStatus | str) -> str:
    """Rótulo legível de um status."""
    value = status.value if isinstance(status, RunStatus) else str(status)
    return STATUS_LABELS.get(value, value)


def status_class(status: RunStatus | str) -> str:
    """Classe CSS de um status (``status-success``, ``status-failure``...)."""
    value = status.value if isinstance(status, RunStatus) else str(status)
    return f"status-{value}"


def format_duration(seconds: float | None) -> str:
    """Duração compacta: ``0.4s``, ``12s``, ``3m 07s``, ``1h 05m``."""
    if seconds is None:
        return "-"
    if seconds < 10:
        return f"{seconds:.1f}s"
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_timestamp(value: datetime | None) -> str:
    """Data/hora em UTC (o JavaScript converte para o fuso do navegador)."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def iso_timestamp(value: datetime | None) -> str:
    """Data/hora ISO 8601 para o atributo ``datetime`` de ``<time>``."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def short_sha(sha: str | None) -> str:
    """Os 8 primeiros caracteres de um SHA."""
    return (sha or "")[:8] or "-"
