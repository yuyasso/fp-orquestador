"""
Orquestación del equipo con máquina de fases + logging a canales.
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
from src import channel_logger, state

logger = logging.getLogger(__name__)


CONTEXT_WINDOW = 20
MAX_PHASE_ITERATIONS = 12


@dataclass
class TurnResult:
    route: str = ""
    speakers_invoked: list[str] = field(default_factory=list)
    phases_visited: list[str] = field(default_factory=list)
    halted_reason: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


def _is_analytical_decision(decision: Decision) -> bool:
    return decision.speaker in ("a1", "a2")


async def _run_agent(
    role: Role,
    history_text: str,
    extra_instruction: str = "",
) -> tuple[str, float, int, int]:
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

    await channel_logger.budget(
        f"💬 **{role.display_name}** ({role.model}): "
        f"${response.cost_usd:.4f} "
        f"· {response.input_tokens} in / {response.output_tokens} out"
    )

    text = (response.result or "").strip() or "(sin respuesta)"
    return text, response.cost_usd, response.input_tokens, response.output_tokens


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
    result = TurnResult(route="analytical")
    # Arrastramos el coste del Decisor inicial
    result.total_cost_usd += initial_decision.cost_usd
    result.total_input_tokens += initial_decision.input_tokens
    result.total_output_tokens += initial_decision.output_tokens

    state = PhaseState(phase=Phase.ANALYSIS)
    first_speaker_hint = initial_decision.speaker

    iterations = 0
    while iterations < MAX_PHASE_ITERATIONS:
        iterations += 1

        action: PhaseAction = decide_next_action(
            state=state,
            last_message_author=None,
        )

        if iterations == 1 and action.kind == "speak" and first_speaker_hint in ("a1", "a2"):
            action = PhaseAction(
                kind="speak",
                speaker=first_speaker_hint,
                instruction=action.instruction,
                reason=action.reason + f" (arranque sesgado: {first_speaker_hint})",
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
            await channel_logger.log(
                f"🔄 `{state.phase.value}` → `{action.next_phase.value}` · {action.reason}"
            )
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

            await channel_logger.log(
                f"🎤 `{state.phase.value}` → **{role.display_name}** habla"
            )

            try:
                reply, cost, tin, tout = await _run_agent(
                    role, history_text, action.instruction
                )
                result.total_cost_usd += cost
                result.total_input_tokens += tin
                result.total_output_tokens += tout
            except Exception as e:
                logger.exception(f"Error generando respuesta de {role.display_name}")
                reply = f"(Error interno en {role.display_name}: {e})"

            await _publish_and_save(role, reply)
            result.speakers_invoked.append(role.id)

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
                    return result
                else:
                    await channel_logger.log(
                        f"↩️ Jefe rechazó (rechazos: {state.rejection_count}/2) → volvemos a ANALYSIS"
                    )
                    apply_transition(state, Phase.ANALYSIS)

            await asyncio.sleep(0.8)
            continue

    result.halted_reason = "max_phase_iterations"
    logger.warning(f"Flujo analítico alcanzó el máximo de {MAX_PHASE_ITERATIONS} iteraciones")
    return result


async def _run_shortcut(initial_decision: Decision) -> TurnResult:
    result = TurnResult(route="shortcut")
    # Arrastramos el coste del Decisor inicial
    result.total_cost_usd += initial_decision.cost_usd
    result.total_input_tokens += initial_decision.input_tokens
    result.total_output_tokens += initial_decision.output_tokens

    speaker_id = initial_decision.speaker
    role = ALL_ROLES.get(speaker_id) if speaker_id else None
    if role is None:
        result.halted_reason = "unknown_role_shortcut"
        return result

    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    history_text = format_context(recent)

    await channel_logger.log(f"🎤 Shortcut → **{role.display_name}** habla")

    try:
        reply, cost, tin, tout = await _run_agent(role, history_text)
        result.total_cost_usd += cost
        result.total_input_tokens += tin
        result.total_output_tokens += tout
    except Exception as e:
        logger.exception(f"Error generando respuesta de {role.display_name}")
        reply = f"(Error interno en {role.display_name}: {e})"

    await _publish_and_save(role, reply)
    result.speakers_invoked.append(role.id)
    result.halted_reason = "shortcut_done"
    return result


async def _finalize_turn(result: TurnResult) -> TurnResult:
    """Emite el resumen de fin de turno en #logs y #presupuesto y devuelve result."""
    await channel_logger.log(
        f"✅ Turno cerrado: `{result.halted_reason}` · "
        f"speakers={result.speakers_invoked} · fases={result.phases_visited}"
    )
    await channel_logger.budget(
        f"💰 **Turno completo** ({result.route}): "
        f"${result.total_cost_usd:.4f} · "
        f"{len(result.speakers_invoked)} intervenciones · "
        f"{result.total_input_tokens} in / {result.total_output_tokens} out"
    )
    return result


async def handle_user_message(
    user_name: str,
    user_id: str,
    content: str,
    notify_clarification,
) -> TurnResult:
    save_message(
        author_kind="human",
        author_name=user_name,
        author_id=user_id,
        content=content,
    )

    # Si el orquestador está pausado, no convocamos a nadie.
    if state.is_paused():
        await channel_logger.log(
            f"⏸️ Mensaje recibido con orquestador PAUSADO. Mensaje guardado pero sin respuesta."
        )
        return TurnResult(route="paused", halted_reason="orchestrator_paused")

    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    history_text = format_context(recent)
    decision = await decide_next(history_text)

    logger.info(
        f"[decisor_inicial] speaker={decision.speaker} "
        f"clarify={decision.needs_clarification} "
        f"reasoning={decision.reasoning!r} "
        f"cost=${decision.cost_usd:.4f}"
    )
    await channel_logger.log(
        f"🧭 **Decisor inicial** → speaker=`{decision.speaker}` · {decision.reasoning[:200]}"
    )
    await channel_logger.budget(
        f"🧭 **Decisor** (haiku): ${decision.cost_usd:.4f} · "
        f"{decision.input_tokens} in / {decision.output_tokens} out"
    )

    # Clarificación
    if decision.needs_clarification and decision.clarification_question:
        await notify_clarification(decision.clarification_question)
        result = TurnResult(
            route="clarification",
            needs_clarification=True,
            clarification_question=decision.clarification_question,
            halted_reason="clarification",
            total_cost_usd=decision.cost_usd,
            total_input_tokens=decision.input_tokens,
            total_output_tokens=decision.output_tokens,
        )
        return await _finalize_turn(result)

    # Silencio
    if decision.speaker is None:
        await channel_logger.log("🤐 Sin speaker: silencio")
        result = TurnResult(
            route="silent",
            halted_reason="no_speaker",
            total_cost_usd=decision.cost_usd,
            total_input_tokens=decision.input_tokens,
            total_output_tokens=decision.output_tokens,
        )
        return await _finalize_turn(result)

    # Analítica vs shortcut
    if _is_analytical_decision(decision):
        await channel_logger.log("🧠 Ruta: **ANALÍTICA** (máquina de fases)")
        result = await _run_analytical_flow(decision)
    else:
        await channel_logger.log(f"⚡ Ruta: **SHORTCUT** ({decision.speaker})")
        result = await _run_shortcut(decision)

    return await _finalize_turn(result)
