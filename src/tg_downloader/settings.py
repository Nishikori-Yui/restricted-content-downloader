from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when a required setting is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    user_session: str
    allowed_user_ids: frozenset[int]
    max_batch: int = 500
    max_file_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def from_environment(cls, env_file: Path | None = None) -> Settings:
        if env_file is None:
            env_file = Path.cwd() / ".env"
        load_dotenv(env_file, override=False)

        api_id = _positive_int("TG_API_ID")
        api_hash = _required("TG_API_HASH")
        bot_token = _required("TG_BOT_TOKEN")
        user_session = _required("TG_USER_SESSION")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", bot_token):
            raise ConfigurationError("TG_BOT_TOKEN is not in Telegram bot-token format")

        raw_user_ids = _required("TG_ALLOWED_USER_IDS")
        try:
            allowed_user_ids = frozenset(
                int(part.strip()) for part in raw_user_ids.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ConfigurationError("TG_ALLOWED_USER_IDS must be comma-separated integers") from exc
        if not allowed_user_ids or any(user_id <= 0 for user_id in allowed_user_ids):
            raise ConfigurationError("TG_ALLOWED_USER_IDS must contain positive Telegram user IDs")

        max_batch = _optional_positive_int("TG_MAX_BATCH", 500)
        max_file_bytes = _optional_positive_int(
            "TG_MAX_FILE_BYTES", 2 * 1024 * 1024 * 1024
        )
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            user_session=user_session,
            allowed_user_ids=allowed_user_ids,
            max_batch=max_batch,
            max_file_bytes=max_file_bytes,
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required setting: {name}")
    return value


def _positive_int(name: str) -> int:
    value = _required(name)
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if number <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return number


def _optional_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if number <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return number
