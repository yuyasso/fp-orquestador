"""
Máquina de estados para el flujo de decisión del equipo.

Fases del flujo analítico (las únicas implementadas en Modo A):
- IDLE: esperando input humano.
- ANALYSIS: analistas debaten hasta consenso o límite de rondas.
- SYNTHESIS: PO sintetiza la propuesta consensuada.
- REVIEW: Jefe de Proyecto valida o rechaza.

Si Jefe rechaza → vuelve a ANALYSIS con su feedback como contexto.
Si Jefe valida → vuelve a IDLE (Modo A). En Modo B avanzará a PLANNING/EXECUTION.

Las preguntas puntuales (no analíticas) no usan esta máquina: van por la
ruta rápida con el Decisor libre (ver team.py).
"""
from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    IDLE = "IDLE"
    ANALYSIS = "ANALYSIS"
    SYNTHESIS = "SYNTHESIS"
    REVIEW = "REVIEW"


@dataclass
class PhaseState:
    phase: Phase = Phase.IDLE
    a1_turns: int = 0           # intervenciones de A1 en la fase ANALYSIS actual
    a2_turns: int = 0
    last_analyst_speaker: str | None = None  # "a1" | "a2" | None
    rejection_count: int = 0    # veces que el Jefe ha rechazado en este ciclo

    def reset_analysis_counters(self) -> None:
        self.a1_turns = 0
        self.a2_turns = 0
        self.last_analyst_speaker = None


# Límites
MAX_ANALYST_ROUNDS = 3           # máximo por analista en una fase ANALYSIS
MAX_REJECTIONS = 2               # tras N rechazos del Jefe, forzamos cierre


@dataclass
class PhaseAction:
    """Qué debe hacer el ejecutor a continuación."""
    kind: str                            # "speak" | "transition" | "close"
    speaker: str | None = None           # rol a invocar si kind=="speak"
    instruction: str = ""                # instrucción específica para el rol
    next_phase: Phase | None = None      # fase destino si kind=="transition"
    reason: str = ""


def decide_next_action(state: PhaseState, last_message_author: str | None) -> PhaseAction:
    """
    Dado el estado de fase y quién habló por última vez, decide qué hacer.
    No mira el contenido de los mensajes — decisiones basadas en estructura.
    """
    if state.phase == Phase.IDLE:
        # No debería llamarse en IDLE; quien pasó a no-IDLE lo hace explícitamente.
        return PhaseAction(kind="close", reason="idle: nada que hacer")

    if state.phase == Phase.ANALYSIS:
        # Estrategia: alternar A1 y A2 hasta completar al menos 2 rondas cada uno
        # o hasta alcanzar el máximo.
        both_reached_min = state.a1_turns >= 2 and state.a2_turns >= 2
        a1_at_max = state.a1_turns >= MAX_ANALYST_ROUNDS
        a2_at_max = state.a2_turns >= MAX_ANALYST_ROUNDS

        if both_reached_min or (a1_at_max and a2_at_max):
            return PhaseAction(
                kind="transition",
                next_phase=Phase.SYNTHESIS,
                reason=(
                    f"análisis completo "
                    f"(A1:{state.a1_turns}, A2:{state.a2_turns})"
                ),
            )

        # Decidir quién habla siguiente: el que ha hablado menos, empezando por A1 si empate.
        if state.a1_turns <= state.a2_turns and not a1_at_max:
            next_speaker = "a1"
        elif not a2_at_max:
            next_speaker = "a2"
        else:
            next_speaker = "a1"

        # Evitar que el mismo analista hable dos veces seguidas salvo necesidad
        if state.last_analyst_speaker == next_speaker:
            alt = "a2" if next_speaker == "a1" else "a1"
            if (alt == "a1" and not a1_at_max) or (alt == "a2" and not a2_at_max):
                next_speaker = alt

        instruction = (
            "Estamos en fase ANALYSIS. Aporta tu perspectiva de forma concreta y breve. "
            "Si otro analista ya ha hablado, complementa o discrepa con argumento — NO repitas. "
            "Si tienes pregunta directa para el otro analista, formúlala al final."
        )
        if state.a1_turns + state.a2_turns >= 2:
            instruction += (
                " Estamos avanzando hacia consenso: si ya estás de acuerdo con lo propuesto, "
                "confírmalo explícitamente y añade solo matices críticos."
            )

        return PhaseAction(
            kind="speak",
            speaker=next_speaker,
            instruction=instruction,
            reason=f"ANALYSIS (A1:{state.a1_turns}, A2:{state.a2_turns})",
        )

    if state.phase == Phase.SYNTHESIS:
        return PhaseAction(
            kind="speak",
            speaker="po",
            instruction=(
                "Estamos en fase SYNTHESIS. Los analistas han debatido. "
                "Tu tarea: SINTETIZAR la propuesta consensuada con estructura clara: "
                "(1) resumen en una frase, (2) estrategia concreta propuesta, "
                "(3) criterios de validación que aplicarás, (4) riesgos identificados. "
                "Sé conciso y decisorio. No repitas lo que ya dijeron los analistas: consolida."
            ),
            reason="SYNTHESIS → PO sintetiza",
        )

    if state.phase == Phase.REVIEW:
        return PhaseAction(
            kind="speak",
            speaker="jefe",
            instruction=(
                "Estamos en fase REVIEW. El PO ha sintetizado una propuesta. "
                "Tu tarea: VALIDAR o RECHAZAR con criterios exigentes. "
                "Si validas, empieza tu mensaje con: **[VALIDADO]** seguido de tu razonamiento breve "
                "y cualquier apunte estratégico. "
                "Si rechazas, empieza con: **[RECHAZADO]** seguido de los puntos concretos "
                "que el equipo debe reforzar o replantear. No aceptes mediocridad, conformismo "
                "ni atajos. Si la propuesta es buena, dilo sin regalar elogios."
            ),
            reason="REVIEW → Jefe valida o rechaza",
        )

    return PhaseAction(kind="close", reason=f"fase desconocida: {state.phase}")


def register_agent_turn(state: PhaseState, speaker: str) -> None:
    """Actualiza contadores tras una intervención de un agente durante ANALYSIS."""
    if state.phase == Phase.ANALYSIS:
        if speaker == "a1":
            state.a1_turns += 1
            state.last_analyst_speaker = "a1"
        elif speaker == "a2":
            state.a2_turns += 1
            state.last_analyst_speaker = "a2"


def apply_transition(state: PhaseState, new_phase: Phase) -> None:
    """Aplica una transición de fase con efectos colaterales."""
    if new_phase == Phase.ANALYSIS:
        state.reset_analysis_counters()
    state.phase = new_phase


def handle_jefe_verdict(state: PhaseState, jefe_reply: str) -> Phase:
    """
    Interpreta la respuesta del Jefe en REVIEW.
    Devuelve la siguiente fase:
      - IDLE si validó o si se superó el máximo de rechazos.
      - ANALYSIS si rechazó y aún hay margen.
    """
    text = jefe_reply.upper()
    if "[VALIDADO]" in text:
        return Phase.IDLE
    if "[RECHAZADO]" in text:
        state.rejection_count += 1
        if state.rejection_count >= MAX_REJECTIONS:
            return Phase.IDLE  # cortamos, el humano decidirá
        return Phase.ANALYSIS
    # Si no puso ninguna etiqueta, lo tratamos como validación por defecto
    # para no bloquearnos, pero lo logueamos.
    return Phase.IDLE
