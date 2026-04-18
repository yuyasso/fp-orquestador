"""
Orquestación del equipo: coordina Decisor -> Agentes -> Webhooks.
Este es el corazón del sistema en Modo A (reactivo).
"""
import asyncio
import logging
from dataclasses import dataclass

from src.claude_runner import run_claude
from src.decider import decide_speakers, Decision
from src.history import (
    save_message,
    get_recent_messages,
    format_context,
    Message,
)
from src.roles import ALL_ROLES, Role
from src.webhooks import post_as_role

logger = logging.getLogger(__name__)


CONTEXT_WINDOW = 20  # cuántos mensajes recientes incluir como contexto


@dataclass
class TurnResult:
    speakers_invoked: list[str]
    needs_clarification: bool
    clarification_question: str
    reasoning: str


async def _generate_agent_response(role: Role, context: str, user_message: str) -> str:
    """Genera la respuesta de un agente dado el contexto y el mensaje disparador."""
    prompt = (
        f"Historial reciente del canal (orden cronológico, tú también puedes aparecer):\n"
        f"{context}\n\n"
        f"Último mensaje del humano (Fran):\n"
        f"{user_message}\n\n"
        f"Responde desde tu rol. No te presentes si ya has hablado antes. "
        f"Si ves que otro compañero ya ha dicho lo mismo, complementa o discrepa, "
        f"no repitas."
    )

    response = await run_claude(
        prompt=prompt,
        model=role.model,
        system_prompt=role.system_prompt,
    )

    logger.info(
        f"[{role.display_name}] respuesta generada "
        f"({response.input_tokens} in / {response.output_tokens} out / "
        f"${response.cost_usd:.4f})"
    )

    text = (response.result or "").strip()
    return text or "(sin respuesta)"


async def handle_user_message(
    user_name: str,
    user_id: str,
    content: str,
    notify_clarification,  # callable async (texto) -> None, para pedir clarificación
) -> TurnResult:
    """
    Punto de entrada. Se llama cada vez que el humano escribe en #lobby.
    Devuelve el resultado del turno (qué roles hablaron, si hubo clarificación, etc.).
    """
    # 1. Guardar el mensaje del humano
    save_message(
        author_kind="human",
        author_name=user_name,
        author_id=user_id,
        content=content,
    )

    # 2. Obtener contexto reciente (incluye el mensaje que acabamos de guardar)
    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    context_text = format_context(recent)

    # 3. Decidir
    decision: Decision = await decide_speakers(
        user_message=content,
        context=context_text,
    )
    logger.info(
        f"Decisión: speakers={decision.speakers} "
        f"clarify={decision.needs_clarification} "
        f"reasoning={decision.reasoning!r}"
    )

    # 4. Si necesita clarificación, lo notificamos al humano (sin firmar como rol)
    if decision.needs_clarification and decision.clarification_question:
        await notify_clarification(decision.clarification_question)
        return TurnResult(
            speakers_invoked=[],
            needs_clarification=True,
            clarification_question=decision.clarification_question,
            reasoning=decision.reasoning,
        )

    # 5. Si nadie responde, fin silencioso
    if not decision.speakers:
        return TurnResult(
            speakers_invoked=[],
            needs_clarification=False,
            clarification_question="",
            reasoning=decision.reasoning,
        )

    # 6. Para cada rol convocado: generar, publicar, guardar en historial.
    # Recalculamos el contexto entre speakers para que cada siguiente vea lo que dijo el anterior.
    for role_id in decision.speakers:
        role = ALL_ROLES.get(role_id)
        if role is None:
            logger.warning(f"Rol desconocido: {role_id}, saltando")
            continue

        recent = get_recent_messages(limit=CONTEXT_WINDOW)
        context_text = format_context(recent)

        try:
            reply = await _generate_agent_response(
                role=role,
                context=context_text,
                user_message=content,
            )
        except Exception as e:
            logger.exception(f"Error generando respuesta de {role.display_name}")
            reply = f"(Error interno en {role.display_name}: {e})"

        # Publicar en Discord
        try:
            await post_as_role(role, reply)
        except Exception:
            logger.exception(f"Error publicando webhook de {role.display_name}")

        # Guardar en historial
        save_message(
            author_kind="agent",
            author_name=role.display_name,
            author_id=role.id,
            content=reply,
        )

        # Pequeña pausa entre agentes para que se vea natural
        await asyncio.sleep(0.5)

    return TurnResult(
        speakers_invoked=decision.speakers,
        needs_clarification=False,
        clarification_question="",
        reasoning=decision.reasoning,
    )
