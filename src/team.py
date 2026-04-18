"""
Orquestación del equipo con máquina de fases.

Flujo:
- Mensaje humano llega → Decisor inicial clasifica.
- Si la decisión es "analítica" (convoca A1 o A2) → activa máquina de fases:
  ANALYSIS → SYNTHESIS → REVIEW → IDLE (o vuelta a ANALYSIS si Jefe rechaza).
- Si la decisión es puntual (TL, PO solo, o Jefe solo sin contexto de análisis) →
  ruta corta: ese rol habla una vez y fin.
- Si el Decisor pide clarificación → se le pregunta al humano.
- Si el Decisor cierra sin speaker → silencio.
"""
import asyncio
import logging
from dataclasses import dataclass, field

from src.claude_runner import run_claude
from src.decider import decide_next, Decision
from src.history import (
    save_message,
    get_recent_messages,
    format_context,
)
from src.phases import (
    Phase,
    PhaseState,
    PhaseAction,
    decide_next_action,
    register_agent_turn,
    apply_transition,
    handle_jefe_verdict,
)
from src.roles import ALL_ROLES, Role
from src.webhooks import post_as_role

logger = logging.getLogger(__name__)


CONTEXT_WINDOW = 20
MAX_PHASE_ITERATIONS = 12  # salvaguarda absoluta del bucle de fases


@dataclass
class TurnResult:
    route: str = ""                      # "analytical" | "shortcut" | "silent" | "clarification"
    speakers_invoked: list[str] = field(default_factory=list)
    phases_visited: list[str] = field(default_factory=list)
    halted_reason: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""


def _is_analytical_decision(decision: Decision) -> bool:
    """
    Determina si la decisión inicial debe activar la máquina de fases.
    Criterio: el Decisor ha convocado a un Analista (A1 o A2).
    """
    return decision.speaker in ("a1", "a2")


async def _run_agent(role: Role, history_text: str, extra_instruction: str = "") -> str:
    """Genera la respuesta de un agente dado el historial y una instrucción opcional de fase."""
    prompt_parts = [
        f"Historial reciente del canal (orden cronológico):\n{history_text}",
        "",
        f"Responde desde tu rol ({role.display_name}).",
    ]
    if extra_instruction:
        prompt_parts.append("")
        prompt_parts.append(f"Instrucción para este turno:\n{extra_instruction}")
    prompt_parts.extend([
        "",
        "No te presentes si ya has hablado antes. Si otro compañero ya cubrió un punto, "
        "complementa o discrepa. No repitas. Sé conciso.",
    ])
    prompt = "\n".join(prompt_parts)

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

    return (response.result or "").strip() or "(sin respuesta)"


async def _publish_and_save(role: Role, content: str) -> None:
    try:
        await post_as_role(role, content)
    except Exception:
        logger.exception(f"Error publicando webhook de {role.display_name}")
    save_message(
        author_kind="agent",
        author_name=role.display_name,
        author_id=role.id,
        content=content,
    )


async def _run_analytical_flow(initial_decision: Decision) -> TurnResult:
    """Ejecuta la máquina de fases: ANALYSIS → SYNTHESIS → REVIEW."""
    result = TurnResult(route="analytical")
    state = PhaseState(phase=Phase.ANALYSIS)

    # Primer analista lo marca el Decisor inicial (a1 o a2). Sesga el arranque.
    first_speaker_hint = initial_decision.speaker  # "a1" o "a2"

    iterations = 0
    while iterations < MAX_PHASE_ITERATIONS:
        iterations += 1

        action: PhaseAction = decide_next_action(
            state=state,
            last_message_author=None,
        )

        # En la primera iteración, si el Decisor sugirió A2, arrancamos con A2 en vez de A1
        if iterations == 1 and action.kind == "speak" and first_speaker_hint in ("a1", "a2"):
            action = PhaseAction(
                kind="speak",
                speaker=first_speaker_hint,
                instruction=action.instruction,
                reason=action.reason + f" (arranque sesgado por Decisor: {first_speaker_hint})",
            )

        logger.info(
            f"[phase {state.phase.value}] action={action.kind} "
            f"speaker={action.speaker} reason={action.reason!r}"
        )
        if state.phase.value not in result.phases_visited:
            result.phases_visited.append(state.phase.value)

        if action.kind == "close":
            result.halted_reason = f"phase_close: {action.reason}"
            return result

        if action.kind == "transition" and action.next_phase:
            apply_transition(state, action.next_phase)
            continue

        if action.kind == "speak" and action.speaker:
            role = ALL_ROLES.get(action.speaker)
            if role is None:
                logger.warning(f"Rol desconocido en fase: {action.speaker}")
                result.halted_reason = "unknown_role"
                return result

            recent = get_recent_messages(limit=CONTEXT_WINDOW)
            history_text = format_context(recent)

            try:
                reply = await _run_agent(role, history_text, action.instruction)
            except Exception as e:
                logger.exception(f"Error generando respuesta de {role.display_name}")
                reply = f"(Error interno en {role.display_name}: {e})"

            await _publish_and_save(role, reply)
            result.speakers_invoked.append(role.id)

            # Post-efectos según rol/fase
            if state.phase == Phase.ANALYSIS:
                register_agent_turn(state, role.id)
            elif state.phase == Phase.SYNTHESIS:
                apply_transition(state, Phase.REVIEW)
            elif state.phase == Phase.REVIEW:
                next_phase = handle_jefe_verdict(state, reply)
                if next_phase == Phase.IDLE:
                    result.halted_reason = (
                        "jefe_validated" if "[VALIDADO]" in reply.upper()
                        else "jefe_rejected_max" if state.rejection_count >= 2
                        else "jefe_implicit_close"
                    )
                    if state.phase.value not in result.phases_visited:
                        result.phases_visited.append(state.phase.value)
                    return result
                else:
                    # rechazo con margen → vuelve a ANALYSIS
                    apply_transition(state, Phase.ANALYSIS)

            await asyncio.sleep(0.8)
            continue

    result.halted_reason = "max_phase_iterations"
    logger.warning(f"Flujo analítico alcanzó el máximo de {MAX_PHASE_ITERATIONS} iteraciones")
    return result


async def _run_shortcut(initial_decision: Decision) -> TurnResult:
    """Ruta rápida: un único rol responde una vez."""
    result = TurnResult(route="shortcut")
    speaker_id = initial_decision.speaker
    role = ALL_ROLES.get(speaker_id) if speaker_id else None
    if role is None:
        result.halted_reason = "unknown_role_shortcut"
        return result

    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    history_text = format_context(recent)

    try:
        reply = await _run_agent(role, history_text)
    except Exception as e:
        logger.exception(f"Error generando respuesta de {role.display_name}")
        reply = f"(Error interno en {role.display_name}: {e})"

    await _publish_and_save(role, reply)
    result.speakers_invoked.append(role.id)
    result.halted_reason = "shortcut_done"
    return result


async def handle_user_message(
    user_name: str,
    user_id: str,
    content: str,
    notify_clarification,  # callable async (str) -> None
) -> TurnResult:
    """
    Punto de entrada. Decide ruta corta vs analítica según el Decisor inicial.
    """
    # 1. Guardar el mensaje humano
    save_message(
        author_kind="human",
        author_name=user_name,
        author_id=user_id,
        content=content,
    )

    # 2. Decisor inicial
    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    history_text = format_context(recent)
    decision = await decide_next(history_text)

    logger.info(
        f"[decisor_inicial] speaker={decision.speaker} "
        f"clarify={decision.needs_clarification} "
        f"reasoning={decision.reasoning!r}"
    )

    # 3. Clarificación
    if decision.needs_clarification and decision.clarification_question:
        await notify_clarification(decision.clarification_question)
        return TurnResult(
            route="clarification",
            needs_clarification=True,
            clarification_question=decision.clarification_question,
            halted_reason="clarification",
        )

    # 4. Sin speaker → silencio
    if decision.speaker is None:
        return TurnResult(
            route="silent",
            halted_reason="no_speaker",
        )

    # 5. Enrutado: analítico vs shortcut
    if _is_analytical_decision(decision):
        logger.info("Ruta: ANALÍTICA (máquina de fases)")
        return await _run_analytical_flow(decision)
    else:
        logger.info(f"Ruta: SHORTCUT ({decision.speaker})")
        return await _run_shortcut(decision)
