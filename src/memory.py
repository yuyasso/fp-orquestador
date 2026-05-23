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


# ============================================================
# Estado vivo del repo: tree + lectura de archivos clave
# ============================================================

import subprocess as _subprocess
from pathlib import Path as _Path


def _build_tree_text(repo_root: _Path, max_depth: int = 4) -> str:
    """Genera un tree simplificado del repo, ignorando ruido."""
    if not repo_root.exists():
        return f"(repo no encontrado en {repo_root})"

    ignore_patterns = [
        ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".venv", "venv", "*.egg-info", "node_modules", ".coverage",
        "build", "dist", "data/raw",
    ]
    ignore_arg = "|".join(ignore_patterns)

    try:
        result = _subprocess.run(
            ["tree", "-L", str(max_depth), "-I", ignore_arg, "--noreport", str(repo_root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, _subprocess.TimeoutExpired):
        pass

    # Fallback: walk manual si no hay tree instalado
    lines = [str(repo_root)]
    for path in sorted(repo_root.rglob("*")):
        rel = path.relative_to(repo_root)
        parts = rel.parts
        if any(ignore in str(rel) for ignore in [".git", "__pycache__", ".pytest_cache",
                                                    ".mypy_cache", ".ruff_cache", ".venv",
                                                    "egg-info", ".coverage"]):
            continue
        if len(parts) > max_depth:
            continue
        depth = len(parts) - 1
        indent = "  " * depth
        lines.append(f"{indent}{parts[-1]}")
    return "\n".join(lines)


def _read_pyproject_summary(repo_root: _Path) -> str:
    """Lee pyproject.toml si existe."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return "(sin pyproject.toml)"
    try:
        return pyproject.read_text(encoding="utf-8")[:2000]
    except Exception:
        return "(error leyendo pyproject.toml)"


def get_repo_state_for_planning() -> str:
    """
    Snapshot del estado real del repo de trading para inyectar en el prompt del TL en PLANNING.
    Incluye:
    - Tree de carpetas (hasta 4 niveles, sin ruido).
    - pyproject.toml (para que sepa qué dependencias hay).

    Esto evita que el TL invente rutas como `domain/ports/data_source.py` cuando debe ser
    `src/trading/ports/data_source.py`.
    """
    from src.config import settings as _s

    repo_root = _Path(_s.trading_repo_path).expanduser().resolve()
    tree_text = _build_tree_text(repo_root)
    pyproject = _read_pyproject_summary(repo_root)

    return (
        "=== ESTADO REAL DEL REPO fp-trading-system ===\n"
        "Cuando planifiques rutas de archivos, USA estas rutas reales. "
        "No inventes nuevas estructuras: extiende lo que ya hay.\n\n"
        "## Estructura de carpetas (tree)\n"
        f"```\n{tree_text}\n```\n\n"
        "## pyproject.toml\n"
        f"```toml\n{pyproject}\n```\n"
    )
