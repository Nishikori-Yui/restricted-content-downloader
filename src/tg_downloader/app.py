from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from .router import BotRouter
from .settings import ConfigurationError, Settings
from .transfers import PostCopier


def configure_logging():
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    logging.getLogger("telethon").setLevel(logging.WARNING)


async def run(settings: Settings):
    logger = logging.getLogger(__name__)
    try:
        user_session = StringSession(settings.user_session)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "TG_USER_SESSION is not a valid Telethon session string. "
            "Generate one with `python -m tg_downloader.session`."
        ) from exc
    reader = TelegramClient(
        user_session, settings.api_id, settings.api_hash,
        device_model="Post Copier", app_version="0.1.0",
    )
    sender = TelegramClient(
        StringSession(), settings.api_id, settings.api_hash,
        device_model="Post Copier Bot", app_version="0.1.0",
    )
    try:
        await reader.connect()
        if not await reader.is_user_authorized():
            raise RuntimeError(
                "TG_USER_SESSION is expired or is not a user session; generate a new session."
            )
        owner_identity = await reader.get_me()
        if not owner_identity or owner_identity.bot:
            raise RuntimeError("TG_USER_SESSION must belong to a Telegram user account.")
        await sender.start(bot_token=settings.bot_token)
        bot_identity = await sender.get_me()
        if not bot_identity or not bot_identity.bot:
            raise RuntimeError("TG_BOT_TOKEN did not start a bot account.")

        copier = PostCopier(
            reader, sender, max_file_bytes=settings.max_file_bytes, concurrency=2
        )
        router = BotRouter(
            sender,
            copier,
            settings.allowed_user_ids,
            max_batch=settings.max_batch,
        )
        router.install()
        logger.info("Connected as @%s; polling for bot updates", bot_identity.username)
        await sender.run_until_disconnected()
    finally:
        if sender.is_connected():
            await sender.disconnect()
        if reader.is_connected():
            await reader.disconnect()
        logger.info("Telegram clients disconnected")


def main():
    configure_logging()
    try:
        settings = Settings.from_environment()
        asyncio.run(run(settings))
    except ConfigurationError as exc:
        logging.getLogger(__name__).error("Invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutdown requested")
