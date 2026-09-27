from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from .links import PostLink

LOGGER = logging.getLogger(__name__)


class TransferError(RuntimeError):
    pass


class PostCopier:
    """Fetch posts with the owner account and deliver them with the bot account."""

    def __init__(self, reader, sender, *, max_file_bytes: int, concurrency: int = 2):
        self.reader = reader
        self.sender = sender
        self.max_file_bytes = max_file_bytes
        self._slots = asyncio.Semaphore(concurrency)
        self._entities = {}

    async def _entity(self, peer_reference):
        if peer_reference not in self._entities:
            self._entities[peer_reference] = await self.reader.get_entity(peer_reference)
        return self._entities[peer_reference]

    async def fetch(self, post_link: PostLink):
        peer = await self._entity(post_link.peer)
        post = await self.reader.get_messages(peer, ids=post_link.message_id)
        if isinstance(post, (list, tuple)):
            post = post[0] if post else None
        return peer, post

    async def fetch_many(self, peer_reference, message_ids: list[int]):
        peer = await self._entity(peer_reference)
        posts = await self.reader.get_messages(peer, ids=message_ids)
        if not isinstance(posts, (list, tuple)):
            posts = [posts] if posts else []
        return peer, {
            post.id: post for post in posts if post is not None and getattr(post, "id", None)
        }

    async def copy(self, post, peer, destination, reply_to: int, *, progress=None) -> int:
        async with self._slots:
            if post is None or getattr(post, "empty", False):
                raise TransferError("The post is unavailable to the owner account.")

            if getattr(post, "grouped_id", None):
                sources = await self._album_members(peer, post)
            else:
                sources = [post]

            if all(not getattr(item, "file", None) for item in sources):
                await self._copy_text(sources[0], destination, reply_to)
                return 1

            await self._copy_media(sources, destination, reply_to, progress)
            return 1

    async def _album_members(self, peer, post):
        start = max(1, post.id - 10)
        requested_ids = list(range(start, post.id + 11))
        candidates = await self.reader.get_messages(peer, ids=requested_ids)
        if not isinstance(candidates, (list, tuple)):
            candidates = [candidates] if candidates else []
        same_group = [
            candidate
            for candidate in candidates
            if candidate and getattr(candidate, "grouped_id", None) == post.grouped_id
        ]
        same_group.sort(key=lambda candidate: candidate.id)
        if not same_group:
            raise TransferError("The media group could not be read from the source chat.")
        return same_group

    async def _copy_text(self, source, destination, reply_to):
        body = getattr(source, "message", None) or ""
        if not body:
            raise TransferError("This post has no text or downloadable media.")
        await self.sender.send_message(
            destination,
            body,
            formatting_entities=getattr(source, "entities", None) or None,
            link_preview=False,
            reply_to=reply_to,
        )

    async def _copy_media(self, sources, destination, reply_to, progress):
        with tempfile.TemporaryDirectory(prefix="tg-copy-") as temp_dir:
            downloaded: list[str] = []
            captions: list[str] = []
            entities = []
            for source in sources:
                source_file = getattr(source, "file", None)
                known_size = getattr(source_file, "size", None)
                if known_size is not None and known_size > self.max_file_bytes:
                    raise TransferError(
                        f"A file is larger than the configured limit of "
                        f"{self.max_file_bytes} bytes."
                    )

            for source in sources:
                if not getattr(source, "file", None):
                    continue
                download_options = {}
                if progress:
                    download_options["progress_callback"] = (
                        lambda current, total: progress("Downloading", current, total)
                    )
                path = await self.reader.download_media(
                    source, file=temp_dir, **download_options
                )
                if not path:
                    raise TransferError(f"Could not download source post {source.id}.")
                local_path = Path(path)
                try:
                    file_size = local_path.stat().st_size
                except OSError as exc:
                    raise TransferError("The downloaded file is missing or unreadable.") from exc
                if file_size > self.max_file_bytes:
                    raise TransferError(
                        f"A file is larger than the configured limit of {self.max_file_bytes} bytes."
                    )
                downloaded.append(str(local_path))
                captions.append(getattr(source, "message", None) or "")
                entities.append(getattr(source, "entities", None) or [])

            if not downloaded:
                raise TransferError("No downloadable media was found in that post.")

            try:
                upload_options = {}
                if progress:
                    upload_options["progress_callback"] = (
                        lambda current, total: progress("Uploading", current, total)
                    )
                await self.sender.send_file(
                    destination,
                    downloaded if len(downloaded) > 1 else downloaded[0],
                    caption=captions if len(downloaded) > 1 else captions[0],
                    formatting_entities=entities if len(downloaded) > 1 else entities[0],
                    reply_to=reply_to,
                    force_document=False,
                    **upload_options,
                )
            except Exception:
                LOGGER.exception("The bot account could not deliver media")
                raise
