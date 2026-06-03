"""
Ejecutor de Claude Code en modo headless con streaming.

Invoca `claude -p "..."` como subproceso largo, parsea cada línea JSON del
stream, y va llamando al callback con eventos digeridos para que la capa
superior los postee a Discord.

Eventos digeridos que emitimos:
- start: session_id, cwd, modelo.
- thinking: texto del razonamiento (silenciado por defecto, lo guardamos por si).
- text: texto que Claude "dice" al usuario.
- tool_use: tool_name, input. Útil para mostrar acciones en curso.
- tool_result: resumen del resultado de la herramienta.
- error: cualquier fallo en el parseo o subproceso.
- end: resultado final, coste, duración.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    success: bool = False
    result_text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    num_turns: int = 0
    error: str = ""
    events_count: int = 0


EventCallback = Callable[[dict], Awaitable[None]]


async def execute_claude_code(
    prompt: str,
    cwd: Path,
    on_event: EventCallback,
    model: str = "sonnet",
    allowed_tools: Optional[list[str]] = None,
    disallowed_tools: Optional[list[str]] = None,
    skip_permissions: bool = True,
    timeout_seconds: int = 30 * 60,  # 30 min para tareas largas
) -> ExecutionResult:
    """
    Ejecuta Claude Code en cwd, streamea eventos al callback, y devuelve el
    resultado final consolidado.
    """
    cwd = Path(cwd).expanduser().resolve()
    if not cwd.exists():
        return ExecutionResult(success=False, error=f"cwd no existe: {cwd}")

    # Pasamos el prompt por stdin para evitar el limite ARG_MAX del SO (~128 KB).
    # -p sin argumento posicional lee de stdin.
    cmd = [
        "claude",
        "-p",
        "--model", model,
        "--output-format", "stream-json",
        "--input-format", "text",
        "--verbose",
    ]

    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools:
        cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])

    logger.info(f"Ejecutando Claude Code en {cwd}: {prompt[:80]}...")

    # Límite del stream: el default (64 KB) es insuficiente para tool_results grandes.
    # Lo subimos a 10 MB para cubrir lecturas de archivos grandes u output extenso de Bash.
    STREAM_LIMIT = 10 * 1024 * 1024  # 10 MB

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            limit=STREAM_LIMIT,
        )
    except FileNotFoundError:
        return ExecutionResult(
            success=False,
            error="Comando 'claude' no encontrado. ¿Claude Code está instalado?",
        )

    result = ExecutionResult()
    stderr_bytes = b""

    async def read_stdout():
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError as e:
                # Línea aún mayor que el límite: leer en bruto hasta \n
                logger.warning(f"Línea excede límite del stream, leyendo en bruto: {e}")
                chunks = []
                while True:
                    chunk = await proc.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                line = b"".join(chunks)
                if not line:
                    break
            if not line:
                break
            await _handle_line(line, on_event, result)

    async def read_stderr():
        nonlocal stderr_bytes
        assert proc.stderr is not None
        stderr_bytes = await proc.stderr.read()

    # Enviar el prompt por stdin y cerrar para que claude empiece a procesar
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:
        logger.exception("Error escribiendo prompt a stdin de claude")
        proc.kill()
        await proc.wait()
        return ExecutionResult(success=False, error="No se pudo enviar prompt a stdin")

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        result.success = False
        result.error = f"Claude Code excedió timeout de {timeout_seconds}s"
        await on_event({"type": "error", "message": result.error})
        return result

    if proc.returncode != 0:
        result.success = False
        result.error = (
            f"Claude Code salió con código {proc.returncode}. "
            f"stderr: {stderr_bytes.decode('utf-8', errors='replace')[:500]}"
        )
        await on_event({"type": "error", "message": result.error})

    return result


async def _handle_line(line: bytes, on_event: EventCallback, result: ExecutionResult):
    """Parsea una línea JSON del stream y emite eventos digeridos."""
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"Línea no JSON en stream: {text[:200]}")
        return

    result.events_count += 1
    event_type = data.get("type")

    if event_type == "system" and data.get("subtype") == "init":
        result.session_id = data.get("session_id", "")
        await on_event({
            "type": "start",
            "session_id": result.session_id,
            "cwd": data.get("cwd", ""),
            "model": data.get("model", ""),
        })
        return

    if event_type == "assistant":
        message = data.get("message", {})
        content = message.get("content", [])
        for block in content:
            btype = block.get("type")
            if btype == "thinking":
                await on_event({
                    "type": "thinking",
                    "text": block.get("thinking", ""),
                })
            elif btype == "text":
                await on_event({
                    "type": "text",
                    "text": block.get("text", ""),
                })
            elif btype == "tool_use":
                await on_event({
                    "type": "tool_use",
                    "tool_name": block.get("name", ""),
                    "tool_input": block.get("input", {}),
                    "tool_id": block.get("id", ""),
                })
        return

    if event_type == "user":
        # tool_result llega como mensaje de usuario sintético
        message = data.get("message", {})
        content = message.get("content", [])
        for block in content:
            if block.get("type") == "tool_result":
                await on_event({
                    "type": "tool_result",
                    "tool_id": block.get("tool_use_id", ""),
                    "is_error": block.get("is_error", False),
                    "content_summary": _summarize_tool_result(block.get("content", "")),
                })
        return

    if event_type == "result":
        result.success = not data.get("is_error", False)
        result.result_text = data.get("result", "")
        result.cost_usd = data.get("total_cost_usd", 0.0)
        usage = data.get("usage", {}) or {}
        result.input_tokens = usage.get("input_tokens", 0)
        result.output_tokens = usage.get("output_tokens", 0)
        result.duration_ms = data.get("duration_ms", 0)
        result.num_turns = data.get("num_turns", 0)
        await on_event({
            "type": "end",
            "success": result.success,
            "result_text": result.result_text,
            "cost_usd": result.cost_usd,
            "duration_ms": result.duration_ms,
            "num_turns": result.num_turns,
        })
        return

    if event_type == "rate_limit_event":
        # Solo informativo, no emitimos a Discord
        return


def _summarize_tool_result(content) -> str:
    """Resume el contenido de un tool_result en una línea legible."""
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        joined = " ".join(parts)
        return joined[:200]
    return str(content)[:200]
