"""
Comandos del bot.
- /reset: borra todo el historial conversacional (con confirmación).
- /pausa: detiene el procesamiento de mensajes por el equipo.
- /resume: reanuda el procesamiento.
- /estado: muestra el estado actual del orquestador.
- /auto on|off|status: gestiona el modo autónomo (Director decide próximo sprint).
Permisos: solo el usuario configurado en DISCORD_MY_USER_ID puede ejecutarlos.
"""
import logging
import discord
from discord.ext import commands

from src.config import settings
from src.history import clear_all, count_messages
from src import channel_logger, state

logger = logging.getLogger(__name__)


ALLOWED_CHANNELS = {
    settings.discord_lobby_channel_id,
}


def _is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == settings.discord_my_user_id


def _channel_allowed(interaction: discord.Interaction) -> bool:
    return interaction.channel_id in ALLOWED_CHANNELS


class ConfirmResetView(discord.ui.View):
    def __init__(self, owner_id: int, message_count: int):
        super().__init__(timeout=30.0)
        self.owner_id = owner_id
        self.message_count = message_count
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Solo quien lanzó el comando puede confirmarlo.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirmar borrado", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        deleted = clear_all()
        await interaction.response.edit_message(
            content=f"🧹 Historial borrado ({deleted} mensajes eliminados).",
            view=self,
        )
        await channel_logger.log(
            f"🧹 **Reset ejecutado** por <@{self.owner_id}> "
            f"({deleted} mensajes borrados)"
        )
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelado. Historial intacto.", view=self
        )
        self.stop()

    async def on_timeout(self):
        if self.confirmed is None:
            for child in self.children:
                child.disabled = True


def register_commands(bot: commands.Bot) -> None:
    guild_obj = discord.Object(id=settings.discord_guild_id)

    @bot.tree.command(
        name="reset",
        description="Borra todo el historial conversacional del equipo (requiere confirmación).",
        guild=guild_obj,
    )
    async def reset(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message(
                "No tienes permiso para ejecutar este comando.", ephemeral=True
            )
            return
        if not _channel_allowed(interaction):
            await interaction.response.send_message(
                "Este comando solo se ejecuta en #lobby.", ephemeral=True
            )
            return

        n = count_messages()
        if n == 0:
            await interaction.response.send_message(
                "El historial ya está vacío.", ephemeral=True
            )
            return

        view = ConfirmResetView(owner_id=interaction.user.id, message_count=n)
        await interaction.response.send_message(
            f"⚠️ Vas a borrar **{n} mensajes** del historial conversacional. ¿Confirmas?",
            view=view,
            ephemeral=False,
        )

    @bot.tree.command(
        name="pausa",
        description="Detiene el procesamiento de mensajes por el equipo.",
        guild=guild_obj,
    )
    async def pausa(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message(
                "No tienes permiso.", ephemeral=True
            )
            return
        if state.is_paused():
            await interaction.response.send_message(
                "⏸️ Ya estaba pausado.", ephemeral=True
            )
            return
        state.set_paused(True)
        await interaction.response.send_message(
            "⏸️ **Orquestador pausado.** Los mensajes siguen guardándose pero el equipo no responde."
        )
        await channel_logger.log(
            f"⏸️ **Pausa activada** por <@{interaction.user.id}>"
        )

    @bot.tree.command(
        name="resume",
        description="Reanuda el procesamiento de mensajes por el equipo.",
        guild=guild_obj,
    )
    async def resume(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message(
                "No tienes permiso.", ephemeral=True
            )
            return
        if not state.is_paused():
            await interaction.response.send_message(
                "▶️ No estaba pausado.", ephemeral=True
            )
            return
        state.set_paused(False)
        await interaction.response.send_message(
            "▶️ **Orquestador reanudado.** El equipo vuelve a trabajar."
        )
        await channel_logger.log(
            f"▶️ **Resume ejecutado** por <@{interaction.user.id}>"
        )

    @bot.tree.command(
        name="estado",
        description="Muestra el estado actual del orquestador.",
        guild=guild_obj,
    )
    async def estado(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message(
                "No tienes permiso.", ephemeral=True
            )
            return
        paused = state.is_paused()
        n_msgs = count_messages()
        chain = state.get_chain_status()
        status = "⏸️ PAUSADO" if paused else "▶️ ACTIVO"
        auto = "🤖 ON" if chain["autonomous_mode"] else "👤 OFF"
        await interaction.response.send_message(
            f"**Estado:** {status}\n"
            f"**Modo autónomo:** {auto}\n"
            f"**Cadena autónoma:** {chain['chain_sprints']}/{chain['max_chain_sprints']} sprints · "
            f"${chain['chain_cost_eur']:.2f}/${chain['max_chain_cost_eur']:.2f}\n"
            f"**Mensajes en historial:** {n_msgs}",
            ephemeral=True,
        )

    # /auto: gestión del modo autónomo
    auto_group = discord.app_commands.Group(
        name="auto",
        description="Gestiona el modo autónomo del orquestador.",
        guild_ids=[settings.discord_guild_id],
    )

    @auto_group.command(name="on", description="Activa el modo autónomo (Director decide próximo sprint).")
    async def auto_on(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message("No tienes permiso.", ephemeral=True)
            return
        if state.is_autonomous():
            chain = state.get_chain_status()
            await interaction.response.send_message(
                f"🤖 Ya estaba ON. Cadena actual: {chain['chain_sprints']}/{chain['max_chain_sprints']} sprints, "
                f"${chain['chain_cost_eur']:.2f} acumulados.",
                ephemeral=True,
            )
            return
        state.enable_autonomous()
        chain = state.get_chain_status()
        await interaction.response.send_message(
            f"🤖 **Modo autónomo ACTIVADO.**\n"
            f"El Director decidirá automáticamente el siguiente sprint tras cada `[ACEPTADO]`.\n"
            f"Límites de seguridad: máx {chain['max_chain_sprints']} sprints consecutivos y "
            f"${chain['max_chain_cost_eur']:.2f} de coste acumulado antes de pausa forzosa."
        )
        await channel_logger.log(
            f"🤖 **Modo autónomo ACTIVADO** por <@{interaction.user.id}>"
        )

    @auto_group.command(name="off", description="Desactiva el modo autónomo. El sistema vuelve a esperar input humano.")
    async def auto_off(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message("No tienes permiso.", ephemeral=True)
            return
        if not state.is_autonomous():
            await interaction.response.send_message("👤 Ya estaba OFF.", ephemeral=True)
            return
        state.disable_autonomous()
        await interaction.response.send_message(
            "👤 **Modo autónomo DESACTIVADO.** El sistema vuelve a esperar tu input tras cada sprint."
        )
        await channel_logger.log(
            f"👤 **Modo autónomo DESACTIVADO** por <@{interaction.user.id}>"
        )

    @auto_group.command(name="status", description="Muestra el estado de la cadena autónoma.")
    async def auto_status(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message("No tienes permiso.", ephemeral=True)
            return
        chain = state.get_chain_status()
        icon = "🤖 ON" if chain["autonomous_mode"] else "👤 OFF"
        await interaction.response.send_message(
            f"**Modo autónomo:** {icon}\n"
            f"**Sprints en cadena:** {chain['chain_sprints']}/{chain['max_chain_sprints']}\n"
            f"**Coste acumulado:** ${chain['chain_cost_eur']:.2f}/${chain['max_chain_cost_eur']:.2f}",
            ephemeral=True,
        )

    bot.tree.add_command(auto_group, guild=guild_obj)
