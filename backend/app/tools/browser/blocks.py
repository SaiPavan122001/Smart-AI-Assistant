"""Detect bot-check / CAPTCHA interstitials. Pure and deterministic.

The agent never solves or bypasses these. Detecting them lets the runtime stop poking at a challenge page,
report it honestly, and switch to a structured search provider instead.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# (needle in lower-cased visible text, human reason). Text rules only apply to SHORT pages: challenge pages
# are tiny, while a news article that mentions "unusual traffic" is not a challenge.
_TEXT = (
    ("unusual traffic", "unusual-traffic bot check"),
    ("automatically detects requests coming from your computer", "automated-request bot check"),
    ("i'm not a robot", "CAPTCHA"),
    ("i am not a robot", "CAPTCHA"),
    ("verify you are human", "human-verification check"),
    ("verify that you are human", "human-verification check"),
    ("are you a robot", "robot check"),
    ("complete the security check", "security check"),
    ("checking if the site connection is secure", "browser integrity check"),
    ("enable javascript and cookies to continue", "browser integrity check"),
    ("press & hold", "human-verification check"),
)
_TITLE = (
    ("just a moment", "browser integrity check"),
    ("attention required", "browser integrity check"),
    ("captcha", "CAPTCHA"),
    ("are you a robot", "robot check"),
    ("robot check", "robot check"),
    ("verify you are human", "human-verification check"),
)
MAX_CHALLENGE_PAGE_CHARS = 4000


def classify_block(
    url: str, title: str, text: str, *, total_chars: int | None = None
) -> str | None:
    """Return a short reason when the page is a bot/CAPTCHA challenge, else ``None``."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        parts = urlsplit("")
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    if "google." in host and path.startswith("/sorry"):
        return "Google bot check (unusual traffic)"
    if "captcha" in host or path.rstrip("/").endswith("captcha"):
        return "CAPTCHA page"
    low_title = (title or "").lower()
    for needle, reason in _TITLE:
        if needle in low_title:
            return reason
    size = total_chars if total_chars is not None else len(text or "")
    if size <= MAX_CHALLENGE_PAGE_CHARS:
        low = (text or "").lower()
        for needle, reason in _TEXT:
            if needle in low:
                return reason
    return None
