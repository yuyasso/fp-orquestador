import logging
import discord
from discord.ext import commands

from src.config import settings
from src.history import init_db
from src.team import handle_user_message

logger = logging.getLogger(__name__)


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        init_db()
        logger.info(f"Bot conectado como {bot.user} (id={bot.user.id})")
        guild = bot.get_guild(settings.discord_guild_id)
        if guild:
            logger.info(f"Servidor: {guild.name}")
        else:
            logger.warning("No encuentro el servidor. ¿Está el bot añadido?")

    @bot.event
    async def on_message(message: discord.Message):
        # Ignorar mensajes del propio bot
        if message.author == bot.user:
            return
        # Ignorar mensajes procedentes de webhooks (los propios agentes)
        if message.webhook_id is not None:
            return
        # Ignorar mensajes de otros bots
        if message.author.bot:
            return
        # Solo atendemos #lobby por ahora
        if message.channel.id != settings.discord_lobby_channel_id:
            return
        # Ignorar mensajes vacíos (ej. adjuntos sin texto)
        if not message.content.strip():
            return

        logger.info(f"Mensaje de {message.author.name}: {message.content}")

        async def notify_clarification(question: str) -> None:
            await message.channel.send(f"🤔 {question}")

        async with message.channel.typing():
            try:
                result = await handle_user_message(
                    user_name=message.author.display_name or message.author.name,
                    user_id=str(message.author.id),
                    content=message.content,
                    notify_clarification=notify_clarification,
                )
                logger.info(
                    f"Turno completo: speakers={result.speakers_invoked} "
                    f"clarify={result.needs_clarification}"
                )
            except Exception as e:
                logger.exception("Error en handle_user_message")
                await message.channel.send(f"⚠️ Error interno: {e}")

    return bot
