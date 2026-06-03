"""
Estado runtime del orquestador.

En memoria: se reinicia cuando el bot arranca. No persiste entre reinicios
intencionalmente — cada arranque debe ser un estado limpio.

Contiene:
- Flag de pausa global (/pausa y /resume).
- Flag de modo autónomo (/auto on/off).
- Contadores de la cadena autónoma actual (sprints consecutivos y coste acumulado).
"""

# ============================================================
# Pausa global del orquestador
# ============================================================

_paused: bool = False


def is_paused() -> bool:
    return _paused


def set_paused(value: bool) -> None:
    global _paused
    _paused = value


# ============================================================
# Modo autónomo (el Director decide el siguiente sprint)
# ============================================================

# Límites de seguridad de la cadena autónoma. Si se alcanzan, el Director
# pausa automáticamente y espera intervención humana.
MAX_CHAIN_SPRINTS = 5            # máx sprints consecutivos sin humano
MAX_CHAIN_COST_EUR = 5.0         # máx euros gastados en cadena autónoma

_autonomous_mode: bool = False
_chain_sprints: int = 0
_chain_cost_eur: float = 0.0


def is_autonomous() -> bool:
    return _autonomous_mode


def enable_autonomous() -> None:
    global _autonomous_mode
    _autonomous_mode = True
    reset_chain()


def disable_autonomous() -> None:
    global _autonomous_mode
    _autonomous_mode = False
    reset_chain()


def bump_chain_sprint() -> int:
    """Incrementa el contador de sprints en cadena autónoma y lo devuelve."""
    global _chain_sprints
    _chain_sprints += 1
    return _chain_sprints


def add_chain_cost(amount_usd: float) -> None:
    """Acumula coste (recibimos USD, lo guardamos como EUR ~1:1 para simplificar)."""
    global _chain_cost_eur
    _chain_cost_eur += amount_usd


def reset_chain() -> None:
    """Resetea contadores cuando se desactiva autonomía o el humano interviene."""
    global _chain_sprints, _chain_cost_eur
    _chain_sprints = 0
    _chain_cost_eur = 0.0


def get_chain_status() -> dict:
    """Snapshot del estado actual de la cadena autónoma."""
    return {
        "autonomous_mode": _autonomous_mode,
        "chain_sprints": _chain_sprints,
        "max_chain_sprints": MAX_CHAIN_SPRINTS,
        "chain_cost_eur": round(_chain_cost_eur, 4),
        "max_chain_cost_eur": MAX_CHAIN_COST_EUR,
    }
