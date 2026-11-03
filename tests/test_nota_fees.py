"""Clearing-fee extraction from the nota's CBLC block."""

from __future__ import annotations

import pytest

from nota_fees import cblc_fees, extract_dc_flag


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


def test_extract_dc_flag():
    text = "Resumo Financeiro\nTotal CBLC 1.000,30 D\nTaxa de liquidação 0,25"
    assert extract_dc_flag("Total CBLC", text) == "D"
    assert extract_dc_flag("Total CBLC", "Total CBLC 999,70 C") == "C"
    assert extract_dc_flag("Total CBLC", "Total CBLC 999,70") is None
