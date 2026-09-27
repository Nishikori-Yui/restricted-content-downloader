from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from telethon.utils import get_peer_id

from .links import PostLink

LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "manifest.json"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class TransferError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CopyResult:
    posts: int
    files: int = 0
    cache_hit: bool = False


class PostCopier:
    """Fetch posts with the owner account and deliver them with the bot account."""

    def __init__(
        self,
        reader,
        sender,
        *,
        max_file_bytes: int,
        media_dir: Path = Path("downloads"),
        concurrency: int = 2,
    ):
        self.reader = reader
        self.sender = sender
        self.max_file_bytes = max_file_bytes
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.media_dir.chmod(0o700)
        except OSError:
            LOGGER.debug(
                "Could not restrict media directory permissions", exc_info=True
            )
        self._staging_dir = self.media_dir / ".staging"
        self._staging_dir.mkdir(exist_ok=True)
        try:
            self._staging_dir.chmod(0o700)
        except OSError:
            LOGGER.debug(
                "Could not restrict staging directory permissions", exc_info=True
            )
        self._slots = asyncio.Semaphore(concurrency)
        self._entities = {}
        self._active_cache_dirs: set[Path] = set()

    async def _entity(self, peer_reference):
        if peer_reference not in self._entities:
            self._entities[peer_reference] = await self.reader.get_entity(
                peer_reference
            )
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
            post.id: post
            for post in posts
            if post is not None and getattr(post, "id", None)
        }

    async def copy(
        self, post, peer, destination, reply_to: int, *, progress=None
    ) -> CopyResult:
        async with self._slots:
            if post is None or getattr(post, "empty", False):
                raise TransferError("The post is unavailable to the owner account.")
            if getattr(post, "grouped_id", None):
                sources = await self._album_members(peer, post)
            else:
                sources = [post]
            media_sources = [item for item in sources if getattr(item, "file", None)]
            if not media_sources:
                await self._copy_text(sources[0], destination, reply_to)
                return CopyResult(posts=1)
            files, cache_hit = await self._copy_media(
                sources, peer, destination, reply_to, progress
            )
            return CopyResult(posts=1, files=files, cache_hit=cache_hit)

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
            raise TransferError(
                "The media group could not be read from the source chat."
            )
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

    async def _copy_media(self, sources, peer, destination, reply_to, progress):
        media_sources = [source for source in sources if getattr(source, "file", None)]
        sizes = [self._known_size(source) for source in media_sources]
        for size in sizes:
            if size > self.max_file_bytes:
                raise TransferError(
                    f"A file is larger than the configured limit of {self.max_file_bytes} bytes."
                )
        total_bytes = sum(sizes)
        cache_dir = self._cache_dir(peer, media_sources)
        self._active_cache_dirs.add(cache_dir.resolve())
        staging = None
        try:
            cached = self._load_cached_files(cache_dir, media_sources)
            if cached is not None:
                sizes = [path.stat().st_size for path in cached]
                total_bytes = sum(sizes)
                if progress:
                    await progress(
                        "Using cached files", total_bytes, max(total_bytes, 1)
                    )
                await self._upload_media(
                    cached,
                    media_sources,
                    destination,
                    reply_to,
                    progress,
                    sizes,
                    total_bytes,
                )
                return len(cached), True

            staging = self._staging_dir / uuid.uuid4().hex
            staging.mkdir(parents=True)
            self._active_cache_dirs.add(staging.resolve())
            try:
                downloaded = await self._download_media(
                    media_sources, staging, progress, sizes, total_bytes
                )
                sizes = [path.stat().st_size for path in downloaded]
                total_bytes = sum(sizes)
                self._write_manifest(
                    staging / MANIFEST_NAME,
                    self._manifest(peer, media_sources, downloaded, complete=True),
                )
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(cache_dir)
                self._active_cache_dirs.discard(staging.resolve())
                downloaded = [cache_dir / Path(path).name for path in downloaded]
            except Exception as exc:
                try:
                    self._write_manifest(
                        staging / MANIFEST_NAME,
                        self._manifest(
                            peer, media_sources, [], complete=False, error=str(exc)
                        ),
                    )
                except OSError:
                    LOGGER.debug(
                        "Could not write incomplete cache manifest", exc_info=True
                    )
                raise

            await self._upload_media(
                downloaded,
                media_sources,
                destination,
                reply_to,
                progress,
                sizes,
                total_bytes,
            )
            return len(downloaded), False
        finally:
            self._active_cache_dirs.discard(cache_dir.resolve())
            if staging is not None:
                self._active_cache_dirs.discard(staging.resolve())

    async def _download_media(self, sources, staging, progress, sizes, total_bytes):
        downloaded = []
        offset = 0
        for index, (source, expected_size) in enumerate(zip(sources, sizes), start=1):
            options = {}
            if progress:

                async def report(current, total, *, index=index, offset=offset):
                    await progress(
                        f"Downloading file {index}/{len(sources)}",
                        offset + current,
                        max(total_bytes, total, 1),
                    )

                options["progress_callback"] = report
            path = await self.reader.download_media(
                source, file=str(staging), **options
            )
            if not path:
                raise TransferError(f"Could not download source post {source.id}.")
            local_path = Path(path)
            try:
                file_size = local_path.stat().st_size
            except OSError as exc:
                raise TransferError(
                    "The downloaded file is missing or unreadable."
                ) from exc
            if file_size > self.max_file_bytes:
                raise TransferError(
                    f"A file is larger than the configured limit of {self.max_file_bytes} bytes."
                )
            target = staging / f"{index:03d}_{self._safe_filename(local_path.name)}"
            if local_path != target:
                local_path.rename(target)
            downloaded.append(target)
            offset += file_size if expected_size == 0 else expected_size
        return downloaded

    async def _upload_media(
        self, paths, sources, destination, reply_to, progress, sizes, total_bytes
    ):
        captions = [getattr(source, "message", None) or "" for source in sources]
        entities = [getattr(source, "entities", None) or [] for source in sources]
        options = {}
        if progress:
            if len(paths) == 1:

                async def report(current, total):
                    await progress(
                        "Uploading file 1/1", current, max(total_bytes, total, 1)
                    )
            else:

                async def report(current, total):
                    value = float(current)
                    completed = min(len(paths), int(value))
                    fraction = 0.0 if completed >= len(paths) else value - completed
                    current_bytes = sum(sizes[:completed])
                    if completed < len(paths):
                        current_bytes += fraction * sizes[completed]
                    file_number = min(len(paths), completed + 1)
                    await progress(
                        f"Uploading file {file_number}/{len(paths)}",
                        current_bytes,
                        max(total_bytes, 1),
                    )

            options["progress_callback"] = report
        try:
            await self.sender.send_file(
                destination,
                [str(path) for path in paths] if len(paths) > 1 else str(paths[0]),
                caption=captions if len(paths) > 1 else captions[0],
                formatting_entities=entities if len(paths) > 1 else entities[0],
                reply_to=reply_to,
                force_document=False,
                **options,
            )
        except Exception:
            LOGGER.exception("The bot account could not deliver media")
            raise

    @staticmethod
    def _known_size(source):
        value = getattr(getattr(source, "file", None), "size", None)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else 0

    @staticmethod
    def _safe_filename(name):
        name = Path(name or "media").name
        cleaned = SAFE_NAME.sub("_", name).strip("._")
        return cleaned[:180] or "media"

    def _cache_dir(self, peer, sources):
        try:
            peer_id = get_peer_id(peer)
        except (TypeError, ValueError):
            peer_id = getattr(peer, "id", "unknown")
        first_id = min(int(source.id) for source in sources)
        return self.media_dir / str(peer_id) / f"{first_id:020d}"

    def _manifest(self, peer, sources, paths, *, complete, error=None):
        try:
            peer_id = get_peer_id(peer)
        except (TypeError, ValueError):
            peer_id = getattr(peer, "id", "unknown")
        files = []
        for source, path in zip(sources, paths):
            file_path = Path(path)
            files.append(
                {
                    "message_id": int(source.id),
                    "name": file_path.name,
                    "size": file_path.stat().st_size if file_path.exists() else 0,
                    "media_id": str(getattr(getattr(source, "file", None), "id", "")),
                }
            )
        result = {
            "version": 1,
            "complete": complete,
            "peer_id": str(peer_id),
            "message_ids": [int(source.id) for source in sources],
            "files": files,
            "downloaded_at": datetime.now(UTC).isoformat(),
        }
        if error:
            result["error"] = error[:500]
        return result

    @staticmethod
    def _write_manifest(path, manifest):
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _load_cached_files(self, cache_dir, sources):
        manifest_path = cache_dir / MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not manifest.get("complete"):
                return None
            expected_ids = [int(source.id) for source in sources]
            if manifest.get("message_ids") != expected_ids:
                return None
            entries = manifest.get("files") or []
            if len(entries) != len(sources):
                return None
            paths = []
            for source, entry in zip(sources, entries):
                path = (cache_dir / str(entry["name"])).resolve()
                path.relative_to(cache_dir.resolve())
                if not path.is_file() or path.stat().st_size != int(entry["size"]):
                    return None
                known_size = self._known_size(source)
                if known_size and path.stat().st_size != known_size:
                    return None
                expected_media_id = str(
                    getattr(getattr(source, "file", None), "id", "")
                )
                if entry.get("media_id", "") != expected_media_id:
                    return None
                paths.append(path)
            return paths
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def cleanup_preview(self):
        items = []
        if not self.media_dir.exists():
            return {"items": [], "complete": 0, "incomplete": 0, "bytes": 0}
        candidates = []
        staging = self.media_dir / ".staging"
        if staging.is_dir():
            candidates.extend(path for path in staging.iterdir())
        for peer_dir in self.media_dir.iterdir():
            if peer_dir.name == ".staging" or not peer_dir.is_dir():
                continue
            for item in peer_dir.iterdir():
                if item.is_dir():
                    candidates.append(item)
                else:
                    candidates.append(item)
        for path in candidates:
            resolved = path.resolve()
            if self._is_active(resolved):
                continue
            size = self._tree_size(path)
            complete = path.parent != staging and self._manifest_is_complete(path)
            items.append(
                {
                    "path": str(path.relative_to(self.media_dir)),
                    "complete": complete,
                    "bytes": size,
                }
            )
        return {
            "items": items,
            "complete": sum(item["complete"] for item in items),
            "incomplete": sum(not item["complete"] for item in items),
            "bytes": sum(item["bytes"] for item in items),
        }

    def cleanup(self, relative_paths):
        removed = 0
        freed = 0
        root = self.media_dir.resolve()
        for relative in relative_paths:
            path = (self.media_dir / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if self._is_active(path) or not path.exists():
                continue
            freed += self._tree_size(path)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        return {"removed": removed, "bytes": freed}

    def cache_stats(self):
        preview = self.cleanup_preview()
        return {
            "bytes": preview["bytes"],
            "items": len(preview["items"]),
            "complete": preview["complete"],
            "incomplete": preview["incomplete"],
        }

    def clear_entity_cache(self):
        count = len(self._entities)
        self._entities.clear()
        return count

    def _manifest_is_complete(self, path):
        try:
            return bool(
                json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8")).get(
                    "complete"
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _is_active(self, path):
        return any(
            active == path or active in path.parents or path in active.parents
            for active in self._active_cache_dirs
        )

    @staticmethod
    def _tree_size(path):
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total = 0
        try:
            for child in path.rglob("*"):
                if child.is_file():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total
