"""
Máquina de estados para el flujo de decisión + planificación del equipo.

Fases:
- IDLE: esperando input humano.
- ANALYSIS: analistas debaten. Quien habla y cuándo cerrar lo decide el Decisor (no esta máquina).
- SYNTHESIS: PO sintetiza la propuesta consensuada. Único speaker, fase corta.
- REVIEW: Jefe valida [VALIDADO] o rechaza [RECHAZADO]. Sin tope de rechazos.
- PLANNING: TL redacta el encargo concreto para Claude Code. Único speaker.
- AUTHORIZATION: gate humano (botones).

Transiciones automáticas (decididas por código):
- SYNTHESIS termina → REVIEW.
- REVIEW [VALIDADO] → PLANNING.
- REVIEW [RECHAZADO] → vuelta a ANALYSIS (sin tope, las veces que haga falta).
- PLANNING termina → AUTHORIZATION.

Dentro de ANALYSIS: el Decisor decide en cada paso si habla A1, A2, Jefe (alerta), o
si la fase debe cerrarse y pasar a SYNTHESIS. Sin contadores rígidos.
"""
from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    IDLE = "IDLE"
    ANALYSIS = "ANALYSIS"
    SYNTHESIS = "SYNTHESIS"
    REVIEW = "REVIEW"
    PLANNING = "PLANNING"
    AUTHORIZATION = "AUTHORIZATION"
    EXECUTION = "EXECUTION"
    REPORTING = "REPORTING"


@dataclass
class PhaseState:
    phase: Phase = Phase.IDLE
    rejection_count: int = 0  # informativo, ya no es tope


@dataclass
class PhaseAction:
    kind: str                            # "speak" | "transition" | "close" | "request_authorization" | "delegate_to_decider"
    speaker: str | None = None
    instruction: str = ""
    next_phase: Phase | None = None
    reason: str = ""


def decide_next_action(state: PhaseState) -> PhaseAction:
    """
    Decide qué hacer a continuación según la fase. Las decisiones de ANALYSIS
    se delegan al Decisor (kind='delegate_to_decider').
    """
    if state.phase == Phase.IDLE:
        return PhaseAction(kind="close", reason="idle: nada que hacer")

    if state.phase == Phase.ANALYSIS:
        return PhaseAction(
            kind="delegate_to_decider",
            reason="ANALYSIS → Decisor decide siguiente speaker o cierre",
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
                "Recuerda: tu verdict es BINARIO. Mensaje empieza con [VALIDADO] o [RECHAZADO] "
                "como primer elemento, una sola etiqueta, sin mezclar. "
                "Si rechazas, lista qué falta dirigido a cada rol que deba resolverlo. "
                "No aceptes 'validado con condiciones'. No hay límite de iteraciones — rechaza "
                "las veces que sea necesario hasta que todo esté limpio."
            ),
            reason="REVIEW → Jefe valida o rechaza",
        )

    if state.phase == Phase.PLANNING:
        return PhaseAction(
            kind="speak",
            speaker="tl",
            instruction=(
                "Estamos en fase PLANNING. La propuesta ha sido VALIDADA por el Jefe. "
                "Tu tarea: redactar un ENCARGO CONCRETO para Claude Code con esta estructura EXACTA:\n\n"
                "**Objetivo:** qué se construye, en una frase.\n"
                "**Archivos a crear/modificar:** rutas concretas en el repo fp-trading-system.\n"
                "**Implementación:** descripción técnica precisa: clases, funciones, interfaces, patrones (hexagonal, etc.).\n"
                "**Tests:** qué tests deben existir y qué cubren (TDD donde aplique).\n"
                "**Criterios de aceptación:** lista verificable que el PO usará para validar.\n"
                "**Comandos de validación:** qué comandos ejecutar para comprobar que está bien (tests, linter, etc.).\n\n"
                "No escribas código aquí. Solo el encargo. Claude Code lo recibirá tal cual. "
                "Sé técnico, preciso, ejecutable. Evita ambigüedades."
            ),
            reason="PLANNING → TL redacta encargo",
        )

    if state.phase == Phase.AUTHORIZATION:
        return PhaseAction(
            kind="request_authorization",
            reason="AUTHORIZATION → gate humano",
        )

    if state.phase == Phase.EXECUTION:
        return PhaseAction(
            kind="execute_plan",
            reason="EXECUTION → Claude Code ejecuta el plan del TL",
        )

    if state.phase == Phase.REPORTING:
        return PhaseAction(
            kind="speak",
            speaker="tl",
            instruction=(
                "Estamos en fase REPORTING. Claude Code ha terminado la ejecución del plan que tú redactaste. "
                "Tienes en el historial reciente el resumen técnico de lo que hizo. "
                "Tu tarea: producir un REPORTE ESTRUCTURADO para el equipo (PO especialmente) con:\n"
                "**Entregado:** qué se construyó realmente (módulos, archivos clave).\n"
                "**Validaciones:** qué tests pasan, mypy/ruff/cobertura.\n"
                "**Desviaciones del plan:** si Claude Code hizo algo distinto de lo planeado, dilo claramente.\n"
                "**Deuda técnica:** lo que queda por hacer o pulir, sin maquillaje.\n"
                "**Riesgos descubiertos:** problemas que aparecieron durante la implementación.\n"
                "**Próximo paso recomendado:** qué sería lo siguiente.\n\n"
                "Sé honesto: si algo no quedó bien, dilo. El PO va a validar contra los criterios "
                "de aceptación que definió en SYNTHESIS — tu trabajo aquí es darle la información "
                "que necesita para decidir, no venderle el trabajo."
            ),
            reason="REPORTING → TL informa al equipo",
        )

    return PhaseAction(kind="close", reason=f"fase desconocida: {state.phase}")


def apply_transition(state: PhaseState, new_phase: Phase) -> None:
    state.phase = new_phase


def handle_jefe_verdict(state: PhaseState, jefe_reply: str) -> Phase:
    """
    Reglas estrictas:
    - Si aparece [RECHAZADO] → rechazo (prioridad sobre [VALIDADO]).
    - Si solo aparece [VALIDADO] → validación.
    - Si no hay etiqueta clara → cierre por defecto (IDLE).

    Sin tope de rechazos: el equipo itera hasta que esté limpio.
    """
    text = jefe_reply.upper()
    has_rejected = "[RECHAZADO]" in text
    has_validated = "[VALIDADO]" in text

    if has_rejected:
        state.rejection_count += 1
        return Phase.ANALYSIS

    if has_validated:
        return Phase.PLANNING

    return Phase.IDLE
