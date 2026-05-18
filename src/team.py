"""
Orquestación del equipo con fases + Decisor orgánico en ANALYSIS + bloqueos humanos.

Bloqueos humanos:
- [BLOQUEO_HUMANO_BLOQUEANTE]: detiene el turno. Solo TL o Jefe.
- [BLOQUEO_HUMANO_DIFERIDO]: notifica pero no detiene. Cualquier rol.
- [BLOQUEO_HUMANO] (legacy): tratado como diferido.

Las etiquetas deben aparecer en los primeros HUMAN_BLOCK_PREFIX_WINDOW caracteres
(al principio del mensaje), no en cualquier punto.
"""
import asyncio
import logging
from dataclasses import dataclass, field

from src.claude_runner import run_claude
from src.config import settings
from src.decider import decide_next, decide_within_analysis, Decision
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
    apply_transition,
    handle_jefe_verdict,
)
from src.roles import ALL_ROLES, Role
from src.webhooks import post_as_role
from src.authorization import request_authorization
from src import channel_logger, state, memory

logger = logging.getLogger(__name__)


CONTEXT_WINDOW = 20
MAX_AGENT_TURNS_PER_TURN = 20  # salvaguarda única anti-bucle

# Marcadores de bloqueo humano (deben aparecer al inicio del mensaje)
HUMAN_BLOCK_TAG_BLOCKING = "[BLOQUEO_HUMANO_BLOQUEANTE"
HUMAN_BLOCK_TAG_DEFERRED = "[BLOQUEO_HUMANO_DIFERIDO"
HUMAN_BLOCK_TAG_LEGACY = "[BLOQUEO_HUMANO]"
HUMAN_BLOCK_PREFIX_WINDOW = 200
ROLES_THAT_CAN_BLOCK = {"tl", "jefe"}


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
    human_blocks: int = 0
    blocking_human_block: bool = False
    authorization_result: str = ""


def _is_analytical_decision(decision: Decision) -> bool:
    return decision.speaker in ("a1", "a2")


async def _run_agent(
    role: Role,
    history_text: str,
    extra_instruction: str = "",
) -> tuple[str, float, int, int]:
    memory_block = memory.format_as_context()

    prompt_parts = [
        memory_block,
        "",
        f"Historial reciente del canal (orden cronológico):\n{history_text}",
        "",
        f"Responde desde tu rol ({role.display_name}).",
    ]
    if extra_instruction:
        prompt_parts.append("")
        prompt_parts.append(f"Instrucción para este turno:\n{extra_instruction}")
    prompt_parts.extend([
        "",
        "Antes de responder, revisa la memoria del proyecto: respeta los principios, "
        "no propongas estrategias ya descartadas (ver strategies_tested.md), "
        "y ten en cuenta la tarea en curso si la hay. "
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


async def _maybe_flag_human_block(role: Role, content: str, bot) -> tuple[bool, bool]:
    """
    Detecta bloqueos humanos al inicio del mensaje.
    Devuelve (detected, is_blocking).
    """
    head = content[:HUMAN_BLOCK_PREFIX_WINDOW]
    is_blocking_tag = HUMAN_BLOCK_TAG_BLOCKING in head
    is_deferred_tag = HUMAN_BLOCK_TAG_DEFERRED in head
    is_legacy_tag = HUMAN_BLOCK_TAG_LEGACY in head

    if not (is_blocking_tag or is_deferred_tag or is_legacy_tag):
        return False, False

    # Validación de permisos: solo TL y Jefe pueden emitir BLOQUEANTE
    is_blocking = is_blocking_tag and role.id in ROLES_THAT_CAN_BLOCK

    if is_blocking_tag and role.id not in ROLES_THAT_CAN_BLOCK:
        logger.warning(
            f"{role.display_name} intentó emitir BLOQUEANTE sin permiso, degradado a DIFERIDO"
        )
        await channel_logger.log(
            f"⚠️ **{role.display_name}** intentó emitir BLOQUEANTE sin permiso, "
            f"degradado a DIFERIDO."
        )

    kind = "BLOQUEANTE" if is_blocking else "DIFERIDO"
    icon = "🛑" if is_blocking else "🚨"
    mention = f"<@{settings.discord_my_user_id}>"
    anuncio = (
        f"{icon} **Bloqueo humano detectado ({kind})** {mention}\n"
        f"Origen: **{role.display_name}**\n\n"
        f"{content[:1500]}"
    )
    try:
        anuncios_channel = bot.get_channel(settings.discord_anuncios_channel_id)
        if anuncios_channel is not None:
            await anuncios_channel.send(anuncio[:1990])
    except Exception:
        logger.exception("Error publicando en #anuncios")

    await channel_logger.log(
        f"{icon} **[BLOQUEO_HUMANO_{kind}]** emitido por **{role.display_name}**"
    )
    return True, is_blocking


_bot_ref = None


def set_bot(bot):
    global _bot_ref
    _bot_ref = bot


async def _publish_and_save(role: Role, content: str) -> tuple[bool, bool]:
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
    had_block = False
    is_blocking = False
    if _bot_ref is not None:
        had_block, is_blocking = await _maybe_flag_human_block(role, content, _bot_ref)
    return had_block, is_blocking


async def _execute_agent_turn(
    role: Role,
    extra_instruction: str,
    result: TurnResult,
) -> str:
    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    history_text = format_context(recent)

    try:
        reply, cost, tin, tout = await _run_agent(role, history_text, extra_instruction)
        result.total_cost_usd += cost
        result.total_input_tokens += tin
        result.total_output_tokens += tout
    except Exception as e:
        logger.exception(f"Error generando respuesta de {role.display_name}")
        reply = f"(Error interno en {role.display_name}: {e})"

    had_block, is_blocking = await _publish_and_save(role, reply)
    if had_block:
        result.human_blocks += 1
    if is_blocking:
        result.blocking_human_block = True
    result.speakers_invoked.append(role.id)
    return reply


async def _run_analytical_flow(initial_decision: Decision) -> TurnResult:
    result = TurnResult(route="analytical")
    result.total_cost_usd += initial_decision.cost_usd
    result.total_input_tokens += initial_decision.input_tokens
    result.total_output_tokens += initial_decision.output_tokens

    state_obj = PhaseState(phase=Phase.ANALYSIS)
    first_speaker_hint = initial_decision.speaker
    last_planning_reply: str = ""
    first_analysis_iteration = True

    while len(result.speakers_invoked) < MAX_AGENT_TURNS_PER_TURN:
        action: PhaseAction = decide_next_action(state_obj)

        if state_obj.phase.value not in result.phases_visited:
            result.phases_visited.append(state_obj.phase.value)

        logger.info(
            f"[phase {state_obj.phase.value}] action={action.kind} "
            f"speaker={action.speaker} reason={action.reason!r}"
        )

        if action.kind == "close":
            result.halted_reason = f"phase_close: {action.reason}"
            return result

        if action.kind == "request_authorization":
            await channel_logger.log(
                f"🔄 `{state_obj.phase.value}` → solicitando autorización humana"
            )
            channel = None
            if _bot_ref is not None:
                channel = _bot_ref.get_channel(settings.discord_lobby_channel_id)
            if channel is None:
                result.halted_reason = "authorization_no_channel"
                return result

            decision_auth = await request_authorization(channel, last_planning_reply or "(plan no capturado)")
            result.authorization_result = decision_auth
            result.halted_reason = f"authorization_{decision_auth}"
            return result

        if action.kind == "delegate_to_decider":
            if first_analysis_iteration and first_speaker_hint in ("a1", "a2"):
                speaker_id = first_speaker_hint
                first_analysis_iteration = False
            else:
                first_analysis_iteration = False
                recent = get_recent_messages(limit=CONTEXT_WINDOW)
                history_text = format_context(recent)
                analysis_decision = await decide_within_analysis(history_text)
                result.total_cost_usd += analysis_decision.cost_usd
                result.total_input_tokens += analysis_decision.input_tokens
                result.total_output_tokens += analysis_decision.output_tokens

                await channel_logger.log(
                    f"🧭 **Decisor ANALYSIS** → action=`{analysis_decision.action}` "
                    f"speaker=`{analysis_decision.speaker}` · {analysis_decision.reasoning[:200]}"
                )
                await channel_logger.budget(
                    f"🧭 **Decisor ANALYSIS** (haiku): ${analysis_decision.cost_usd:.4f} · "
                    f"{analysis_decision.input_tokens} in / {analysis_decision.output_tokens} out"
                )

                if analysis_decision.action == "close_phase":
                    await channel_logger.log(
                        f"🔄 `ANALYSIS` → `SYNTHESIS` · {analysis_decision.reasoning[:100]}"
                    )
                    apply_transition(state_obj, Phase.SYNTHESIS)
                    continue

                speaker_id = analysis_decision.speaker

            role = ALL_ROLES.get(speaker_id) if speaker_id else None
            if role is None:
                logger.warning(f"Rol desconocido en ANALYSIS: {speaker_id}")
                apply_transition(state_obj, Phase.SYNTHESIS)
                continue

            await channel_logger.log(
                f"🎤 `ANALYSIS` → **{role.display_name}** habla"
            )

            analysis_instruction = (
                "Estamos en fase ANALYSIS. Aporta tu perspectiva de forma concreta. "
                "Si otro analista ya ha hablado, complementa o discrepa con argumento — NO repitas. "
                "Si tienes pregunta directa para el otro analista, formúlala al final. "
                "Si estás de acuerdo con lo propuesto, dilo explícitamente y añade solo matices críticos."
            )
            await _execute_agent_turn(role, analysis_instruction, result)

            # Si alguien emitió BLOQUEANTE, paramos el turno
            if result.blocking_human_block:
                result.halted_reason = "blocking_human_block"
                await channel_logger.log(
                    "🛑 Turno detenido por **BLOQUEO_HUMANO_BLOQUEANTE**. "
                    "El equipo espera la respuesta del humano."
                )
                return result

            await asyncio.sleep(0.8)
            continue

        if action.kind == "speak" and action.speaker:
            role = ALL_ROLES.get(action.speaker)
            if role is None:
                logger.warning(f"Rol desconocido en fase: {action.speaker}")
                result.halted_reason = "unknown_role"
                return result

            await channel_logger.log(
                f"🎤 `{state_obj.phase.value}` → **{role.display_name}** habla"
            )
            reply = await _execute_agent_turn(role, action.instruction, result)

            # Halt por bloqueante
            if result.blocking_human_block:
                result.halted_reason = "blocking_human_block"
                await channel_logger.log(
                    "🛑 Turno detenido por **BLOQUEO_HUMANO_BLOQUEANTE**. "
                    "El equipo espera la respuesta del humano."
                )
                return result

            # Transiciones tras hablar
            if state_obj.phase == Phase.SYNTHESIS:
                await channel_logger.log(f"🔄 `SYNTHESIS` → `REVIEW`")
                apply_transition(state_obj, Phase.REVIEW)
            elif state_obj.phase == Phase.REVIEW:
                next_phase = handle_jefe_verdict(state_obj, reply)
                if next_phase == Phase.IDLE:
                    result.halted_reason = "jefe_no_clear_verdict"
                    return result
                elif next_phase == Phase.PLANNING:
                    await channel_logger.log(f"🔄 `REVIEW` → `PLANNING` · Jefe validó")
                    apply_transition(state_obj, Phase.PLANNING)
                else:
                    await channel_logger.log(
                        f"↩️ Jefe rechazó (rechazo #{state_obj.rejection_count}) → vuelta a ANALYSIS"
                    )
                    apply_transition(state_obj, Phase.ANALYSIS)
                    first_analysis_iteration = True
            elif state_obj.phase == Phase.PLANNING:
                last_planning_reply = reply
                await channel_logger.log(f"🔄 `PLANNING` → `AUTHORIZATION`")
                apply_transition(state_obj, Phase.AUTHORIZATION)

            await asyncio.sleep(0.8)
            continue

    result.halted_reason = "max_agent_turns"
    logger.warning(
        f"Flujo analítico alcanzó el máximo de {MAX_AGENT_TURNS_PER_TURN} intervenciones"
    )
    await channel_logger.log(
        f"⚠️ Salvaguarda activada: {MAX_AGENT_TURNS_PER_TURN} intervenciones alcanzadas, cerrando turno"
    )
    return result


async def _run_shortcut(initial_decision: Decision) -> TurnResult:
    result = TurnResult(route="shortcut")
    result.total_cost_usd += initial_decision.cost_usd
    result.total_input_tokens += initial_decision.input_tokens
    result.total_output_tokens += initial_decision.output_tokens

    speaker_id = initial_decision.speaker
    role = ALL_ROLES.get(speaker_id) if speaker_id else None
    if role is None:
        result.halted_reason = "unknown_role_shortcut"
        return result

    await channel_logger.log(f"🎤 Shortcut → **{role.display_name}** habla")
    await _execute_agent_turn(role, "", result)

    if result.blocking_human_block:
        result.halted_reason = "blocking_human_block"
        await channel_logger.log(
            "🛑 Turno detenido por **BLOQUEO_HUMANO_BLOQUEANTE**."
        )
        return result

    result.halted_reason = "shortcut_done"
    return result


async def _finalize_turn(result: TurnResult) -> TurnResult:
    extra_parts = []
    if result.human_blocks:
        if result.blocking_human_block:
            extra_parts.append(f"🛑 {result.human_blocks} bloqueo(s) (BLOQUEANTE)")
        else:
            extra_parts.append(f"🚨 {result.human_blocks} bloqueo(s) (diferido)")
    if result.authorization_result:
        extra_parts.append(f"🛂 auth={result.authorization_result}")
    extra = (" · " + " · ".join(extra_parts)) if extra_parts else ""

    await channel_logger.log(
        f"✅ Turno cerrado: `{result.halted_reason}` · "
        f"speakers={result.speakers_invoked} ({len(result.speakers_invoked)}) · "
        f"fases={result.phases_visited}{extra}"
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

    if _is_analytical_decision(decision):
        await channel_logger.log("🧠 Ruta: **ANALÍTICA** (orgánica, Decisor manda en ANALYSIS)")
        result = await _run_analytical_flow(decision)
    else:
        await channel_logger.log(f"⚡ Ruta: **SHORTCUT** ({decision.speaker})")
        result = await _run_shortcut(decision)

    return await _finalize_turn(result)
