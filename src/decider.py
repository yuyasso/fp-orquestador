"""
Decisor: decide qué rol(es) deben responder a un mensaje y en qué orden.
Si no tiene claro a quién convocar, puede pedir clarificación al humano.
"""
import json
import logging
from dataclasses import dataclass, field

from src.claude_runner import run_claude

logger = logging.getLogger(__name__)


DECIDER_SYSTEM_PROMPT = """Eres el Orquestador de un equipo de desarrollo de un sistema de trading. \
Tu única tarea es decidir qué miembros del equipo deben responder al mensaje del humano y en qué orden.

El equipo está compuesto por:
- "jefe" (Jefe de Proyecto): exigente, supervisa calidad global, interviene en temas estratégicos, riesgos, o cuando detecta conformismo. NO participa en cada mensaje.
- "po" (Product Owner): define alcance, prioriza, valida entregas, traduce necesidades en requisitos.
- "tl" (Tech Lead): decisiones técnicas, arquitectura, stack, interlocutor con Claude Code para código.
- "a1" (Analista 1, cuantitativo): estrategias clásicas, rigor estadístico, métricas, backtesting robusto.
- "a2" (Analista 2, microestructura): régimen de mercado, order flow, contexto macro, liquidez.

Reglas de convocatoria:
1. Saludos, agradecimientos, small talk, mensajes triviales → NADIE responde. speakers = [].
2. Pregunta técnica concreta sobre arquitectura, stack, código → solo "tl".
3. Pregunta sobre qué construir, prioridades, alcance, aceptación → solo "po".
4. Pregunta sobre estrategias de trading, análisis de mercado, qué invertir → "a1" y "a2" en ese orden para que debatan.
5. Pregunta estratégica de alto nivel, o mensaje donde se detecta que el equipo se está conformando con algo mediocre → "jefe".
6. Mensajes complejos que requieren varios roles → lista ordenada con máximo 3 speakers.
7. Cuando haya dudas, sé PARCO: es mejor que solo 1-2 roles hablen que convocar a todos.
8. El humano (Fran) tiene autoridad: si pide explícitamente escuchar a alguien concreto ("quiero que opine TL", "@po qué opinas"), convoca a ESE rol aunque las demás reglas sugieran otro.

Si el mensaje es GENUINAMENTE AMBIGUO y no sabes a quién convocar (podría ser técnico o de producto, podría ser estrategia o decisión de alto nivel, etc.), en lugar de adivinar, PIDE CLARIFICACIÓN al humano. Para eso, devuelve needs_clarification=true y una pregunta corta y útil.

Tu respuesta DEBE ser un JSON válido con esta estructura EXACTA, sin texto adicional antes ni después:

Caso normal (sabes a quién convocar o nadie debe responder):
{"speakers": ["a1", "a2"], "reasoning": "breve explicación", "needs_clarification": false, "clarification_question": ""}

Caso mensaje trivial (nadie responde):
{"speakers": [], "reasoning": "mensaje trivial", "needs_clarification": false, "clarification_question": ""}

Caso ambiguo (necesitas clarificación del humano):
{"speakers": [], "reasoning": "no queda claro el ámbito", "needs_clarification": true, "clarification_question": "¿Va dirigido al TL (técnico) o al PO (producto)?"}

IMPORTANTE: responde SOLO con el JSON, sin markdown, sin ```json, sin texto de introducción ni cierre."""


@dataclass
class Decision:
    speakers: list[str] = field(default_factory=list)
    reasoning: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""


async def decide_speakers(user_message: str, context: str = "") -> Decision:
    """
    Dado un mensaje del usuario, decide qué roles responden y en qué orden.
    Puede indicar que necesita clarificación del humano si el mensaje es ambiguo.

    context: opcional, historial reciente u otra info relevante que ayude a decidir.
    """
    prompt = user_message
    if context:
        prompt = f"Contexto reciente:\n{context}\n\nMensaje nuevo del humano:\n{user_message}"

    response = await run_claude(
        prompt=prompt,
        model="haiku",
        system_prompt=DECIDER_SYSTEM_PROMPT,
    )

    raw = response.result.strip()

    # Limpieza defensiva por si el modelo envuelve en markdown pese al prompt
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
            speakers=[],
            reasoning="parse_error",
            needs_clarification=True,
            clarification_question=(
                "No he podido interpretar bien tu mensaje. "
                "¿Puedes reformularlo indicando a qué rol te diriges "
                "(TL, PO, Jefe, Analistas)?"
            ),
        )

    speakers = data.get("speakers", [])
    if not isinstance(speakers, list):
        speakers = []

    valid_ids = {"jefe", "po", "tl", "a1", "a2"}
    speakers = [s for s in speakers if s in valid_ids]

    return Decision(
        speakers=speakers,
        reasoning=data.get("reasoning", ""),
        needs_clarification=bool(data.get("needs_clarification", False)),
        clarification_question=data.get("clarification_question", ""),
    )
