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
    handle_po_verdict,
)
from src.roles import ALL_ROLES, Role
from src.webhooks import post_as_role
from src.authorization import request_authorization
from src import memory_writer
from src import director
from src.claude_executor import execute_claude_code, ExecutionResult
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
    execution_success: bool = False
    execution_cost_usd: float = 0.0
    execution_session_id: str = ""


def _is_analytical_decision(decision: Decision) -> bool:
    return decision.speaker in ("a1", "a2")


async def _run_agent(
    role: Role,
    history_text: str,
    extra_instruction: str = "",
    include_repo_state: bool = False,
) -> tuple[str, float, int, int]:
    memory_block = memory.format_as_context()

    prompt_parts = [
        memory_block,
        "",
    ]

    # Inyectar estado real del repo solo cuando hace falta (PLANNING del TL).
    if include_repo_state:
        try:
            repo_state = memory.get_repo_state_for_planning()
            prompt_parts.append(repo_state)
            prompt_parts.append("")
        except Exception:
            logger.exception("Error obteniendo estado del repo para PLANNING")

    prompt_parts.extend([
        f"Historial reciente del canal (orden cronológico):\n{history_text}",
        "",
        f"Responde desde tu rol ({role.display_name}).",
    ])
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
    include_repo_state: bool = False,
) -> str:
    recent = get_recent_messages(limit=CONTEXT_WINDOW)
    history_text = format_context(recent)

    try:
        reply, cost, tin, tout = await _run_agent(
            role, history_text, extra_instruction, include_repo_state=include_repo_state
        )
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
    last_po_synthesis: str = ""
    last_tl_report: str = ""
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
            # En modo autónomo, AUTHORIZATION se salta: el Director ya autoriza
            # implícitamente al activar /auto on. Vuelve a botones humanos con /auto off.
            if state.is_autonomous():
                await channel_logger.log(
                    f"🔄 `AUTHORIZATION` → `EXECUTION` · 🤖 auto-autorizado (modo autónomo)"
                )
                result.authorization_result = "auto_authorized"
                apply_transition(state_obj, Phase.EXECUTION)
                continue

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
            if decision_auth != "authorized":
                result.halted_reason = f"authorization_{decision_auth}"
                return result
            # Autorizada → pasamos a EXECUTION
            await channel_logger.log(f"🔄 `AUTHORIZATION` → `EXECUTION` · plan aprobado")
            apply_transition(state_obj, Phase.EXECUTION)
            continue

        if action.kind == "execute_plan":
            await _run_execution(last_planning_reply, result)
            if not result.execution_success:
                result.halted_reason = "execution_failed"
                return result
            # Ejecución OK → REPORTING (TL informa al equipo)
            await channel_logger.log(f"🔄 `EXECUTION` → `REPORTING`")
            apply_transition(state_obj, Phase.REPORTING)
            continue

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
            # Inyectar estado del repo en PLANNING para que el TL no invente rutas
            inject_repo = (state_obj.phase == Phase.PLANNING and role.id == "tl")
            if inject_repo:
                await channel_logger.log(
                    f"📂 Inyectando estado real del repo al TL para PLANNING"
                )
            reply = await _execute_agent_turn(
                role, action.instruction, result, include_repo_state=inject_repo
            )

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
                last_po_synthesis = reply
                await channel_logger.log(f"🔄 `SYNTHESIS` → `REVIEW`")
                apply_transition(state_obj, Phase.REVIEW)
            elif state_obj.phase == Phase.REVIEW:
                next_phase = handle_jefe_verdict(state_obj, reply)
                if next_phase == Phase.IDLE:
                    result.halted_reason = "jefe_no_clear_verdict"
                    return result
                elif next_phase == Phase.PLANNING:
                    await channel_logger.log(f"🔄 `REVIEW` → `PLANNING` · Jefe validó")
                    # Persistir la decisión validada en decisions.md
                    try:
                        if memory_writer.record_jefe_validation(last_po_synthesis, reply):
                            await channel_logger.log(
                                f"💾 `decisions.md` actualizado con la decisión validada"
                            )
                    except Exception:
                        logger.exception("Error escribiendo decisions.md")
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
            elif state_obj.phase == Phase.REPORTING:
                last_tl_report = reply
                await channel_logger.log(f"🔄 `REPORTING` -> `ACCEPTANCE`")
                apply_transition(state_obj, Phase.ACCEPTANCE)
            elif state_obj.phase == Phase.ACCEPTANCE:
                next_phase = handle_po_verdict(state_obj, reply)
                if next_phase == Phase.IDLE:
                    # Aceptado -> sprint cerrado
                    result.halted_reason = "accepted"
                    await channel_logger.log(
                        f"✅ PO **[ACEPTADO]** -> sprint cerrado"
                    )
                    # Persistir entregable y vaciar tarea actual
                    try:
                        if memory_writer.record_sprint_acceptance(
                            tl_planning=last_planning_reply,
                            tl_report=last_tl_report,
                            po_acceptance=reply,
                            execution_session_id=result.execution_session_id,
                        ):
                            await channel_logger.log(
                                f"💾 Memoria actualizada: `strategies_tested.md`, "
                                f"`current_task.md`, `roadmap.md`"
                            )
                    except Exception:
                        logger.exception("Error escribiendo memoria tras ACEPTADO")

                    # Auto-commit del repo de trading
                    try:
                        commit_msg = memory_writer.build_commit_message(
                            last_planning_reply,
                            result.execution_session_id,
                        )
                        await channel_logger.log(
                            f"📦 Iniciando auto-commit del repo de trading..."
                        )
                        success, log_text = memory_writer.commit_and_push_repo(commit_msg)
                        icon = "✅" if success else "⚠️"
                        await channel_logger.log(
                            f"{icon} Auto-commit:\n{log_text[:1500]}"
                        )
                    except Exception:
                        logger.exception("Error en auto-commit del repo")
                        await channel_logger.log(
                            f"🔴 Auto-commit falló con excepción (ver logs del bot)"
                        )

                    # Modo autónomo: invocar al Director para decidir siguiente sprint
                    if state.is_autonomous():
                        try:
                            await _invoke_director_and_dispatch(
                                tl_planning=last_planning_reply,
                                tl_report=last_tl_report,
                                po_acceptance=reply,
                                turn_cost_usd=result.total_cost_usd,
                            )
                        except Exception:
                            logger.exception("Error invocando al Director")
                            await channel_logger.log(
                                f"🔴 Director falló con excepción → pausa autónoma"
                            )
                            state.disable_autonomous()
                            await _notify_director_pause("excepción interna del Director")

                    return result
                else:
                    # Rechazado -> vuelta a PLANNING para iterar
                    await channel_logger.log(
                        f"↩️ PO **[RECHAZADO]** -> vuelta a PLANNING para iterar"
                    )
                    apply_transition(state_obj, Phase.PLANNING)

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


async def _run_execution(plan_text: str, result: TurnResult) -> None:
    """
    Invoca Claude Code para ejecutar el plan del TL en el repo de trading.
    Eventos en tiempo real → #claude-code.
    Resumen final firmado por el TL → #lobby (vía webhook).
    """
    from src.roles import TECH_LEAD

    cwd = settings.trading_repo_path

    await channel_logger.log(
        f"⚙️ **EXECUTION** arrancando en `{cwd}`"
    )

    # Canal #claude-code para streaming de eventos técnicos
    claude_code_channel = None
    if _bot_ref is not None:
        from src.config import settings as _s
        # Reutilizamos el discord_logs_channel_id como fallback si no hay #claude-code configurado
        claude_code_channel = _bot_ref.get_channel(_s.discord_logs_channel_id)

    # Anuncio en #lobby de inicio
    if _bot_ref is not None:
        lobby = _bot_ref.get_channel(settings.discord_lobby_channel_id)
        if lobby is not None:
            await lobby.send(
                f"⚙️ **Claude Code arrancando** — implementando el plan del Tech Lead. "
                f"Sigue los detalles técnicos en #logs."
            )

    async def on_event(event: dict) -> None:
        etype = event.get("type")
        if etype == "start":
            sid = event.get("session_id", "")
            await channel_logger.log(
                f"🟢 **Claude Code START** · session=`{sid[:8]}` · model=`{event.get('model', '')}`"
            )
        elif etype == "tool_use":
            name = event.get("tool_name", "")
            inp = event.get("tool_input", {}) or {}
            summary = _summarize_tool_input(name, inp)
            await channel_logger.log(f"🔨 `{name}` · {summary}")
        elif etype == "tool_result":
            is_err = event.get("is_error", False)
            content_sum = event.get("content_summary", "")
            icon = "❌" if is_err else "✅"
            await channel_logger.log(f"{icon} resultado · {content_sum[:200]}")
        elif etype == "text":
            text = event.get("text", "").strip()
            if text:
                await channel_logger.log(f"💬 Claude: {text[:300]}")
        elif etype == "error":
            await channel_logger.log(f"🔴 Error en ejecución: {event.get('message', '')}")
        # thinking: silenciado en #logs

    # Disallowlist mínima de cosas peligrosas
    disallowed = [
        "Bash(sudo *)",
        "Bash(rm -rf /*)",
        "Bash(rm -rf ~*)",
        "Bash(git push --force*)",
        "Bash(git push -f*)",
    ]

    exec_result: ExecutionResult = await execute_claude_code(
        prompt=plan_text,
        cwd=cwd,
        on_event=on_event,
        model="sonnet",
        disallowed_tools=disallowed,
        skip_permissions=True,
    )

    # Acumular coste y registrar
    result.execution_success = exec_result.success
    result.execution_cost_usd = exec_result.cost_usd
    result.execution_session_id = exec_result.session_id
    result.total_cost_usd += exec_result.cost_usd
    result.total_input_tokens += exec_result.input_tokens
    result.total_output_tokens += exec_result.output_tokens

    await channel_logger.budget(
        f"⚙️ **Claude Code ejecución**: ${exec_result.cost_usd:.4f} · "
        f"{exec_result.input_tokens} in / {exec_result.output_tokens} out · "
        f"{exec_result.duration_ms / 1000:.1f}s · {exec_result.num_turns} turnos"
    )

    # Resumen final firmado por el TL en #lobby
    if exec_result.success:
        summary_msg = (
            f"✅ **Ejecución completada.**\n\n"
            f"{exec_result.result_text[:1500]}\n\n"
            f"_Detalles técnicos en #logs · sesión `{exec_result.session_id[:8]}`._"
        )
    else:
        summary_msg = (
            f"❌ **La ejecución ha fallado.**\n\n"
            f"Error: {exec_result.error or 'sin detalle'}\n\n"
            f"_Detalles técnicos en #logs._"
        )

    try:
        await post_as_role(TECH_LEAD, summary_msg)
    except Exception:
        logger.exception("Error publicando resumen de ejecución")

    # Guardar el resumen en el historial conversacional
    save_message(
        author_kind="agent",
        author_name=TECH_LEAD.display_name,
        author_id=TECH_LEAD.id,
        content=summary_msg,
    )

    await channel_logger.log(
        f"⚙️ **EXECUTION terminada** · success={exec_result.success} · "
        f"events={exec_result.events_count}"
    )


def _summarize_tool_input(name: str, inp: dict) -> str:
    """Resume el input de una tool en una línea legible."""
    if name == "Write" or name == "Edit":
        path = inp.get("file_path", "?")
        return f"`{path}`"
    if name == "Read":
        path = inp.get("file_path") or inp.get("path", "?")
        return f"`{path}`"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"`{cmd[:120]}`"
    if name == "Glob":
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"`{pattern}` en `{path}`"
    if name == "Grep":
        pattern = inp.get("pattern", "")
        return f"`{pattern}`"
    # Genérico
    short = str(inp)[:120]
    return short


async def _notify_director_pause(reason: str) -> None:
    """Publica en #anuncios que el Director ha pausado la cadena autónoma."""
    if _bot_ref is None:
        return
    try:
        anuncios = _bot_ref.get_channel(settings.discord_anuncios_channel_id)
        if anuncios is not None:
            mention = f"<@{settings.discord_my_user_id}>"
            await anuncios.send(
                f"⏸️ **Director: pausa autónoma** {mention}\n"
                f"Motivo: {reason[:500]}\n\n"
                f"El modo autónomo se ha desactivado. Lanza tu siguiente input cuando quieras "
                f"o reactívalo con `/auto on`."
            )
    except Exception:
        logger.exception("Error publicando notificación de pausa del Director")


async def _notify_director_stop(reason: str) -> None:
    """Publica en #anuncios que el Director ha detenido el experimento."""
    if _bot_ref is None:
        return
    try:
        anuncios = _bot_ref.get_channel(settings.discord_anuncios_channel_id)
        if anuncios is not None:
            mention = f"<@{settings.discord_my_user_id}>"
            await anuncios.send(
                f"🛑 **Director: STOP** {mention}\n"
                f"Motivo: {reason[:500]}\n\n"
                f"El modo autónomo se ha desactivado. Decisión humana requerida."
            )
    except Exception:
        logger.exception("Error publicando notificación de stop del Director")


async def _invoke_director_and_dispatch(
    tl_planning: str,
    tl_report: str,
    po_acceptance: str,
    turn_cost_usd: float,
) -> None:
    """
    Invoca al Director tras [ACEPTADO] del PO. Si decide continuar, publica
    el siguiente mensaje en #lobby como si lo escribiera el humano, lo que
    desencadena el procesamiento del siguiente turno automáticamente.
    """
    # Acumular el coste del turno que acaba de cerrar en la cadena
    state.add_chain_cost(turn_cost_usd)
    chain = state.get_chain_status()

    await channel_logger.log(
        f"🤖 **Director** evaluando... "
        f"(cadena: {chain['chain_sprints']}/{chain['max_chain_sprints']} sprints, "
        f"${chain['chain_cost_eur']:.2f}/${chain['max_chain_cost_eur']:.2f})"
    )

    decision = await director.decide_next_action(
        tl_planning=tl_planning,
        tl_report=tl_report,
        po_acceptance=po_acceptance,
        sprints_in_autonomous_chain=chain['chain_sprints'],
        accumulated_cost_eur=chain['chain_cost_eur'],
        max_chain_sprints=chain['max_chain_sprints'],
        max_chain_cost_eur=chain['max_chain_cost_eur'],
    )

    state.add_chain_cost(decision.cost_usd)

    await channel_logger.budget(
        f"🤖 **Director** (sonnet): ${decision.cost_usd:.4f} · "
        f"{decision.input_tokens} in / {decision.output_tokens} out"
    )

    await channel_logger.log(
        f"🤖 **Director decide:** `{decision.action}` "
        f"(confianza {decision.confidence}) · {decision.reasoning[:300]}"
    )

    if decision.action == "stop":
        state.disable_autonomous()
        await _notify_director_stop(decision.reasoning)
        return

    if decision.action == "pause":
        state.disable_autonomous()
        await _notify_director_pause(decision.reasoning)
        return

    # continue: publicar el next_message en #lobby como si lo escribiera el humano
    if not decision.next_message.strip():
        state.disable_autonomous()
        await _notify_director_pause(
            "Director devolvió continue pero next_message vacío. Pauso por seguridad."
        )
        return

    # Bumpeamos el contador ANTES de publicar
    state.bump_chain_sprint()
    chain = state.get_chain_status()

    if _bot_ref is None:
        logger.error("No hay bot_ref para publicar mensaje del Director")
        state.disable_autonomous()
        return

    lobby = _bot_ref.get_channel(settings.discord_lobby_channel_id)
    if lobby is None:
        logger.error("No se encontró #lobby para publicar mensaje del Director")
        state.disable_autonomous()
        return

    # Publicar el mensaje del Director como si fuera del humano (mismo flujo del bot)
    prefix = (
        f"🤖 **[Director · sprint {chain['chain_sprints']}/{chain['max_chain_sprints']}]** "
        f"_(modo autónomo activo, ${chain['chain_cost_eur']:.2f}/${chain['max_chain_cost_eur']:.2f})_\n\n"
    )
    full_msg = prefix + decision.next_message
    # Discord limita a 2000 chars
    if len(full_msg) > 1990:
        full_msg = full_msg[:1990]

    try:
        await lobby.send(full_msg)
        await channel_logger.log(
            f"🤖 Director lanzó siguiente sprint en #lobby"
        )
    except Exception:
        logger.exception("Error publicando mensaje del Director en #lobby")
        state.disable_autonomous()
        await _notify_director_pause("error publicando next_message en #lobby")


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
