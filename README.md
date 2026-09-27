# Restricted Content Downloader

Restricted Content Downloader is a private Telegram bot that copies posts from channels and chats its owner account can access. Send a post link to the bot, or use `/bdl` to copy a range of post IDs.

The bot uses two Telegram clients: an owner user account reads the source post, and the bot account sends the copied content to the requesting chat.  **_The owner account must already be able to open the source chat._**

## Features

- Copy text and captions while preserving Telegram formatting.
- Copy photos, videos, audio, documents, and media groups.
- Show download and upload progress, including transferred and total bytes.
- Keep downloaded media in a structured persistent cache and reuse it on repeat requests.
- Accept post links in commands, message text, or forwarded posts.
- Copy a message ID range with `/bdl` and report successful, failed, and skipped IDs when finished.
- Restrict bot use to Telegram user IDs in an allow-list.
- Cancel active transfers and inspect status, resource usage, and logs.
- Preview and confirm media-cache cleanup from the bot.

## Disclaimer

Use this software only with Telegram accounts, chats, and files that you are authorized to access and copy. You are responsible for complying with Telegram's terms, applicable laws, copyright and licensing rules, privacy obligations, and any rules of the source chat. Do not use the bot to bypass access controls, redistribute someone else's work without permission, or expose private information. The maintainers do not provide access to restricted content and are not responsible for how the software is used. This project is not affiliated with Telegram.

## Requirements

- Docker with the Docker Compose plugin, or Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org).
- A bot token from [@BotFather](https://t.me/BotFather).
- A Telegram user session for an account that can access the source chats.

## Configure

Run the following commands from the repository root:

```sh
cp .env.example .env
```

Set the values in `.env`:

| Variable | Purpose |
| --- | --- |
| `TG_API_ID` | Telegram application ID |
| `TG_API_HASH` | Telegram application hash |
| `TG_BOT_TOKEN` | Token created with @BotFather |
| `TG_ALLOWED_USER_IDS` | Comma-separated Telegram user IDs permitted to use the bot |
| `TG_USER_SESSION` | Telethon `StringSession` for the owner user account |
| `TG_MAX_FILE_BYTES` | Per-file size limit in bytes; defaults to 2 GiB |
| `TG_MAX_BATCH` | Maximum number of IDs in a `/bdl` batch; defaults to 500 |
| `TG_MEDIA_DIR` | Persistent downloaded-media cache directory; defaults to `downloads` |

Keep `.env` and the user session private. `.env` is excluded by `.gitignore`; `.env.example` is safe to commit and contains placeholders.

Create a user session interactively with uv:

```sh
uv sync
uv run python -m tg_downloader.session
```

Complete Telegram's login prompts and put the printed `TG_USER_SESSION=...` line in `.env`. This must be a Telethon `StringSession` for a user account, not a bot token or a session string from another client library.

## Run directly with uv

This is the lightest development and single-host deployment path. From the repository root:

```sh
uv sync --locked
uv run python -m tg_downloader
```

Generate the owner session with:

```sh
uv run python -m tg_downloader.session
```

## Run with Docker Compose

Docker is the convenient single-host deployment path. Build the image once, then start it:

```sh
docker compose build telegram-bot
docker compose up -d
docker compose logs -f telegram-bot
```

After changing source code, run `docker compose build telegram-bot` again. If the image is already current, `docker compose up -d` does not rebuild it.

Stop it with:

```sh
docker compose down
```

Compose stores application logs in the `logs/` directory here.
Downloaded media is stored in the `downloads/` directory here. Protect this directory because it contains copies of source media.


## Bot commands

| Command | Action |
| --- | --- |
| `/start` | Show an introduction |
| `/help` | Show command help |
| `/dl <URL>` | Copy one post |
| `/dl` | Copy the post URL in the replied-to message or a forwarded post |
| `/bdl <first URL> <last URL>` | Copy a range of IDs from one chat |
| `/status` | Show active transfers in the current private chat |
| `/cancel` | Cancel transfers in the current private chat |
| `/killall` | Cancel transfers in all private chats |
| `/stats` | Show process, host, disk, and network statistics |
| `/logs` | Send the current application log |
| `/cleanup` | Preview persistent media-cache cleanup |
| `/cleanup confirm` | Confirm the cleanup preview within 60 seconds |

A post URL can also be sent without a command. Requests are handled in private chats and checked against `TG_ALLOWED_USER_IDS`.

Examples:

```text
/dl https://t.me/example_channel/123
/bdl https://t.me/example_channel/100 https://t.me/example_channel/120
/cleanup
/cleanup confirm
```

Media files are downloaded into a staging directory first. Once complete, they are moved into a structured cache under `TG_MEDIA_DIR/<peer-id>/<first-message-id>/` with a `manifest.json` file. Repeating a request reuses a complete, size-checked cache entry. `/cleanup` only previews removable entries; no files are deleted until the matching `/cleanup confirm` is sent within 60 seconds. Active transfers are left untouched.

## Development

Install development dependencies, run tests, and lint from this directory:

```sh
uv sync --extra test --group dev
uv run pytest -q
uv run ruff check src tests
```

## License

Released under the [MIT License](LICENSE).
