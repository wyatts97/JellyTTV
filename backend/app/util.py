from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")
# Names Windows refuses to create regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def utcnow() -> datetime:
    """Naive UTC 'now' - matches how datetimes are stored in the DB."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalise an aware or naive datetime to naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_twitch_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def xmltv_time(value: datetime) -> str:
    """XMLTV wants `YYYYMMDDHHMMSS +0000`."""
    return value.strftime("%Y%m%d%H%M%S") + " +0000"


def sanitize_filename(name: str, *, max_length: int = 120, fallback: str = "untitled") -> str:
    """Make `name` safe as a single path component on Linux, macOS and Windows."""
    cleaned = unicodedata.normalize("NFKC", name or "")
    cleaned = _INVALID_PATH_CHARS.sub(" ", cleaned)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    # Trailing dots/spaces are illegal on Windows.
    cleaned = cleaned.rstrip(". ")
    if cleaned.upper().split(".")[0] in _RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")
    return cleaned or fallback


def parse_twitch_duration(value: str | None) -> int | None:
    """Twitch returns durations like `3h21m33s`. Returns seconds."""
    if not value:
        return None
    total = 0
    matched = False
    for amount, unit in re.findall(r"(\d+)([hms])", value):
        matched = True
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total if matched else None


def twitch_thumbnail(url: str | None, width: int = 1280, height: int = 720) -> str | None:
    """Twitch thumbnail urls contain `%{width}`/`%{height}` placeholders."""
    if not url:
        return None
    return url.replace("%{width}", str(width)).replace("%{height}", str(height))


def normalise_channel_input(value: str) -> str:
    """Accept a login, a twitch.tv url, or a url with query/path noise."""
    text = (value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:www\.|m\.|go\.)?twitch\.tv/", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    text = text.split("/", 1)[0]
    return text.lower()


def human_bytes(num: int | float | None) -> str:
    if not num:
        return "0 B"
    step = 1024.0
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < step:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= step
    return f"{value:.1f} PiB"
