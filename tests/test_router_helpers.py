from tg_downloader.router import BotRouter


def test_redact_logs_removes_configured_secrets(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:secret-token")
    monkeypatch.setenv("TG_USER_SESSION", "session-secret")
    monkeypatch.setenv("TG_API_HASH", "hash-secret")

    redacted = BotRouter._redact_logs(
        "123456:secret-token session-secret hash-secret 987654:abcdefghijklmnopqrst"
    )

    assert "secret-token" not in redacted
    assert "session-secret" not in redacted
    assert "hash-secret" not in redacted
    assert "[REDACTED_BOT_TOKEN]" in redacted
