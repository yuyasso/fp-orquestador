import logging
import discord
from discord.ext import commands

from src.config import settings
from src.claude_runner import run_claude

logger = logging.getLogger(__name__)


ORCHESTRATOR_SYSTEM_PROMPT = """Eres el Orquestador de un equipo de desarrollo de software \
especializado en sistemas de trading. Tu rol es dirigir un equipo compuesto por Jefe de Proyecto, \
Product Owner, Tech Lead y dos Analistas de trading.

Por ahora solo estás validando la conexión. Responde al usuario de forma breve y profesional, \
confirmando que has recibido su mensaje y dando un apunte útil si procede. No inventes que has \
consultado a otros miembros del equipo — aún no están activos. Máximo 3 frases."""


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

        async with message.channel.typing():
            try:
                response = await run_claude(
                    prompt=message.content,
                    model="haiku",
                    system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                )
                reply = response.result or "(Respuesta vacía)"
                logger.info(
                    f"Respuesta generada ({response.input_tokens} in / "
                    f"{response.output_tokens} out / ${response.cost_usd:.4f})"
                )
            except Exception as e:
                logger.exception("Error llamando a Claude Code")
                reply = f"⚠️ Error interno: {e}"

        await message.channel.send(reply)

    return bot
