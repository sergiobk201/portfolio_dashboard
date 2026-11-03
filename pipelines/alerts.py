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

import requests

MAX_MESSAGE_CHARS = 3900  # Telegram hard limit is 4096


def escape(text: object) -> str:
    """HTML-escape a value for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


def send_telegram(message: str) -> bool:
    """Send an HTML-formatted alert; returns False (never raises) on failure."""
    token = os.environ.get("PORT_BOT_TOKEN")
    chat_id = os.environ.get("PORT_BOT_CHAT")
    if not token or not chat_id:
        print("[alert] Telegram not configured — alert printed only")
        return False
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "\n…(truncated)"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[alert] Telegram send failed ({type(exc).__name__})")
        return False
    print("[alert] Telegram alert sent")
    return True
