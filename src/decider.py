"""
Decisor: tiene dos funciones distintas.

1. decide_next (general): decide quién habla cuando NO estamos en una fase específica
   (tras un mensaje humano nuevo). Determina la ruta: shortcut o analítica.

2. decide_within_analysis: decide dentro de la fase ANALYSIS quién habla a continuación
   (A1, A2, jefe en alerta) o si la fase debe cerrarse (pasar a SYNTHESIS).
"""
import json
import logging
from dataclasses import dataclass

from src.claude_runner import run_claude

logger = logging.getLogger(__name__)


DECIDER_SYSTEM_PROMPT = """Eres el Orquestador de un equipo de desarrollo de un sistema de trading. \
Tu única tarea es decidir QUÉ ÚNICO miembro del equipo debe hablar A CONTINUACIÓN, \
o si el turno ya debe cerrarse.

El equipo:
- "jefe" (Jefe de Proyecto): exigente, supervisa calidad global, combate conformismo, interviene en temas estratégicos y riesgos. NO participa en cada mensaje.
- "po" (Product Owner): define alcance, prioriza, valida entregas, sintetiza acuerdos.
- "tl" (Tech Lead): decisiones técnicas, arquitectura, stack, interlocutor con Claude Code.
- "a1" (Analista 1, cuantitativo): estrategias clásicas, rigor estadístico, métricas.
- "a2" (Analista 2, microestructura): régimen de mercado, order flow, contexto macro.

El humano del equipo se llama Fran. Sus mensajes tienen MÁXIMA PRIORIDAD.

---

REGLA 0 (crítica, por encima de todas las demás):
Un mensaje del humano que contiene una pregunta, petición o instrucción NUNCA puede cerrarse con speaker=null argumentando "ya está respondido en el contexto". Si tiene contenido sustantivo, SIEMPRE convocas al rol apropiado.

Solo se cierra con speaker=null un mensaje humano genuinamente trivial: saludo, agradecimiento, reacción corta.

---

Resto de reglas:
1. Pregunta directa a un agente concreto → ese agente.
2. Pregunta humana sobre arquitectura/stack/código → "tl".
3. Pregunta humana sobre alcance/prioridades/MVP → "po".
4. Pregunta humana sobre estrategias de trading → "a1" primero.
5. Pregunta estratégica de alto nivel o detección de conformismo → "jefe".

Tu respuesta DEBE ser un JSON válido con esta estructura EXACTA, sin texto adicional, sin markdown:

{"speaker": "a1", "reasoning": "breve explicación", "needs_clarification": false, "clarification_question": ""}

Si nadie debe responder:
{"speaker": null, "reasoning": "motivo", "needs_clarification": false, "clarification_question": ""}

Si el humano es ambiguo:
{"speaker": null, "reasoning": "ambiguo", "needs_clarification": true, "clarification_question": "¿X o Y?"}

Responde SOLO con el JSON."""


ANALYSIS_DECIDER_SYSTEM_PROMPT = """Eres el Orquestador de un equipo de trading durante la fase ANALYSIS.

En esta fase los analistas A1 y A2 debaten una propuesta. Tu tarea es decidir en cada paso una de estas CUATRO opciones — NADA MÁS:
- ¿Habla A1?
- ¿Habla A2?
- ¿Habla el Jefe de Proyecto (intervención correctiva por conformismo o falta de rigor)?
- ¿O la fase debe cerrarse y pasar a SYNTHESIS (PO sintetiza)?

RESTRICCIÓN ESTRICTA: durante ANALYSIS, NUNCA convocas a "po" ni a "tl". El PO interviene SOLO en su fase SYNTHESIS (gestionada por el sistema, no por ti), y el TL en su fase PLANNING (idem). Si un analista pregunta directamente al PO o al TL ("¿definimos esto, PO?" / "TL, ¿esto es viable?"), NO los convoques — cierra la fase (close_phase) para que el sistema pase a SYNTHESIS donde el PO responderá de forma estructurada. Los analistas no pueden invocar al PO o TL en mitad del debate. Si crees que la fase está agotada porque hay preguntas dirigidas a PO/TL, eso es señal clara de close_phase, no de convocar al PO.

Los analistas:
- "a1": cuantitativo clásico. Mean reversion, momentum, rigor estadístico, métricas.
- "a2": microestructura, régimen de mercado, contexto macro, liquidez.

El Jefe:
- "jefe": interviene SOLO si detecta conformismo, propuesta tibia, o que el equipo se está saltando rigor. NO interviene como turno normal del debate.

CRITERIOS PARA DECIDIR:

1. **Continúa el debate (speaker=a1 o a2)** si:
   - Acaban de plantear un punto nuevo que el otro analista no ha contestado.
   - El último mensaje contiene una pregunta directa al otro analista.
   - Hay desacuerdo activo y la discusión está aportando información nueva.
   - Un analista ha hablado mucho menos que el otro y eso desequilibra el debate.

2. **Convoca al Jefe (speaker=jefe)** si:
   - Detectas tibieza, conformismo o atajos que comprometen el resultado.
   - Los analistas están conformándose con métricas mediocres.
   - Se están saltando algún criterio importante del proyecto (rigor estadístico, walk-forward, etc.).
   - El Jefe ya intervino antes en este debate, NO lo convoques otra vez salvo nueva tibieza.

3. **Cierra la fase (close_phase=true)** si:
   - Hay consenso claro y articulado entre A1 y A2 sobre una propuesta concreta.
   - El debate se está repitiendo sin aportar info nueva (síntoma de estancamiento).
   - Los analistas explícitamente cierran ("estoy de acuerdo", "acepto tu punto", "consenso").
   - Llevamos 6+ intervenciones de analistas y el último mensaje no abre nada nuevo.

NO TE LIMITES POR NÚMERO MÍNIMO de turnos: si en 2 turnos hay consenso claro, cierra. Si necesitan 8 para llegar a algo sólido, deja que sigan. Tu criterio es CALIDAD del debate, no cantidad.

---

Tu respuesta DEBE ser un JSON válido con esta estructura EXACTA:

{"action": "speak", "speaker": "a2", "reasoning": "breve explicación"}

o cuando toca cerrar la fase:

{"action": "close_phase", "speaker": null, "reasoning": "consenso alcanzado / debate estancado"}

Valores válidos para speaker cuando action=speak: "a1", "a2", "jefe".

Responde SOLO con el JSON, sin markdown, sin texto adicional."""


@dataclass
class Decision:
    speaker: str | None = None
    reasoning: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class AnalysisDecision:
    action: str           # "speak" | "close_phase"
    speaker: str | None
    reasoning: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


VALID_ROLES = {"jefe", "po", "tl", "a1", "a2"}
VALID_ANALYSIS_SPEAKERS = {"a1", "a2", "jefe"}


def _strip_markdown_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


async def decide_next(history_text: str) -> Decision:
    """Decisor general (tras mensaje humano nuevo)."""
    prompt = (
        f"Historial reciente del canal (orden cronológico):\n"
        f"{history_text}\n\n"
        f"¿Quién debería hablar a continuación? ¿O cierras el turno?"
    )

    response = await run_claude(
        prompt=prompt,
        model="haiku",
        system_prompt=DECIDER_SYSTEM_PROMPT,
    )

    raw = _strip_markdown_fences(response.result)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"Decisor devolvió JSON inválido: {raw[:300]}")
        return Decision(
            speaker=None,
            reasoning="parse_error",
            needs_clarification=True,
            clarification_question=(
                "No he podido interpretar bien tu mensaje. "
                "¿Puedes reformularlo indicando a qué rol te diriges?"
            ),
            cost_usd=response.cost_usd,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    speaker = data.get("speaker")
    if speaker is not None and speaker not in VALID_ROLES:
        logger.warning(f"Decisor devolvió speaker inválido: {speaker}")
        speaker = None

    return Decision(
        speaker=speaker,
        reasoning=data.get("reasoning", ""),
        needs_clarification=bool(data.get("needs_clarification", False)),
        clarification_question=data.get("clarification_question", ""),
        cost_usd=response.cost_usd,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


async def decide_within_analysis(history_text: str) -> AnalysisDecision:
    """Decisor específico para la fase ANALYSIS."""
    prompt = (
        f"Historial reciente del canal durante la fase ANALYSIS (orden cronológico):\n"
        f"{history_text}\n\n"
        f"¿Quién habla a continuación (a1, a2, jefe), o cierras la fase?"
    )

    response = await run_claude(
        prompt=prompt,
        model="haiku",
        system_prompt=ANALYSIS_DECIDER_SYSTEM_PROMPT,
    )

    raw = _strip_markdown_fences(response.result)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(f"Decisor ANALYSIS devolvió JSON inválido: {raw[:300]}")
        # Por seguridad, cerramos la fase
        return AnalysisDecision(
            action="close_phase",
            speaker=None,
            reasoning="parse_error: cerrando fase por seguridad",
            cost_usd=response.cost_usd,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    action = data.get("action", "close_phase")
    speaker = data.get("speaker")
    if action == "speak":
        if speaker not in VALID_ANALYSIS_SPEAKERS:
            logger.warning(f"Decisor ANALYSIS devolvió speaker inválido: {speaker}")
            action = "close_phase"
            speaker = None

    return AnalysisDecision(
        action=action,
        speaker=speaker,
        reasoning=data.get("reasoning", ""),
        cost_usd=response.cost_usd,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
