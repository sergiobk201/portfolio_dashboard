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
# Ceiling on a nota's total costs. B3 fees run a few hundredths of a percent of
# the traded value; 1% plus R$10 (a fixed per-order fee on a tiny nota) clears
# any real nota and still catches a misread amount of trade size.
MAX_FEE_RATE = 0.01
MAX_FEE_FLOOR = 10.0


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


def check_fee_cap(fees: float, volume: float) -> None:
    """Reject total costs no real nota carries.

    Reconciliation proves the trade lines, not the cost block. A D flag read
    on a net-sell nota, for instance, makes cblc_fees return about twice the
    net proceeds instead of a few reais — positive, so nothing else stops it.

    Args:
        fees: The nota's total costs (clearing + Bovespa + other costs).
        volume: Gross traded value (purchases + sales).

    Raises:
        ValueError: fees exceed MAX_FEE_RATE × volume + MAX_FEE_FLOOR.
    """
    cap = MAX_FEE_RATE * volume + MAX_FEE_FLOOR
    if fees > cap:
        raise ValueError(
            f"nota costs {fees:.2f} exceed the {cap:.2f} cap ({MAX_FEE_RATE:.0%} of {volume:.2f} traded "
            f"+ {MAX_FEE_FLOOR:.0f}) — check the cost block (Total CBLC D/C flag?). Nothing loaded."
        )
