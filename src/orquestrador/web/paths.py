"""Caminhos dos recursos do dashboard distribuídos com o pacote."""

from pathlib import Path

#: Diretório dos templates Jinja2.
TEMPLATES_DIR = Path(__file__).parent / "templates"
#: Diretório dos arquivos estáticos (CSS, JS).
STATIC_DIR = Path(__file__).parent / "static"
