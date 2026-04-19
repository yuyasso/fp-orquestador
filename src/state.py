"""
Estado runtime del orquestador (pausa, etc.).
En memoria: se reinicia cuando el bot arranca.
"""

_paused: bool = False


def is_paused() -> bool:
    return _paused


def set_paused(value: bool) -> None:
    global _paused
    _paused = value
