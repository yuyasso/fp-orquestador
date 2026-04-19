from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_bot_token: str
    discord_guild_id: int
    discord_lobby_channel_id: int
    discord_anuncios_channel_id: int
    discord_logs_channel_id: int
    discord_presupuesto_channel_id: int
    discord_my_user_id: int

    discord_webhook_jefe: str
    discord_webhook_po: str
    discord_webhook_tl: str
    discord_webhook_a1: str
    discord_webhook_a2: str


settings = Settings()
