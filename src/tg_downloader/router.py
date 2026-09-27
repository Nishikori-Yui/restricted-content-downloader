from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

import psutil
from telethon import events
from telethon.utils import get_peer_id

from .links import (
    InvalidPostLink,
    PostLink,
    find_post_link,
    find_post_links,
    parse_post_link,
)
from .transfers import PostCopier, TransferError

LOGGER = logging.getLogger(__name__)
BATCH_FETCH_SIZE = 100

START_TEXT = (
    "Hi! Send a Telegram post URL or use `/dl <URL>` to copy its text or media. "
    "The source must be available to the account session configured by the owner."
)
HELP_TEXT = (
    "Commands:\n"
    "`/dl <URL>` — copy one post (or reply to a message containing a post URL).\n"
    "`/bdl <first URL> <last URL>` — copy a range from one chat, up to {max_batch} post IDs.\n"
    "`/status` — show active transfers.\n"
    "`/cancel` — cancel this chat's active transfers.\n"
    "`/killall` — cancel all active transfers.\n"
    "`/stats` — show bot and host resource usage.\n"
    "`/logs` — send the current log file.\n\n"
    "`/cleanup` — preview persistent media cache cleanup.\n"
    "`/cleanup confirm` — confirm the cleanup preview within 60 seconds.\n\n"
    "You can also send a post URL directly. The source account must be able to open it."
)


class BotRouter:
    def __init__(self, bot, copier: PostCopier, allowed_user_ids, *, max_batch: int):
        self.bot = bot
        self.copier = copier
        self.allowed_user_ids = allowed_user_ids
        self.max_batch = max_batch
        self._jobs: dict[int, set[asyncio.Task]] = {}
        self._cleanup_confirmations = {}
        self._started_at = time.monotonic()

    def install(self):
        self.bot.add_event_handler(self.handle, events.NewMessage(incoming=True))

    async def handle(self, event):
        if not event.is_private:
            return
        if event.sender_id not in self.allowed_user_ids:
            await event.reply(
                "This bot is private and is not enabled for your account."
            )
            return

        text = (event.raw_text or "").strip()
        command, _, argument_text = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        if command == "/start":
            await event.reply(START_TEXT, link_preview=False)
        elif command == "/help":
            await event.reply(
                HELP_TEXT.format(max_batch=self.max_batch), link_preview=False
            )
        elif command == "/status":
            jobs = self._jobs.get(event.chat_id, set())
            active = sum(not job.done() for job in jobs)
            await event.reply(f"Active transfers in this chat: {active}.")
        elif command in {"/cancel", "/killall"}:
            active_jobs = (
                (job for chat_jobs in self._jobs.values() for job in chat_jobs)
                if command == "/killall"
                else iter(self._jobs.get(event.chat_id, set()))
            )
            jobs = [job for job in active_jobs if not job.done()]
            for job in jobs:
                job.cancel()
            await event.reply(f"Cancelled {len(jobs)} transfer(s).")
        elif command == "/stats":
            report = await asyncio.to_thread(self._stats_text)
            await event.reply(report)
        elif command == "/logs":
            log_path = Path("logs/bot.log")
            if not log_path.is_file():
                await event.reply("No log file is available yet.")
            else:
                await self._send_redacted_logs(event, log_path)
        elif command == "/cleanup":
            if argument_text.strip().lower() == "confirm":
                await self._confirm_cleanup(event)
            else:
                await self._preview_cleanup(event)
        elif command == "/dl":
            await self._start_single(event, argument_text)
        elif command == "/bdl":
            await self._start_batch(event, argument_text)
        elif text.startswith("/"):
            return
        else:
            message = getattr(event, "message", None)
            url = find_post_link(text, getattr(message, "entities", None))
            if url:
                self._schedule(event, self._copy_one(event, url))
            elif text:
                await event.reply(
                    "Send a Telegram post URL, or use `/help` for instructions."
                )

    async def _start_single(self, event, argument_text):
        url = find_post_link(argument_text)
        message = getattr(event, "message", None)
        if not url:
            url = find_post_link(
                event.raw_text or "", getattr(message, "entities", None)
            )
        if not url:
            replied = await event.get_reply_message()
            if replied:
                reply_message = getattr(replied, "message", None)
                url = find_post_link(
                    replied.raw_text or "",
                    getattr(reply_message, "entities", None),
                )
                if not url:
                    forwarded = self._forwarded_post_link(replied)
                    if forwarded:
                        self._schedule(event, self._copy_one(event, forwarded))
                        return
        if not url:
            await event.reply("Usage: `/dl <Telegram post URL>`")
            return
        self._schedule(event, self._copy_one(event, url))

    async def _start_batch(self, event, argument_text):
        links = find_post_links(argument_text)
        if len(links) < 2:
            message = getattr(event, "message", None)
            for candidate in find_post_links(
                event.raw_text or "", getattr(message, "entities", None)
            ):
                if candidate not in links:
                    links.append(candidate)
        if len(links) != 2:
            await event.reply("Usage: `/bdl <first post URL> <last post URL>`")
            return
        try:
            first = parse_post_link(links[0])
            last = parse_post_link(links[1])
        except InvalidPostLink:
            await event.reply("Both arguments must be Telegram post URLs.")
            return
        if first.peer != last.peer:
            await event.reply("Both URLs must refer to the same channel or chat.")
            return
        if first.message_id > last.message_id:
            await event.reply(
                "The first post ID must be less than or equal to the last post ID."
            )
            return
        count = last.message_id - first.message_id + 1
        if count > self.max_batch:
            await event.reply(f"A batch can contain at most {self.max_batch} post IDs.")
            return
        self._schedule(event, self._copy_batch(event, first, last))

    def _schedule(self, event, coroutine):
        task = asyncio.create_task(coroutine)
        tasks = self._jobs.setdefault(event.chat_id, set())
        tasks.add(task)

        def finished(done_task):
            tasks.discard(done_task)
            if not tasks:
                self._jobs.pop(event.chat_id, None)
            if done_task.cancelled():
                return
            error = done_task.exception()
            if error:
                LOGGER.error(
                    "Transfer task crashed",
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)

    async def _copy_one(self, event, url):
        status = await event.reply("Looking up that post…")
        try:
            link = parse_post_link(url) if isinstance(url, str) else url
            peer, source = await self.copier.fetch(link)
            await status.edit(
                f"Found {link.peer} / ID {link.message_id} · Preparing transfer…"
            )
            result = await self.copier.copy(
                source,
                peer,
                event.chat_id,
                event.id,
                progress=self._progress_reporter(status),
            )
            files = f" · {result.files} file(s)" if result.files else ""
            cache = " · used cache" if result.cache_hit else ""
            await status.edit(
                f"Completed: {link.peer} ID {link.message_id}; "
                f"copied {result.posts} post(s){files}{cache}."
            )
        except asyncio.CancelledError:
            await status.edit("Transfer cancelled.")
            raise
        except Exception as exc:
            LOGGER.exception("Post transfer failed")
            await status.edit(self._friendly_error(exc))

    @staticmethod
    def _forwarded_post_link(message):
        header = getattr(message, "fwd_from", None)
        if not header:
            return None
        source_peer = getattr(header, "from_id", None) or getattr(
            header, "saved_from_peer", None
        )
        message_id = getattr(header, "channel_post", None) or getattr(
            header, "saved_from_msg_id", None
        )
        if source_peer is None or message_id is None:
            return None
        try:
            return PostLink(peer=get_peer_id(source_peer), message_id=message_id)
        except (TypeError, ValueError):
            LOGGER.info("Could not resolve the source of a forwarded message")
            return None

    async def _copy_batch(self, event, first: PostLink, last: PostLink):
        total = last.message_id - first.message_id + 1
        completed = skipped = failed = 0
        processed = 0
        failed_ids = []
        skipped_ids = []
        failed_reasons = Counter()
        skipped_reasons = Counter()
        seen_albums = set()
        current_id = None

        async def create_progress_message(message_id):
            ordinal = message_id - first.message_id + 1
            try:
                return await event.reply(
                    f"({ordinal}/{total}) ID {message_id} · Preparing…"
                )
            except Exception:
                LOGGER.debug("Could not create batch progress message", exc_info=True)
                return None

        async def delete_progress_message(progress_message):
            if progress_message is None:
                return
            try:
                await progress_message.delete()
            except Exception:
                LOGGER.debug("Could not delete batch progress message", exc_info=True)

        async def process_post(message_id, peer, source, *, fetch_failed=False):
            nonlocal completed, skipped, failed, processed, current_id
            processed += 1
            current_id = message_id
            if not fetch_failed and (source is None or getattr(source, "empty", False)):
                skipped += 1
                skipped_ids.append(message_id)
                skipped_reasons["unavailable or deleted"] += 1
                current_id = None
                return
            album_id = getattr(source, "grouped_id", None) if source else None
            if album_id is not None and album_id in seen_albums:
                skipped += 1
                skipped_ids.append(message_id)
                skipped_reasons["part of an already copied album"] += 1
                current_id = None
                return
            if album_id is not None:
                seen_albums.add(album_id)

            progress_message = await create_progress_message(message_id)
            try:
                if fetch_failed:
                    failed += 1
                    failed_ids.append(message_id)
                    failed_reasons["could not fetch message"] += 1
                    return
                result = await self.copier.copy(
                    source,
                    peer,
                    event.chat_id,
                    event.id,
                    progress=self._progress_reporter(
                        progress_message,
                        prefix=f"({message_id - first.message_id + 1}/{total}) ID {message_id}",
                    )
                    if progress_message
                    else None,
                )
                completed += result.posts
            except asyncio.CancelledError:
                current_id = message_id
                raise
            except Exception as exc:
                failed += 1
                failed_ids.append(message_id)
                failed_reasons[self._failure_reason(exc)] += 1
                LOGGER.exception("Could not copy batch item %s", message_id)
            finally:
                await delete_progress_message(progress_message)
                if current_id == message_id and not asyncio.current_task().cancelling():
                    current_id = None

        try:
            for chunk_start in range(
                first.message_id, last.message_id + 1, BATCH_FETCH_SIZE
            ):
                chunk_end = min(chunk_start + BATCH_FETCH_SIZE, last.message_id + 1)
                message_ids = list(range(chunk_start, chunk_end))
                try:
                    peer, source_by_id = await self.copier.fetch_many(
                        first.peer, message_ids
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Batch lookup failed; retrying IDs individually")
                    for message_id in message_ids:
                        try:
                            peer, source = await self.copier.fetch(
                                PostLink(peer=first.peer, message_id=message_id)
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            LOGGER.exception(
                                "Could not fetch batch item %s", message_id
                            )
                            await process_post(
                                message_id,
                                None,
                                None,
                                fetch_failed=True,
                            )
                            continue
                        await process_post(message_id, peer, source)
                else:
                    for message_id in message_ids:
                        await process_post(
                            message_id, peer, source_by_id.get(message_id)
                        )
        except asyncio.CancelledError:
            await event.reply(
                self._batch_status_text(
                    "Batch cancelled",
                    first,
                    last,
                    total=total,
                    processed=processed,
                    current_id=current_id,
                    completed=completed,
                    failed_ids=failed_ids,
                    skipped_ids=skipped_ids,
                    failed_reasons=failed_reasons,
                    skipped_reasons=skipped_reasons,
                )
            )
            raise
        except Exception:
            LOGGER.exception("Batch processing failed")
            await event.reply(
                self._batch_status_text(
                    "Batch stopped: Telegram returned an error",
                    first,
                    last,
                    total=total,
                    processed=processed,
                    current_id=current_id,
                    completed=completed,
                    failed_ids=failed_ids,
                    skipped_ids=skipped_ids,
                    failed_reasons=failed_reasons,
                    skipped_reasons=skipped_reasons,
                )
            )
            return
        await event.reply(
            self._batch_status_text(
                "Batch complete",
                first,
                last,
                total=total,
                processed=processed,
                current_id=None,
                completed=completed,
                failed_ids=failed_ids,
                skipped_ids=skipped_ids,
                failed_reasons=failed_reasons,
                skipped_reasons=skipped_reasons,
            )
        )

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        if isinstance(error, TransferError):
            return str(error)
        return (
            "Could not fetch or send that post. Check the URL, source access, "
            "and the bot's upload limit."
        )

    @staticmethod
    def _progress_reporter(status, prefix=None):
        last_update = 0.0

        async def report(stage, current, total):
            nonlocal last_update
            if total <= 0:
                return
            now = time.monotonic()
            if current < total and now - last_update < 1.2:
                return
            last_update = now
            percent = min(100, current * 100 / total)
            label = f"{prefix} · " if prefix else ""
            text = (
                f"{label}{stage}: {percent:.0f}% "
                f"({BotRouter._readable_bytes(current)} / {BotRouter._readable_bytes(total)})"
            )
            try:
                if status is not None:
                    await status.edit(text)
            except Exception:
                LOGGER.debug("Could not refresh transfer progress", exc_info=True)

        return report

    @classmethod
    def _batch_status_text(
        cls,
        title,
        first,
        last,
        *,
        total,
        processed,
        current_id,
        completed,
        failed_ids,
        skipped_ids,
        failed_reasons=None,
        skipped_reasons=None,
    ):
        if current_id is None:
            current = "Waiting to start" if processed == 0 else "Finished"
        else:
            current = f"ID {current_id} ({min(processed, total)}/{total})"
        lines = [
            f"Source: {first.peer}",
            f"Range: IDs {first.message_id}–{last.message_id} ({total} total)",
            f"Current: {current}",
            f"Processed: {processed}/{total}",
            f"Successful: {completed}",
            f"Failed: {len(failed_ids)}",
            f"Skipped: {len(skipped_ids)}",
        ]
        if title != "Copying" and failed_ids:
            lines.append(f"Failed IDs: {cls._format_id_list(failed_ids)}")
        if title != "Copying" and skipped_ids:
            lines.append(f"Skipped IDs: {cls._format_id_list(skipped_ids)}")
        if title != "Copying" and skipped_reasons:
            lines.append("Skipped reasons:")
            lines.extend(
                f"- {count} {reason}" for reason, count in skipped_reasons.items()
            )
        if title != "Copying" and failed_reasons:
            lines.append("Failed reasons:")
            lines.extend(
                f"- {count} {reason}" for reason, count in failed_reasons.items()
            )
        lines.append(title)
        return "\n".join(lines)

    @staticmethod
    def _failure_reason(error):
        message = str(error).lower()
        if "larger than the configured limit" in message:
            return "file exceeded the configured size limit"
        if "no downloadable media" in message or "no text" in message:
            return "post had no text or downloadable media"
        if "download" in message:
            return "media download failed"
        if "deliver" in message or "send" in message or "upload" in message:
            return "media upload failed"
        return "Telegram or transfer error"

    async def _preview_cleanup(self, event):
        preview = await asyncio.to_thread(self.copier.cleanup_preview)
        self._cleanup_confirmations[event.chat_id] = {
            "expires_at": time.monotonic() + 60,
            "paths": [item["path"] for item in preview["items"]],
        }
        if not preview["items"]:
            await event.reply("Media cache is empty. Nothing will be deleted.")
            return
        await event.reply(
            "Media cache cleanup preview\n\n"
            f"Complete items: {preview['complete']}\n"
            f"Incomplete items: {preview['incomplete']}\n"
            f"Total size: {self._readable_bytes(preview['bytes'])}\n\n"
            "Nothing has been deleted.\n"
            "Reply with `/cleanup confirm` within 60 seconds to continue."
        )

    async def _confirm_cleanup(self, event):
        confirmation = self._cleanup_confirmations.pop(event.chat_id, None)
        if not confirmation or confirmation["expires_at"] < time.monotonic():
            await event.reply(
                "The cleanup preview has expired. Run `/cleanup` first; nothing was deleted."
            )
            return
        result = await asyncio.to_thread(self.copier.cleanup, confirmation["paths"])
        cleared_entities = self.copier.clear_entity_cache()
        await event.reply(
            "Media cache cleared.\n\n"
            f"Removed: {result['removed']} item(s)\n"
            f"Freed: {self._readable_bytes(result['bytes'])}\n"
            f"Entity cache cleared: {cleared_entities} entries\n"
            "Active transfers were left untouched."
        )

    async def _send_redacted_logs(self, event, log_path):
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".log", delete=False
            ) as handle:
                temporary_path = Path(handle.name)
                raw = log_path.read_text(encoding="utf-8", errors="replace")
                handle.write(self._redact_logs(raw))
            await self.bot.send_file(
                event.chat_id,
                str(temporary_path),
                caption="Bot logs (sensitive values redacted)",
                reply_to=event.id,
            )
        except OSError:
            await event.reply("The log file could not be read.")
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _redact_logs(text):
        secrets = [
            os.getenv("TG_BOT_TOKEN", ""),
            os.getenv("TG_USER_SESSION", ""),
            os.getenv("TG_API_HASH", ""),
        ]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", text)

    @staticmethod
    def _format_id_list(message_ids):
        values = sorted(set(message_ids))
        if not values:
            return "-"
        groups = []
        start = previous = values[0]
        for value in values[1:]:
            if value == previous + 1:
                previous = value
                continue
            groups.append((start, previous))
            start = previous = value
        groups.append((start, previous))
        return ", ".join(
            str(start) if start == end else f"{start}–{end}" for start, end in groups
        )

    @staticmethod
    def _readable_bytes(value):
        size = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}"
            size /= 1024

    def _stats_text(self):
        elapsed = max(0, int(time.monotonic() - self._started_at))
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = " ".join(
            f"{amount}{unit}"
            for amount, unit in (
                (days, "d"),
                (hours, "h"),
                (minutes, "m"),
                (seconds, "s"),
            )
            if amount or unit == "s"
        )
        disk = shutil.disk_usage(".")
        process_memory = psutil.Process().memory_info().rss
        system_memory = psutil.virtual_memory().percent
        cpu = psutil.cpu_percent(interval=0.2)
        network = psutil.net_io_counters()
        cache = self.copier.cache_stats()
        active = sum(not job.done() for jobs in self._jobs.values() for job in jobs)
        return (
            "Bot status\n"
            f"Uptime: {uptime}\n"
            f"Active transfers: {active}\n"
            f"Process memory: {self._readable_bytes(process_memory)}\n"
            f"System CPU: {cpu:.1f}% | RAM: {system_memory:.1f}%\n"
            f"Disk free: {self._readable_bytes(disk.free)} / "
            f"{self._readable_bytes(disk.total)}\n"
            f"Media cache: {self._readable_bytes(cache['bytes'])} · {cache['items']} items\n"
            f"Network sent: {self._readable_bytes(network.bytes_sent)} | "
            f"received: {self._readable_bytes(network.bytes_recv)}"
        )
