from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>]+", re.IGNORECASE
)
TRAILING_PUNCTUATION = ".,;!?)]}>'\""
RESERVED_ROUTES = {"m", "share", "addstickers", "proxy", "iv", "login", "joinchat"}


class InvalidPostLink(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PostLink:
    peer: str | int
    message_id: int


def parse_post_link(raw_url: str) -> PostLink:
    candidate = (raw_url or "").strip().strip("<>").rstrip(TRAILING_PUNCTUATION)
    if not candidate:
        raise InvalidPostLink("Send a Telegram post URL ending in a message number.")
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parsed = urlsplit(candidate)
        if (parsed.hostname or "").lower() not in TELEGRAM_HOSTS:
            raise ValueError
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments[0] == "c" and len(segments) in {3, 4}:
            channel_number = _positive_number(segments[1])
            if len(segments) == 4:
                _positive_number(segments[2])  # Topic identifier in a forum link.
            message_number = _positive_number(segments[-1])
            return PostLink(peer=int(f"-100{channel_number}"), message_id=message_number)

        if segments[0] == "s" and len(segments) == 3:
            username, message_segment = segments[1], segments[2]
        elif len(segments) == 2:
            username, message_segment = segments
        elif len(segments) == 3:
            username = segments[0]
            _positive_number(segments[1])  # Public forum topic identifier.
            message_segment = segments[2]
        else:
            raise ValueError

        if username.lower() in RESERVED_ROUTES or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{3,31}", username
        ):
            raise ValueError
        return PostLink(peer=username.casefold(), message_id=_positive_number(message_segment))
    except (IndexError, TypeError, ValueError, OverflowError) as exc:
        raise InvalidPostLink("That is not a supported Telegram post URL.") from exc


def find_post_links(text: str | None, entities=None) -> list[str]:
    candidates = [match.group(0) for match in URL_PATTERN.finditer(text or "")]
    candidates.extend(getattr(entity, "url", None) for entity in (entities or []))
    links = []
    for raw_candidate in candidates:
        if not raw_candidate:
            continue
        candidate = raw_candidate.rstrip(TRAILING_PUNCTUATION)
        try:
            parse_post_link(candidate)
        except InvalidPostLink:
            continue
        if candidate not in links:
            links.append(candidate)
    return links


def find_post_link(text: str | None, entities=None) -> str | None:
    links = find_post_links(text, entities)
    return links[0] if links else None


def _positive_number(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("Expected a positive number")
    return number
