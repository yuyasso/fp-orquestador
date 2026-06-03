import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResponse:
    result: str
    session_id: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    is_error: bool
    raw: dict


async def run_claude(
    prompt: str,
    model: str = "sonnet",
    system_prompt: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout_seconds: int = 300,
) -> ClaudeResponse:
    """
    Invoca claude-code en modo headless y devuelve la respuesta parseada.

    Pasa el prompt y el system_prompt por stdin para evitar el limite ARG_MAX
    del SO (~128 KB) que rompia con historial grande + memoria + system prompt
    como argumentos.

    model: 'haiku', 'sonnet' o 'opus' (alias que Claude Code resuelve).
    """
    # -p sin argumento posicional -> lee prompt de stdin con --input-format text.
    cmd = [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "json",
        "--input-format", "text",
    ]
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    if session_id:
        cmd.extend(["--resume", session_id])

    logger.info(
        f"Ejecutando Claude Code (modelo={model}, "
        f"prompt={prompt[:60]}... [{len(prompt)} chars])"
    )

    # NOTA: system_prompt aun va como argumento (no hay flag stdin para el).
    # Si crece demasiado tambien rompera ARG_MAX; vigilar.
    sp_len = len(system_prompt) if system_prompt else 0
    if sp_len > 60_000:
        logger.warning(
            f"system_prompt grande ({sp_len} chars). Cercano a limite ARG_MAX."
        )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Claude Code excedió timeout de {timeout_seconds}s")

    if proc.returncode != 0:
        raise RuntimeError(
            f"Claude Code salio con codigo {proc.returncode}. "
            f"stderr: {stderr.decode('utf-8', errors='replace')[:500]}"
        )

    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"No se pudo parsear JSON de Claude Code: {e}. "
            f"Output: {stdout.decode('utf-8', errors='replace')[:500]}"
        )

    return ClaudeResponse(
        result=data.get("result", ""),
        session_id=data.get("session_id", ""),
        cost_usd=data.get("total_cost_usd", 0.0),
        input_tokens=data.get("usage", {}).get("input_tokens", 0),
        output_tokens=data.get("usage", {}).get("output_tokens", 0),
        is_error=data.get("is_error", False),
        raw=data,
    )
