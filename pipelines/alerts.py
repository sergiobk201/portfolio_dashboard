"""
alerts.py
─────────
Telegram alerts — the private channel for anything nominal (BRL amounts).

The repository is public and so are its Actions logs, so logs carry only
dates, counts and percentages; amounts go to Telegram. Failures never echo
the exception text: requests errors embed the URL, which contains the token.
"""

from __future__ import annotations

import html
import os
import re

import requests

MAX_MESSAGE_CHARS = 3900  # Telegram hard limit is 4096
TRUNCATED = "\n…(truncated)"
_TAG = re.compile(r"<(/?)[a-zA-Z][^>]*>")


def escape(text: object) -> str:
    """HTML-escape a value for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


def plain_text(message: str) -> str:
    """``message`` without HTML tags and with entities decoded."""
    return html.unescape(_TAG.sub("", message))


def fit(message: str, limit: int = MAX_MESSAGE_CHARS, *, markup: bool = True) -> str:
    """``message`` cut to ``limit`` characters on a line boundary.

    With ``markup`` the cut only falls where every opened tag is closed:
    Telegram rejects the whole message when a tag is cut. Without markup an
    over-long first line is cut mid-line.

    Returns:
        The message, a truncated prefix ending in TRUNCATED, or "" when the
        markup has no clean cut within ``limit``.
    """
    if len(message) <= limit:
        return message
    budget = limit - len(TRUNCATED)
    cut = pos = depth = 0
    for line in message.splitlines(keepends=True):
        if pos + len(line) > budget:
            break
        pos += len(line)
        if markup:
            depth += sum(-1 if m.group(1) else 1 for m in _TAG.finditer(line))
        if depth == 0:
            cut = pos
    if not cut and not markup:
        cut = budget
    return message[:cut].rstrip("\n") + TRUNCATED if cut else ""


def _post(token: str, chat_id: str, text: str, *, html_mode: bool) -> requests.Response:
    """One sendMessage call. The URL embeds the token: never print it or the exception text."""
    data = {"chat_id": chat_id, "text": text}
    if html_mode:
        data["parse_mode"] = "HTML"
    return requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)


def send_telegram(message: str) -> bool:
    """Send an HTML-formatted alert; returns False (never raises) on failure.

    A message Telegram cannot parse (400) or cannot cut cleanly is sent once
    more as plain text, so formatting never costs the alert.
    """
    token = os.environ.get("PORT_BOT_TOKEN")
    chat_id = os.environ.get("PORT_BOT_CHAT")
    if not token or not chat_id:
        print("[alert] Telegram not configured — alert printed only")
        return False
    text = fit(message)
    try:
        resp = _post(token, chat_id, text, html_mode=True) if text else None
        if resp is None or resp.status_code == 400:
            print("[alert] HTML not accepted — resending as plain text")
            resp = _post(token, chat_id, fit(plain_text(message), markup=False), html_mode=False)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[alert] Telegram send failed ({type(exc).__name__})")
        return False
    print("[alert] Telegram alert sent")
    return True
