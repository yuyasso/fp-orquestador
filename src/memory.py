"""
Memoria persistente del proyecto.
Lee los archivos markdown de docs/memory/ en el repo del proyecto de trading
y los formatea como contexto para los agentes.

En Sub-fase 2.1 (actual): solo LECTURA.
La escritura llegará en Sub-fase 2.3.
"""
import logging
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


MEMORY_FILES = [
    "project.md",
    "roadmap.md",
    "decisions.md",
    "strategies_tested.md",
    "current_task.md",
]


def _memory_dir() -> Path:
    return settings.trading_repo_path / "docs" / "memory"


def read_file(name: str) -> str:
    """Lee un archivo de memoria por nombre. Devuelve cadena vacía si no existe."""
    path = _memory_dir() / name
    if not path.exists():
        logger.warning(f"Archivo de memoria no encontrado: {path}")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.exception(f"Error leyendo {path}: {e}")
        return ""


def read_all() -> dict[str, str]:
    """Lee todos los archivos de memoria. Devuelve {nombre: contenido}."""
    return {name: read_file(name) for name in MEMORY_FILES}


def format_as_context() -> str:
    """
    Formatea la memoria completa como un bloque de contexto para inyectar
    en el prompt de los agentes.
    """
    parts = []
    parts.append("=" * 70)
    parts.append("MEMORIA DEL PROYECTO (leer antes de responder)")
    parts.append("=" * 70)
    parts.append("")

    contents = read_all()
    for name in MEMORY_FILES:
        content = contents.get(name, "").strip()
        if not content:
            continue
        parts.append(f"--- {name} ---")
        parts.append(content)
        parts.append("")

    parts.append("=" * 70)
    parts.append("FIN DE LA MEMORIA")
    parts.append("=" * 70)

    return "\n".join(parts)
