"""Create a Telethon user session string for the owner account."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


def main():
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
    except (KeyError, ValueError) as exc:
        raise SystemExit("Set valid TG_API_ID and TG_API_HASH in .env first.") from exc

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nCopy this complete line into .env:")
        print(f"TG_USER_SESSION={client.session.save()}")
        print("\nKeep the session string private; it grants access to this Telegram account.")


if __name__ == "__main__":
    main()
