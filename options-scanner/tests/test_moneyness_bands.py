"""Moneyness — the ITM% column, the row shading, and the row order.

Five bands, deep-OTM → deep-ITM: >15% OTM, 5–15% OTM, within 5% (ATM), 5–15%
ITM, >15% ITM. Signed moneyness is a % of spot, + = ITM.

Row ordering lives in test_close_tab_split.py, alongside the split it runs
after — the two must permute the position list together.
"""

import pytest

from options_scanner.tabs.trades import (
    MONEYNESS_BANDS, moneyness_color, signed_moneyness,
)

SLATE, SKY, YELLOW, ORANGE, RED = [c for _u, c, _l in MONEYNESS_BANDS]


@pytest.mark.parametrize("itm,expected", [
    (-40.0, SLATE),    # deep OTM
    (-15.1, SLATE),
    (-15.0, SLATE),    # edge shades OTM-ward
    (-14.9, SKY),
    (-5.1, SKY),
    (-5.0, SKY),       # edge shades OTM-ward
    (-4.9, YELLOW),
    (0.0, YELLOW),     # exactly at the money
    (5.0, YELLOW),
    (5.1, ORANGE),
    (15.0, ORANGE),
    (15.1, RED),
    (60.0, RED),       # deep ITM
])
def test_band_boundaries(itm, expected):
    assert moneyness_color(itm) == expected


def test_unknown_moneyness_is_not_shaded():
    # No spot → no shade, rather than a misleading "far OTM" slate row.
    assert moneyness_color(None) == ""
    assert moneyness_color(float("nan")) == ""
    assert moneyness_color("—") == ""


def test_every_band_has_a_distinct_color():
    colors = [c for _u, c, _l in MONEYNESS_BANDS]
    assert len(set(colors)) == len(colors)


def test_bands_are_ordered_and_open_ended_at_the_itm_end():
    uppers = [u for u, _c, _l in MONEYNESS_BANDS]
    assert uppers[-1] is None, "the deepest-ITM band must catch everything above"
    assert uppers[:-1] == sorted(uppers[:-1]), "bands run deep-OTM → deep-ITM"


# ── signed_moneyness: the figure behind the column ──────────────────────────

def test_the_sign_is_the_contracts_not_your_sides():
    # A $30 call against a $34 spot is in the money whether you're long or
    # short it — the sign flips between calls and puts, nothing else.
    assert signed_moneyness(34.0, 30.0, "C") == pytest.approx(11.76, abs=0.01)
    assert signed_moneyness(34.0, 30.0, "P") == pytest.approx(-11.76, abs=0.01)


def test_at_the_money_is_exactly_zero():
    assert signed_moneyness(34.0, 34.0, "C") == 0.0
    assert signed_moneyness(34.0, 34.0, "P") == 0.0


def test_option_type_is_read_case_insensitively():
    assert signed_moneyness(34.0, 30.0, "c") == signed_moneyness(34.0, 30.0, "C")


@pytest.mark.parametrize("spot", [None, 0, -5.0, float("nan"), "—"])
def test_an_unusable_spot_gives_no_moneyness(spot):
    # None, not 0.0 — the callers show a blank cell and skip the shade, and a
    # zero would claim the leg is sitting exactly at the money.
    assert signed_moneyness(spot, 30.0, "C") is None


def test_a_junk_strike_gives_no_moneyness():
    assert signed_moneyness(34.0, None, "C") is None
    assert signed_moneyness(34.0, "n/a", "C") is None
