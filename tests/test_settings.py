from pathlib import Path

import pytest

from tg_downloader.settings import ConfigurationError, Settings


def _clear_settings(monkeypatch):
    for name in (
        "TG_API_ID",
        "TG_API_HASH",
        "TG_BOT_TOKEN",
        "TG_USER_SESSION",
        "TG_ALLOWED_USER_IDS",
        "TG_MAX_BATCH",
        "TG_MAX_FILE_BYTES",
        "TG_MEDIA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_loads_limits_from_env_file(tmp_path: Path, monkeypatch):
    _clear_settings(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TG_API_ID=12345\n"
        "TG_API_HASH=hash\n"
        "TG_BOT_TOKEN=12345:token_value\n"
        "TG_USER_SESSION=session\n"
        "TG_ALLOWED_USER_IDS=10, 20,10\n"
        "TG_MAX_BATCH=12\n"
        "TG_MAX_FILE_BYTES=4096\n",
        encoding="utf-8",
    )
    settings = Settings.from_environment(env_file)
    assert settings.api_id == 12345
    assert settings.allowed_user_ids == frozenset({10, 20})
    assert settings.max_batch == 12
    assert settings.max_file_bytes == 4096
    assert settings.media_dir == Path("downloads")


def test_settings_reads_media_dir(tmp_path: Path, monkeypatch):
    _clear_settings(monkeypatch)
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_BOT_TOKEN", "12345:token_value")
    monkeypatch.setenv("TG_USER_SESSION", "session")
    monkeypatch.setenv("TG_ALLOWED_USER_IDS", "10")
    monkeypatch.setenv("TG_MEDIA_DIR", str(tmp_path / "media"))

    settings = Settings.from_environment(tmp_path / "missing.env")

    assert settings.media_dir == tmp_path / "media"


def test_settings_rejects_invalid_bot_token(tmp_path: Path, monkeypatch):
    _clear_settings(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TG_API_ID=1\nTG_API_HASH=h\nTG_BOT_TOKEN=invalid\n"
        "TG_USER_SESSION=s\nTG_ALLOWED_USER_IDS=1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="TG_BOT_TOKEN"):
        Settings.from_environment(env_file)
