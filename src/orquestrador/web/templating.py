"""Instância Jinja2 do dashboard, com filtros e variáveis globais."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from orquestrador import __version__
from orquestrador.web import formatting
from orquestrador.web.paths import TEMPLATES_DIR

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(
    status_label=formatting.status_label,
    status_class=formatting.status_class,
    duration=formatting.format_duration,
    timestamp=formatting.format_timestamp,
    iso=formatting.iso_timestamp,
    short_sha=formatting.short_sha,
)
templates.env.globals.update(
    version=__version__,
    status_labels=formatting.STATUS_LABELS,
)
