"""Dashboard web: páginas HTML (Jinja2 + HTMX) e atualizações ao vivo via WebSocket."""

from orquestrador.web import live, pages
from orquestrador.web.paths import STATIC_DIR, TEMPLATES_DIR

__all__ = ["STATIC_DIR", "TEMPLATES_DIR", "live", "pages"]
