"""
Logger a canales de Discord (#logs, #presupuesto).
Publica eventos técnicos y de coste sin ruido en #lobby.
"""
import logging
from datetime import datetime, timezone

import discord

from src.config import settings

logger = logging.getLogger(__name__)


class ChannelLogger:
    """
    Envoltorio para publicar en los canales de observabilidad.
    Se instancia con el bot de Discord ya conectado.
    """

    def __init__(self, bot: discord.Client):
        self._bot = bot
        self._logs_channel: discord.TextChannel | None = None
        self._budget_channel: discord.TextChannel | None = None

    def bind_channels(self) -> None:
        """Resuelve los canales. Debe llamarse tras on_ready."""
        logs = self._bot.get_channel(settings.discord_logs_channel_id)
        budget = self._bot.get_channel(settings.discord_presupuesto_channel_id)
        if logs is None:
            logger.warning("No se encontró el canal #logs")
        if budget is None:
            logger.warning("No se encontró el canal #presupuesto")
        self._logs_channel = logs
        self._budget_channel = budget

    async def log(self, message: str) -> None:
        """Publica un mensaje en #logs. Silencioso si falla."""
        if self._logs_channel is None:
            return
        try:
            await self._logs_channel.send(message[:1990])
        except Exception:
            logger.exception("Error publicando en #logs")

    async def budget(self, message: str) -> None:
        """Publica un mensaje en #presupuesto."""
        if self._budget_channel is None:
            return
        try:
            await self._budget_channel.send(message[:1990])
        except Exception:
            logger.exception("Error publicando en #presupuesto")


# Instancia global. Se inicializa desde discord_bot.py tras conectar.
_instance: ChannelLogger | None = None


def init(bot: discord.Client) -> None:
    global _instance
    _instance = ChannelLogger(bot)


def bind_channels() -> None:
    if _instance is not None:
        _instance.bind_channels()


async def log(message: str) -> None:
    if _instance is not None:
        await _instance.log(message)


async def budget(message: str) -> None:
    if _instance is not None:
        await _instance.budget(message)


def now_hhmm() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")
