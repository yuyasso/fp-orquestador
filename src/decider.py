"""
Decisor: decide quién responde a continuación en la conversación.
Funciona tanto para el primer turno tras un mensaje humano como para
turnos sucesivos donde el último mensaje puede ser de otro agente.
"""
import json
import logging
from dataclasses import dataclass, field

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

El humano del equipo se llama Fran. Sus mensajes tienen MÁXIMA PRIORIDAD: si pide algo explícito, respétalo por encima de las otras reglas.

Recibirás el historial reciente de la conversación. Debes decidir:
- Quién es el SIGUIENTE que debería hablar (solo UNO), o
- Que el turno se cierre (nadie habla, se espera al humano).

Reglas:
1. Si el último mensaje es una pregunta directa a un agente concreto (ej. "mi pregunta a A1: ..."), convoca a ese agente.
2. Si el último mensaje es del humano pidiendo explícitamente a un rol, convoca a ese rol.
3. Saludos, agradecimientos, small talk triviales del humano → nadie responde (speaker=null).
4. Pregunta técnica concreta del humano → "tl".
5. Pregunta de alcance/prioridades del humano → "po".
6. Pregunta sobre estrategias de trading del humano → "a1" primero (si no ha hablado aún en el tema) o "a2" (si le toca el contraturno o A1 acaba de hablar).
7. Si los analistas están debatiendo y uno acaba de responder al otro con un punto nuevo, conviene que el otro replique UNA VEZ más para cerrar. Después, cierra el turno con speaker=null para que intervenga el PO/Jefe/Fran, NO dejes que se extiendan indefinidamente.
8. Si detectas que el equipo está llegando a algo mediocre, conformista, o se está saltando rigor → "jefe".
9. Si dos agentes han alcanzado consenso o una propuesta concreta, puede intervenir "po" para sintetizar. O puedes cerrar turno (speaker=null) para esperar al humano.
10. IMPORTANTE: no permitas bucles infinitos. Tras 3-4 intercambios entre el mismo par de agentes, cierra turno.
11. Si el último mensaje deja el tema en un punto estable o contiene una pregunta implícita al humano, cierra turno (speaker=null).

Tu respuesta DEBE ser un JSON válido con esta estructura EXACTA, sin texto adicional, sin markdown:

{"speaker": "a2", "reasoning": "breve explicación", "needs_clarification": false, "clarification_question": ""}

Si el turno se cierra (nadie habla a continuación):
{"speaker": null, "reasoning": "turno cerrado, esperando al humano", "needs_clarification": false, "clarification_question": ""}

Si el mensaje del humano es ambiguo y pides clarificación:
{"speaker": null, "reasoning": "ambiguo", "needs_clarification": true, "clarification_question": "¿Te refieres a X o a Y?"}

Responde SOLO con el JSON."""


@dataclass
class Decision:
    speaker: str | None = None            # uno solo, o None si nadie habla
    reasoning: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""


VALID_ROLES = {"jefe", "po", "tl", "a1", "a2"}


async def decide_next(history_text: str) -> Decision:
    """
    Dado el historial reciente (serializado como texto), decide quién habla a continuación
    o si el turno se cierra.
    """
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

    raw = response.result.strip()

    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

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
                "¿Puedes reformularlo indicando a qué rol te diriges "
                "(TL, PO, Jefe, Analistas)?"
            ),
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
    )
