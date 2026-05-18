"""
Gate de autorización humana.
Publica un mensaje con botones en #lobby y espera la interacción del usuario.
"""
import asyncio
import logging
import discord

from src.config import settings
from src import channel_logger

logger = logging.getLogger(__name__)


AUTH_TIMEOUT_SECONDS = 30 * 60  # 30 minutos


class AuthorizationView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = AUTH_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.result: str | None = None  # "authorized" | "rejected" | None (timeout)
        self._event = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Solo el responsable del proyecto puede autorizar.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Autorizar ejecución", style=discord.ButtonStyle.success)
    async def authorize(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = "authorized"
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ **Ejecución autorizada por el humano.** El equipo procederá en el siguiente paso del flujo.",
            view=self,
        )
        self._event.set()
        self.stop()

    @discord.ui.button(label="❌ Rechazar", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = "rejected"
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❌ **Ejecución rechazada por el humano.** El equipo no procederá.",
            view=self,
        )
        self._event.set()
        self.stop()

    async def on_timeout(self):
        if self.result is None:
            for child in self.children:
                child.disabled = True
            self._event.set()

    async def wait_decision(self) -> str:
        """Espera a que se pulse un botón o expire. Devuelve 'authorized', 'rejected' o 'timeout'."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=AUTH_TIMEOUT_SECONDS + 5)
        except asyncio.TimeoutError:
            pass
        return self.result or "timeout"


async def request_authorization(channel: discord.TextChannel, plan_summary: str) -> str:
    """
    Publica el gate de autorización en el canal indicado y bloquea hasta que el humano decida
    o haya timeout. Devuelve 'authorized' | 'rejected' | 'timeout'.
    """
    mention = f"<@{settings.discord_my_user_id}>"

    # Trim del plan para que no rompa el límite de Discord
    snippet = plan_summary.strip()
    if len(snippet) > 1500:
        snippet = snippet[:1500] + "\n... (recortado, ver mensaje del TL más arriba)"

    body = (
        f"🛂 **Autorización requerida** {mention}\n\n"
        f"El Tech Lead ha redactado el siguiente plan de ejecución:\n\n"
        f"```\n{snippet}\n```\n"
        f"¿Autorizas la ejecución?"
    )

    view = AuthorizationView(owner_id=settings.discord_my_user_id)
    try:
        await channel.send(body[:1990], view=view)
    except Exception:
        logger.exception("Error publicando gate de autorización")
        return "timeout"

    await channel_logger.log("🛂 Gate de autorización publicado. Esperando decisión humana.")

    decision = await view.wait_decision()

    if decision == "authorized":
        await channel_logger.log("🛂 ✅ Autorización **CONCEDIDA**.")
    elif decision == "rejected":
        await channel_logger.log("🛂 ❌ Autorización **DENEGADA**.")
    else:
        await channel_logger.log("🛂 ⏰ Autorización **EXPIRADA** (timeout 30 min).")

    return decision
