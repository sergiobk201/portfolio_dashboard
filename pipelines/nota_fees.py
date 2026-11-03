"""
nota_fees.py
────────────
Cost extraction from a Sinacor-format nota de corretagem (Rico, XP, …).

Pure functions — no PDF or Gmail dependencies — so the arithmetic that feeds
every trade's cost basis is unit-tested on its own.
"""

from __future__ import annotations

import re

DEBIT, CREDIT = "D", "C"


def extract_dc_flag(label: str, text: str) -> str | None:
    """D (debit) / C (credit) flag printed after a nota amount, if present."""
    match = re.search(rf"{label}\s+[\d\.,]+\s*([DC])\b", text)
    return match.group(1) if match else None


def cblc_fees(total_cblc: float, cblc_flag: str | None, buys: float, sells: float) -> float:
    """Clearing fees (liquidação + registro) implied by the CBLC block.

    "Total CBLC" is the NET settlement of the note's operations plus clearing
    fees, printed unsigned with a D/C flag: a debit on net-buy notes, a credit
    on net-sell notes. Fees always add to the debit side, so with signed
    amounts (debit +):  fees = signed_total_cblc - (buys - sells).
    The former formula (total_cblc - buys - sells) returned -fees on sell
    notes and garbage on mixed notes.

    Args:
        total_cblc: Unsigned "Total CBLC" amount.
        cblc_flag: "D", "C", or None when the PDF text carries no flag.
        buys: Gross value of the note's purchases.
        sells: Gross value of the note's sales.

    Returns:
        Non-negative clearing fees, rounded to cents.

    Raises:
        ValueError: The block implies negative fees (misparsed nota).
    """
    net_debit = buys - sells
    if cblc_flag in (DEBIT, CREDIT):
        signed_total = total_cblc if cblc_flag == DEBIT else -total_cblc
        fees = signed_total - net_debit
    else:  # no flag: exact unless a net credit is smaller than the fees
        fees = abs(total_cblc - abs(net_debit))
    fees = round(fees, 2)
    if fees < 0:
        raise ValueError(f"negative clearing fees ({fees:.2f}) — check the nota's CBLC block")
    return fees
