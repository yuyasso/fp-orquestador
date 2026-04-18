"""
Publicación de mensajes en Discord vía webhooks, firmados por rol.
"""
import logging
import aiohttp

from src.config import settings
from src.roles import Role

logger = logging.getLogger(__name__)


# Mapeo rol.id -> URL del webhook
_WEBHOOK_URLS: dict[str, str] = {
    "jefe": settings.discord_webhook_jefe,
    "po": settings.discord_webhook_po,
    "tl": settings.discord_webhook_tl,
    "a1": settings.discord_webhook_a1,
    "a2": settings.discord_webhook_a2,
}


async def post_as_role(role: Role, content: str) -> None:
    """
    Publica un mensaje en el canal del webhook del rol, firmado con su display_name.
    """
    url = _WEBHOOK_URLS.get(role.id)
    if not url:
        raise ValueError(f"No hay webhook configurado para el rol '{role.id}'")

    # Discord limita a 2000 caracteres por mensaje.
    # Si excede, partimos en trozos.
    chunks = _split_message(content, limit=1900)

    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            payload = {"content": chunk}
            async with session.post(url, json=payload) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    logger.error(
                        f"Webhook {role.id} falló con {resp.status}: {text[:300]}"
                    )
                    resp.raise_for_status()

    logger.info(f"Publicado como {role.display_name} ({len(content)} chars)")


def _split_message(content: str, limit: int = 1900) -> list[str]:
    """
    Parte un mensaje largo en trozos respetando el límite de Discord (2000).
    Intenta partir por saltos de línea cuando es posible.
    """
    if len(content) <= limit:
        return [content]

    chunks = []
    remaining = content
    while len(remaining) > limit:
        # Intentar partir en el último \n antes del límite
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
