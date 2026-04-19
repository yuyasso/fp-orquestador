"""
Decisor: decide quién responde a continuación en la conversación.
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

El humano del equipo se llama Fran. Sus mensajes tienen MÁXIMA PRIORIDAD.

---

REGLA 0 (crítica, por encima de todas las demás):
Un mensaje del humano que contiene una pregunta ("?", "cómo", "qué", "quién", "cuándo", "por qué"), \
una petición explícita ("necesito...", "quiero...", "dame..."), una instrucción ("haz...", "explícame..."), \
o una afirmación con contenido sustantivo NUNCA puede cerrarse con speaker=null argumentando \
"ya está respondido en el contexto", "el tema ya está cerrado", "es redundante" o similares.

El humano puede estar:
- Reabriendo un tema que cree que no quedó bien resuelto.
- Pidiendo que se reexplique con otro enfoque.
- Queriendo oír AHORA la opinión del equipo, aunque se haya tratado antes.
- Cambiando de perspectiva sobre algo ya discutido.

SI el mensaje del humano es no trivial, SIEMPRE convocas al rol apropiado. Nunca contestas "ya te lo dijimos".

Solo se cierra con speaker=null un mensaje humano si es GENUINAMENTE trivial: saludo puro \
("hola", "buenas"), agradecimiento ("gracias"), reacción corta ("ok", "vale", "perfecto"), \
o similar sin contenido que merezca respuesta.

---

El resto de reglas aplican después de la regla 0:

1. Si el último mensaje es una pregunta directa a un agente concreto (ej. "mi pregunta a A1: ..."), convoca a ese agente.
2. Si el humano pide explícitamente a un rol ("quiero que opine TL", "@po qué opinas"), convoca a ese rol.
3. Saludos, agradecimientos, small talk TRIVIALES del humano (y nada más) → speaker=null.
4. Pregunta técnica concreta del humano sobre arquitectura/stack/código → "tl".
5. Pregunta de alcance/prioridades del humano → "po".
6. Pregunta sobre estrategias de trading del humano → "a1" primero o "a2" si le toca.
7. Si los analistas están debatiendo y uno acaba de responder al otro con un punto nuevo, el otro replica UNA VEZ más para cerrar. Después, cierra el turno con speaker=null.
8. Si detectas que el equipo está llegando a algo mediocre, conformista, o se salta rigor → "jefe".
9. Si dos agentes han alcanzado consenso o propuesta concreta, "po" puede sintetizar. O cerrar turno para esperar al humano.
10. No permitas bucles infinitos. Tras 3-4 intercambios entre el mismo par de agentes, cierra turno.
11. Si el último mensaje de un agente deja el tema estable o contiene pregunta implícita al humano, cierra turno (speaker=null).

---

Tu respuesta DEBE ser un JSON válido con esta estructura EXACTA, sin texto adicional, sin markdown:

{"speaker": "a2", "reasoning": "breve explicación", "needs_clarification": false, "clarification_question": ""}

Si el turno se cierra:
{"speaker": null, "reasoning": "motivo breve", "needs_clarification": false, "clarification_question": ""}

Si el humano es ambiguo y pides clarificación:
{"speaker": null, "reasoning": "ambiguo", "needs_clarification": true, "clarification_question": "¿Te refieres a X o a Y?"}

Responde SOLO con el JSON."""


@dataclass
class Decision:
    speaker: str | None = None
    reasoning: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


VALID_ROLES = {"jefe", "po", "tl", "a1", "a2"}


async def decide_next(history_text: str) -> Decision:
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
