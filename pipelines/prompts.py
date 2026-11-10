"""Terminal prompts shared by the monthly imports (gmail_automation, dividends_pipeline).

Both run by hand. An unanswered prompt stops the import before its only write,
so no placeholder ever reaches the ledger.
"""

from __future__ import annotations

from collections.abc import Callable


def portfolio_name(answer: str) -> str | None:
    """Upper-case portfolio name, or None for a blank answer."""
    return answer.strip().upper() or None


def ask(prompt: str, parse: Callable[[str], str | None], expected: str) -> str:
    """Prompt until ``parse`` accepts the answer.

    Args:
        prompt: Question shown to the user.
        parse: Answer → accepted value, or None to ask again.
        expected: What a valid answer looks like, for the retry hint.

    Returns:
        The accepted value.

    Raises:
        EOFError: stdin is closed (Ctrl-D, no terminal): the import stops
            before anything is saved, instead of loading a placeholder.
    """
    while True:
        try:
            answer = input(prompt)
        except EOFError:
            raise EOFError(f"no answer to {prompt.strip()!r} (stdin closed) — nothing saved") from None
        value = parse(answer)
        if value:
            return value
        print(f"   {answer.strip()!r} is not {expected} — try again (Ctrl-D stops without saving).")
