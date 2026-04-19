import logging
import discord
from discord.ext import commands

from src.config import settings
from src.history import init_db
from src.team import handle_user_message
from src import channel_logger
from src.commands import register_commands

logger = logging.getLogger(__name__)


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    channel_logger.init(bot)
    register_commands(bot)

    @bot.event
    async def on_ready():
        init_db()
        channel_logger.bind_channels()
        logger.info(f"Bot conectado como {bot.user} (id={bot.user.id})")

        guild = bot.get_guild(settings.discord_guild_id)
        if guild:
            logger.info(f"Servidor: {guild.name}")
            try:
                synced = await bot.tree.sync(guild=guild)
                logger.info(f"Comandos sincronizados en guild: {len(synced)}")
            except Exception:
                logger.exception("Error sincronizando comandos")
        else:
            logger.warning("No encuentro el servidor. ¿Está el bot añadido?")

        await channel_logger.log("🟢 Orquestador online")

    @bot.event
    async def on_message(message: discord.Message):
        if message.author == bot.user:
            return
        if message.webhook_id is not None:
            return
        if message.author.bot:
            return
        if message.channel.id != settings.discord_lobby_channel_id:
            return
        if not message.content.strip():
            return
        # Ignorar comandos slash (llegan por otra vía)
        if message.content.startswith("/"):
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
                    f"Turno completo: route={result.route} "
                    f"speakers={result.speakers_invoked} "
                    f"phases={result.phases_visited} "
                    f"halted={result.halted_reason}"
                )
            except Exception as e:
                logger.exception("Error en handle_user_message")
                await message.channel.send(f"⚠️ Error interno: {e}")
                await channel_logger.log(f"🔴 Error en turno: `{e}`")

    return bot
