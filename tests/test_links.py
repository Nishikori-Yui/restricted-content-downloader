import pytest

from tg_downloader.links import (
    InvalidPostLink,
    PostLink,
    find_post_link,
    find_post_links,
    parse_post_link,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/example_channel/42", PostLink("example_channel", 42)),
        ("t.me/c/3891097885/42", PostLink(-1003891097885, 42)),
        ("https://t.me/c/3891097885/7/42", PostLink(-1003891097885, 42)),
    ],
)
def test_parse_post_link(url, expected):
    assert parse_post_link(url) == expected


def test_find_post_links_deduplicates_and_ignores_invalid_urls():
    text = "https://t.me/example_channel/42 and t.me/example_channel/42 t.me/login/1"
    assert find_post_links(text) == ["https://t.me/example_channel/42", "t.me/example_channel/42"]
    assert find_post_link(text) == "https://t.me/example_channel/42"


def test_parse_post_link_rejects_non_post_routes():
    with pytest.raises(InvalidPostLink):
        parse_post_link("https://t.me/login/1")
