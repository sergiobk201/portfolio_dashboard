"""Telegram alert sending: tag-safe truncation, plain-text resend, no token leaks."""

from __future__ import annotations

import pytest
import requests

import alerts
from alerts import MAX_MESSAGE_CHARS, TRUNCATED, fit, plain_text

TOKEN = "123456:TEST-ONLY-TOKEN"


def test_fit_leaves_a_short_message_alone():
    assert fit("<b>ok</b>") == "<b>ok</b>"


def test_fit_cuts_on_a_line_boundary_with_every_tag_closed():
    lines = [f"• <b>AAAA{i}</b> &amp; more\n" for i in range(400)]
    out = fit("".join(lines))
    assert len(out) <= MAX_MESSAGE_CHARS and out.endswith(TRUNCATED)
    kept = out.removesuffix(TRUNCATED) + "\n"
    assert "".join(lines).startswith(kept)  # whole lines only, so no entity or tag is split
    assert kept.count("<b>") == kept.count("</b>")


def test_fit_never_cuts_inside_a_tag_that_spans_lines():
    message = "intro\n<b>opened\nstill bold\nclosed</b>\n" + "tail\n" * 10
    assert fit(message, limit=40) == "intro" + TRUNCATED  # the cut moves back before the <b>
    assert fit("<b>" + "x" * 50 + "</b>", limit=40) == ""  # no clean cut at all
    assert fit("y" * 50, limit=40, markup=False) == "y" * (40 - len(TRUNCATED)) + TRUNCATED


def test_plain_text_strips_tags_and_decodes_entities():
    assert plain_text('<b>A&amp;B</b> &lt;3 <a href="x">link</a>') == "A&B <3 link"


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:  # requests puts the full URL, token included, in the message
            raise requests.HTTPError(f"{self.status_code} Error for url: https://api.telegram.org/bot{TOKEN}/sendMessage")


def _telegram(monkeypatch: pytest.MonkeyPatch, statuses: list[int]) -> list[dict[str, str]]:
    """Configure fake credentials and answer each sendMessage with the next status."""
    monkeypatch.setenv("PORT_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("PORT_BOT_CHAT", "42")
    sent: list[dict[str, str]] = []
    replies = iter(statuses)

    def post(url: str, data: dict[str, str], timeout: float) -> _Response:
        sent.append(data)
        return _Response(next(replies))

    monkeypatch.setattr(alerts.requests, "post", post)
    return sent


def _assert_no_secret(out: str) -> None:
    assert TOKEN not in out and "api.telegram.org" not in out


def test_a_rejected_html_message_is_resent_once_as_plain_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    sent = _telegram(monkeypatch, [400, 200])
    assert alerts.send_telegram("<b>No close</b>: A&amp;B &lt;3") is True
    assert [d.get("parse_mode") for d in sent] == ["HTML", None]
    assert sent[1]["text"] == "No close: A&B <3"
    _assert_no_secret(capsys.readouterr().out)


def test_a_message_without_a_clean_cut_goes_straight_to_plain_text(monkeypatch: pytest.MonkeyPatch):
    sent = _telegram(monkeypatch, [200])
    assert alerts.send_telegram("<b>" + "x\n" * 3000 + "</b>") is True
    assert len(sent) == 1 and "parse_mode" not in sent[0]
    assert len(sent[0]["text"]) <= MAX_MESSAGE_CHARS and sent[0]["text"].endswith(TRUNCATED)


def test_other_http_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    sent = _telegram(monkeypatch, [500])
    assert alerts.send_telegram("<b>x</b>") is False and len(sent) == 1
    out = capsys.readouterr().out
    assert "Telegram send failed (HTTPError)" in out
    _assert_no_secret(out)


def test_network_errors_never_echo_the_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    _telegram(monkeypatch, [])

    def post(url: str, data: dict[str, str], timeout: float) -> _Response:
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(alerts.requests, "post", post)
    assert alerts.send_telegram("<b>x</b>") is False
    _assert_no_secret(capsys.readouterr().out)


def test_unconfigured_telegram_only_prints(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PORT_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PORT_BOT_CHAT", raising=False)
    monkeypatch.setattr(alerts.requests, "post", lambda *a, **kw: pytest.fail("posted without credentials"))
    assert alerts.send_telegram("<b>x</b>") is False
