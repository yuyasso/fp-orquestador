import logging
import discord
from discord.ext import commands

from src.config import settings

logger = logging.getLogger(__name__)


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        logger.info(f"Bot conectado como {bot.user} (id={bot.user.id})")
        guild = bot.get_guild(settings.discord_guild_id)
        if guild:
            logger.info(f"Servidor: {guild.name}")
        else:
            logger.warning("No encuentro el servidor. ¿Está el bot añadido?")

    @bot.event
    async def on_message(message: discord.Message):
        if message.author == bot.user:
            return
        if message.webhook_id is not None:
            return
        if message.channel.id != settings.discord_lobby_channel_id:
            return

        logger.info(f"Mensaje de {message.author.name}: {message.content}")
        await message.channel.send(
            f"Te escucho, {message.author.mention}. "
            f"(Echo de momento, aún sin agentes.)"
        )

    return bot
