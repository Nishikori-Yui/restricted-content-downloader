from tg_downloader.router import BotRouter


def test_format_id_list_compresses_consecutive_ranges():
    assert BotRouter._format_id_list([13, 11, 12, 18, 20, 19]) == "11–13, 18–20"


def test_readable_bytes_uses_binary_units():
    assert BotRouter._readable_bytes(1024 * 1024) == "1.0 MB"
