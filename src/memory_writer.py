"""
Escritor de memoria persistente.

Append-only sobre los archivos de memoria del proyecto de trading.
Disparado por hitos del flujo:
- Validación del Jefe en REVIEW -> decisions.md
- Aceptación del PO en ACCEPTANCE -> strategies_tested.md + current_task.md + roadmap.md

Filosofía:
- Append-only por defecto (nunca sobrescribimos historia).
- Excepción controlada: current_task.md sí se sobrescribe (refleja el estado actual).
- Todo timestamp en formato ISO local.
- Errores se loguean pero NO rompen el flujo del orquestador.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


def _memory_root() -> Path:
    return Path(settings.trading_repo_path).expanduser().resolve() / "docs" / "memory"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _append_safely(file_path: Path, content: str) -> bool:
    """Append a un archivo, creándolo si no existe. Devuelve True si OK."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")
        with file_path.open("a", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception:
        logger.exception(f"Error escribiendo en {file_path}")
        return False


def _overwrite_safely(file_path: Path, content: str) -> bool:
    """Sobrescribe un archivo entero. Devuelve True si OK."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        logger.exception(f"Error sobrescribiendo {file_path}")
        return False


def record_jefe_validation(po_synthesis: str, jefe_verdict: str) -> bool:
    """
    Tras [VALIDADO] del Jefe en REVIEW, registra la decisión validada en decisions.md.
    """
    path = _memory_root() / "decisions.md"
    entry = (
        f"\n## {_now_iso()} — Decisión validada por el Jefe\n\n"
        f"### Síntesis del Product Owner\n\n"
        f"{po_synthesis.strip()}\n\n"
        f"### Verdict del Jefe de Proyecto\n\n"
        f"{jefe_verdict.strip()}\n\n"
        f"---\n"
    )
    ok = _append_safely(path, entry)
    if ok:
        logger.info(f"decisions.md actualizado con nueva decisión validada")
    return ok


def record_sprint_acceptance(
    tl_planning: str,
    tl_report: str,
    po_acceptance: str,
    execution_session_id: str,
) -> bool:
    """
    Tras [ACEPTADO] del PO en ACCEPTANCE, registra el sprint completado:
    - strategies_tested.md (append): qué se construyó, validaciones, sesión.
    - current_task.md (overwrite): vaciar tarea actual.
    - roadmap.md (append): nueva entrada en histórico de sprints.

    Devuelve True si las 3 escrituras tuvieron éxito.
    """
    now = _now_iso()
    ok_all = True

    # 1) strategies_tested.md
    strategies_path = _memory_root() / "strategies_tested.md"
    strategies_entry = (
        f"\n## {now} — Sprint completado y aceptado\n\n"
        f"### Plan del Tech Lead\n\n"
        f"{tl_planning.strip()[:2000]}\n\n"
        f"### Reporte de entrega del Tech Lead\n\n"
        f"{tl_report.strip()}\n\n"
        f"### Aceptación del Product Owner\n\n"
        f"{po_acceptance.strip()}\n\n"
        f"_Sesión de ejecución de Claude Code: `{execution_session_id[:8]}`_\n\n"
        f"---\n"
    )
    if not _append_safely(strategies_path, strategies_entry):
        ok_all = False

    # 2) current_task.md (overwrite con plantilla vacía)
    current_path = _memory_root() / "current_task.md"
    current_content = (
        f"# Tarea actual\n\n"
        f"_Última actualización: {now}_\n\n"
        f"**Estado:** ninguna tarea activa.\n\n"
        f"El último sprint completado se ha registrado en `strategies_tested.md` y `roadmap.md`.\n"
        f"Esperando próxima decisión del equipo sobre el siguiente sprint.\n"
    )
    if not _overwrite_safely(current_path, current_content):
        ok_all = False

    # 3) roadmap.md (insertar bajo "## Completado")
    roadmap_path = _memory_root() / "roadmap.md"
    plan_summary = _extract_plan_objective(tl_planning)
    roadmap_entry = (
        f"- **{now}** — Sprint aceptado · "
        f"Sesión `{execution_session_id[:8]}` · "
        f"{plan_summary}\n"
    )
    if not _insert_under_section(roadmap_path, "Completado", roadmap_entry):
        ok_all = False

    logger.info(f"Memoria actualizada tras aceptación del PO (ok_all={ok_all})")
    return ok_all


def _extract_plan_objective(plan_text: str) -> str:
    """
    Extrae el resumen del plan buscando la linea que sigue al marcador "Objetivo:".
    Si no lo encuentra, cae a la primera linea no-header significativa.
    """
    lines = plan_text.strip().split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        lower = stripped.lower().lstrip("*").rstrip("*").strip()
        if lower.startswith("objetivo:") or lower.startswith("objetivo "):
            after_colon = stripped.split(":", 1)
            if len(after_colon) > 1 and after_colon[1].strip():
                return after_colon[1].strip()[:200]
            for next_line in lines[i + 1:]:
                if next_line.strip():
                    return next_line.strip()[:200]

    for line in lines:
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and not stripped.startswith("---")
            and len(stripped) > 20
        ):
            return stripped[:200]
    return "(sin resumen extraible)"


def _insert_under_section(file_path: Path, section_name: str, entry: str) -> bool:
    """
    Inserta `entry` justo despues de la linea "## <section_name>" en el archivo.
    Si la seccion no existe, hace append al final.
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")

        current = file_path.read_text(encoding="utf-8")
        lines = current.split("\n")
        section_marker = f"## {section_name}"

        section_index = -1
        for idx, line in enumerate(lines):
            if line.strip() == section_marker:
                section_index = idx
                break

        if section_index == -1:
            new_content = current.rstrip() + f"\n\n## {section_name}\n{entry}"
        else:
            insert_at = section_index + 1
            j = insert_at
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("(") and lines[j].strip().endswith(")"):
                del lines[j]
            new_lines = lines[:insert_at] + ["", entry.rstrip()] + lines[insert_at:]
            new_content = "\n".join(new_lines)

        file_path.write_text(new_content, encoding="utf-8")
        return True
    except Exception:
        logger.exception(f"Error insertando en seccion {section_name} de {file_path}")
        return False
