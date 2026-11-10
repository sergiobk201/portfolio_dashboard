"""Clearing-fee extraction from the nota's CBLC block."""

from __future__ import annotations

import pytest

from nota_fees import cblc_fees, check_fee_cap, extract_dc_flag


@pytest.mark.parametrize(
    ("total_cblc", "flag", "buys", "sells", "fees"),
    [
        (1000.30, "D", 1000.00, 0.00, 0.30),  # buy note
        (999.70, "C", 0.00, 1000.00, 0.30),  # sell note — the old formula returned -0.30
        (500.40, "D", 1500.00, 1000.00, 0.40),  # mixed, net buy
        (499.60, "C", 1000.00, 1500.00, 0.40),  # mixed, net sell
        (999.70, None, 0.00, 1000.00, 0.30),  # no flag in the PDF text
    ],
)
def test_cblc_fees(total_cblc: float, flag: str | None, buys: float, sells: float, fees: float):
    assert cblc_fees(total_cblc, flag, buys, sells) == pytest.approx(fees)


def test_misparsed_block_raises():
    with pytest.raises(ValueError, match="negative clearing fees"):
        cblc_fees(999.70, "D", 1000.00, 0.00)


@pytest.mark.parametrize(
    ("fees", "volume"),
    [
        (0.35, 1000.00),  # B3 fees: a few hundredths of a percent
        (9.90, 50.00),  # a fixed per-order fee on a tiny nota stays under the floor
        (110.00, 10000.00),  # exactly at the cap
    ],
)
def test_plausible_costs_pass_the_cap(fees: float, volume: float):
    check_fee_cap(fees, volume)


def test_a_flag_misread_on_a_sell_note_trips_the_cap():
    fees = cblc_fees(999.70, "D", 0.00, 1000.00)  # the C of a sell note read as D
    assert fees == pytest.approx(1999.70)  # positive, so cblc_fees cannot catch it
    with pytest.raises(ValueError, match="nota costs 1999.70 exceed the 20.00 cap"):
        check_fee_cap(fees, 1000.00)
    with pytest.raises(ValueError, match="exceed"):
        check_fee_cap(110.01, 10000.00)


def test_extract_dc_flag():
    text = "Resumo Financeiro\nTotal CBLC 1.000,30 D\nTaxa de liquidação 0,25"
    assert extract_dc_flag("Total CBLC", text) == "D"
    assert extract_dc_flag("Total CBLC", "Total CBLC 999,70 C") == "C"
    assert extract_dc_flag("Total CBLC", "Total CBLC 999,70") is None
