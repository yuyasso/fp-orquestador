"""
Director: agente autónomo que decide qué hacer tras cada sprint aceptado.

Se invoca tras [ACEPTADO] del PO en ACCEPTANCE. Recibe contexto completo del
sprint (plan, reporte, aceptación) + memoria del proyecto y decide:
- continue: lanza siguiente sprint con un mensaje exacto a publicar en #lobby.
- pause: detiene la cadena autónoma, espera input humano.
- stop: fin del experimento.

Solo se invoca si autonomous_mode está activo. El humano siempre puede
desactivarlo con /auto off en cualquier momento.
"""
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.claude_runner import run_claude
from src.config import settings

logger = logging.getLogger(__name__)


DIRECTOR_SYSTEM_PROMPT = """Eres el Director del equipo de trading. Tu rol es decidir
QUÉ HACE EL EQUIPO A CONTINUACIÓN tras cerrar un sprint, sustituyendo la decisión
del humano cuando opera en MODO AUTÓNOMO.

Tu única salida es un JSON con la siguiente estructura EXACTA, sin texto adicional,
sin markdown, sin preámbulo:

{
  "action": "continue" | "pause" | "stop",
  "next_message": "<mensaje exacto a publicar en #lobby si action=continue>",
  "reasoning": "<por qué decides esto, max 300 chars>",
  "confidence": "high" | "medium" | "low"
}

---

CRITERIOS DE DECISIÓN

Decide "continue" si:
- El último sprint fue ACEPTADO sin reservas y el PO o TL proponen claramente un siguiente paso natural.
- El siguiente paso es incremental y bajo riesgo (no bloquea capital real, no toca componentes críticos sin validación).
- La memoria del proyecto y el roadmap apoyan el siguiente paso.

Decide "pause" si:
- El PO o TL NO proponen un siguiente paso claro (señal de fin de iteración).
- El último sprint introduce deuda técnica que requiere decisión humana sobre prioridad.
- Hay ambigüedad estratégica sobre si seguir explorando, rediseñar o cambiar de hipótesis.
- Tu confianza en el siguiente paso obvio es baja.
- Detectas patrones de bucle (mismo tipo de sprint sin progreso real).

Decide "stop" SIEMPRE si:
- El último sprint emitió paper_trading_authorized=True (decisión que requiere humano siempre).
- Los criterios de éxito del proyecto definidos en project.md se han cumplido.
- El último sprint emitió stop_triggered=True en walk-forward o evaluación de estrategia.
- Hay un error grave que requiere revisión humana del sistema, no del proyecto.

---

REGLAS ESTRICTAS

1. Tu trabajo NO es decidir contenido técnico del siguiente sprint. Eso lo hace el equipo (A1/A2 debaten, PO sintetiza, etc.). Tu trabajo es solo decidir SI continuar y QUÉ pregunta lanzar al equipo para arrancar el siguiente sprint.

2. El campo next_message debe ser un mensaje conciso y bien dirigido (típicamente empezando con "A1, A2:" para forzar ruta analítica, o "TL:" para tareas técnicas concretas), tal y como lo escribiría el humano para arrancar un sprint.

3. NUNCA inventes resultados, métricas, o conclusiones sobre el código. Solo razonas sobre lo que el equipo dijo en el sprint anterior.

4. Si dudas, "pause". Es la opción segura. El humano puede revisar y decidir.

5. confidence: "high" si tienes claridad total sobre el siguiente paso natural. "medium" si hay varias opciones razonables. "low" si la ambigüedad es alta.

6. Responde SOLO con el JSON. Nada más."""


@dataclass
class DirectorDecision:
    action: str  # "continue" | "pause" | "stop"
    next_message: str
    reasoning: str
    confidence: str  # "high" | "medium" | "low"
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


VALID_ACTIONS = {"continue", "pause", "stop"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _strip_markdown_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _read_memory_files() -> str:
    """Lee los 5 archivos de memoria del proyecto y los concatena."""
    memory_dir = Path(settings.trading_repo_path).expanduser() / "docs" / "memory"
    if not memory_dir.exists():
        return "(memoria no encontrada)"

    files = ["project.md", "roadmap.md", "decisions.md", "current_task.md", "strategies_tested.md"]
    parts = []
    for fname in files:
        fpath = memory_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8")
                # Truncar archivos muy largos para no inflar el prompt
                if len(content) > 8000:
                    content = content[:4000] + "\n\n... (truncado) ...\n\n" + content[-3000:]
                parts.append(f"### {fname}\n\n{content}")
            except Exception:
                logger.exception(f"Error leyendo {fpath}")
    return "\n\n---\n\n".join(parts) if parts else "(memoria vacía)"


async def decide_next_action(
    tl_planning: str,
    tl_report: str,
    po_acceptance: str,
    sprints_in_autonomous_chain: int,
    accumulated_cost_eur: float,
    max_chain_sprints: int,
    max_chain_cost_eur: float,
) -> DirectorDecision:
    """
    Invoca al Director para decidir si continuar la cadena autónoma.

    Hard limits aplicados ANTES de invocar al modelo:
    - sprints_in_autonomous_chain >= max_chain_sprints → pause forzoso.
    - accumulated_cost_eur >= max_chain_cost_eur → pause forzoso.

    Si esos pasan, el Director razona sobre contenido.
    """
    # Hard limits que no necesitan al modelo
    if sprints_in_autonomous_chain >= max_chain_sprints:
        return DirectorDecision(
            action="pause",
            next_message="",
            reasoning=f"Hard limit: {sprints_in_autonomous_chain} sprints autónomos consecutivos alcanzados (max {max_chain_sprints}). Pausa para revisión humana.",
            confidence="high",
        )

    if accumulated_cost_eur >= max_chain_cost_eur:
        return DirectorDecision(
            action="pause",
            next_message="",
            reasoning=f"Hard limit: {accumulated_cost_eur:.2f}€ gastados en cadena autónoma (max {max_chain_cost_eur:.2f}€). Pausa para revisión humana.",
            confidence="high",
        )

    # Hard stop si paper_trading aprobado (decisión crítica siempre humana)
    if "paper_trading_authorized: True" in po_acceptance or "paper_trading_authorized=True" in po_acceptance:
        return DirectorDecision(
            action="stop",
            next_message="",
            reasoning="paper_trading_authorized=True detectado. Decisión crítica de capital, requiere humano siempre.",
            confidence="high",
        )

    # Hard stop si stop_triggered (señal de fallo de estrategia que requiere reorientación humana)
    if "stop_triggered: True" in po_acceptance or "stop_triggered=True" in po_acceptance:
        return DirectorDecision(
            action="stop",
            next_message="",
            reasoning="stop_triggered=True detectado. La estrategia ha fallado los gates de validación, decisión de rediseño es humana.",
            confidence="high",
        )

    # Si pasamos los hard limits, invocamos al modelo para razonar
    memory_context = _read_memory_files()

    prompt = (
        f"### CONTEXTO DEL SPRINT QUE ACABA DE CERRARSE\n\n"
        f"**Plan del Tech Lead (PLANNING):**\n{tl_planning.strip()[:3000]}\n\n"
        f"**Reporte de entrega del Tech Lead (REPORTING):**\n{tl_report.strip()[:2000]}\n\n"
        f"**Aceptación del Product Owner (ACCEPTANCE):**\n{po_acceptance.strip()[:2000]}\n\n"
        f"---\n\n"
        f"### ESTADO DEL EXPERIMENTO\n\n"
        f"- Sprints autónomos consecutivos: {sprints_in_autonomous_chain} / {max_chain_sprints}\n"
        f"- Coste acumulado en cadena autónoma: {accumulated_cost_eur:.2f}€ / {max_chain_cost_eur:.2f}€\n\n"
        f"---\n\n"
        f"### MEMORIA DEL PROYECTO\n\n"
        f"{memory_context}\n\n"
        f"---\n\n"
        f"Decide tu siguiente acción siguiendo las reglas del system prompt. "
        f"Responde SOLO con el JSON."
    )

    response = await run_claude(
        prompt=prompt,
        model="sonnet",
        system_prompt=DIRECTOR_SYSTEM_PROMPT,
    )

    raw = _strip_markdown_fences(response.result)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"Director devolvió JSON inválido: {raw[:300]}")
        return DirectorDecision(
            action="pause",
            next_message="",
            reasoning="Parse error del JSON del Director, pauso por seguridad.",
            confidence="low",
            cost_usd=response.cost_usd,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    action = data.get("action", "pause")
    if action not in VALID_ACTIONS:
        logger.warning(f"Director devolvió action inválido: {action}")
        action = "pause"

    confidence = data.get("confidence", "low")
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    # Si confianza baja, forzamos pause
    if confidence == "low" and action == "continue":
        logger.info("Director confianza baja en continue, forzamos pause")
        action = "pause"

    return DirectorDecision(
        action=action,
        next_message=data.get("next_message", "").strip(),
        reasoning=data.get("reasoning", "").strip(),
        confidence=confidence,
        cost_usd=response.cost_usd,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
