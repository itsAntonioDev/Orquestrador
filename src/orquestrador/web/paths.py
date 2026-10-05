"""Caminhos dos recursos do dashboard distribuídos com o pacote."""

from pathlib import Path

#: Jinja2 templates directory.
TEMPLATES_DIR = Path(__file__).parent / "templates"
#: Static files directory (CSS, JS).
STATIC_DIR = Path(__file__).parent / "static"
