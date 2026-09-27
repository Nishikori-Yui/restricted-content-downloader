from pathlib import Path
from types import SimpleNamespace

import pytest

from tg_downloader.transfers import PostCopier


class Reader:
    def __init__(self):
        self.downloads = 0

    async def download_media(self, source, *, file, progress_callback=None):
        self.downloads += 1
        path = Path(file) / source.file.name
        path.write_bytes(b"media-data")
        if progress_callback:
            await progress_callback(len(b"media-data"), len(b"media-data"))
        return str(path)


class Sender:
    def __init__(self):
        self.sends = 0

    async def send_file(self, *args, **kwargs):
        self.sends += 1


@pytest.mark.asyncio
async def test_media_cache_is_reused_on_repeat_copy(tmp_path):
    reader = Reader()
    sender = Sender()
    copier = PostCopier(reader, sender, max_file_bytes=1024, media_dir=tmp_path)
    source = SimpleNamespace(
        id=42,
        grouped_id=None,
        message="caption",
        entities=None,
        file=SimpleNamespace(size=10, id=99, name="sample.jpg"),
    )

    async def progress(*_):
        pass

    first = await copier.copy(
        source, SimpleNamespace(id=123), 1000, 1, progress=progress
    )
    second = await copier.copy(
        source, SimpleNamespace(id=123), 1000, 2, progress=progress
    )

    assert first.files == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert reader.downloads == 1
    assert sender.sends == 2
    assert (tmp_path / "123" / f"{42:020d}" / "manifest.json").is_file()


def test_cleanup_preview_and_confirmed_cleanup(tmp_path):
    copier = PostCopier(Reader(), Sender(), max_file_bytes=1024, media_dir=tmp_path)
    item = tmp_path / "123" / "00000000000000000042"
    item.mkdir(parents=True)
    (item / "sample.jpg").write_bytes(b"12345")
    (item / "manifest.json").write_text('{"complete": true}', encoding="utf-8")

    preview = copier.cleanup_preview()

    assert preview["complete"] == 1
    assert preview["bytes"] == len(b"12345") + len('{"complete": true}')
    assert item.exists()

    result = copier.cleanup([preview["items"][0]["path"]])

    assert result["removed"] == 1
    assert result["bytes"] == preview["bytes"]
    assert not item.exists()
