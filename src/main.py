import logging
import asyncio

from src.config import settings
from src.discord_bot import build_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main():
    bot = build_bot()
    await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
