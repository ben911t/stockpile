"""What a working roll would fill at right now.

A roll buys the held leg back and sells the new one as one net order, so the net
depends on which side of each spread you cross. This is what tells you, while an
order sits unfilled, whether your limit is reachable or you're waiting for the
market to come to you.
"""

import pytest

from options_scanner.trade_actions import roll_net_quote as q


def _leg(bid, ask, mid=None):
    return {"bid": bid, "ask": ask, "mid": mid if mid is not None
            else round((bid + ask) / 2, 2)}


def test_net_credit_across_the_spreads():
    # Buy back at 1.00/1.20, sell the new leg at 1.40/1.60.
    net = q(_leg(1.00, 1.20), _leg(1.40, 1.60))
    assert net["worst"] == pytest.approx(0.20)   # sell the bid, pay the ask
    assert net["mid"] == pytest.approx(0.40)     # mid to mid
    assert net["best"] == pytest.approx(0.60)    # sell the ask, pay the bid


def test_worst_is_never_better_than_best():
    net = q(_leg(1.00, 1.20), _leg(1.40, 1.60))
    assert net["worst"] <= net["mid"] <= net["best"]


def test_a_net_debit_is_negative():
    # Rolling into a cheaper leg costs money — the sign matches the order limit.
    net = q(_leg(2.00, 2.20), _leg(1.00, 1.20))
    assert net["mid"] < 0
    assert net["mid"] == pytest.approx(-1.00)


def test_wide_spreads_widen_the_range_not_the_mid():
    tight = q(_leg(1.05, 1.15), _leg(1.45, 1.55))
    wide = q(_leg(0.80, 1.40), _leg(1.20, 1.80))
    assert tight["mid"] == pytest.approx(wide["mid"])
    assert (wide["best"] - wide["worst"]) > (tight["best"] - tight["worst"])


def test_mid_is_derived_when_the_quote_omits_it():
    net = q({"bid": 1.00, "ask": 1.20}, {"bid": 1.40, "ask": 1.60})
    assert net["mid"] == pytest.approx(0.40)


def test_a_one_sided_market_yields_none():
    # No executable net without both sides of both legs — better to say
    # "unavailable" than to print a number derived from a missing side.
    assert q(_leg(1.00, 1.20), {"bid": 0.0, "ask": 1.60}) is None
    assert q({"bid": 1.00, "ask": 0.0}, _leg(1.40, 1.60)) is None


def test_missing_or_junk_quotes_yield_none():
    assert q(None, _leg(1.40, 1.60)) is None
    assert q(_leg(1.00, 1.20), None) is None
    assert q({}, {}) is None
    assert q({"bid": "x", "ask": "y"}, _leg(1.40, 1.60)) is None


def test_results_are_rounded_to_the_cent():
    net = q(_leg(1.004, 1.206), _leg(1.401, 1.607))
    assert all(v == round(v, 2) for v in net.values())


# ── which leg is which ───────────────────────────────────────────────────────
# A rolling record IS the new leg; roll_from carries the one being bought back.
# Getting these backwards would quote the wrong contracts and invert the net.

def test_roll_legs_splits_the_record_from_its_provenance():
    from options_scanner.tabs.rolls import roll_legs
    t = {"ticker": "AMD", "option_type": "C", "strike": 160.0,
         "expiration": "2026-03-20",
         "roll_from": {"option_type": "C", "strike": 150.0,
                       "expiration": "2026-01-16"}}
    close_leg, open_leg = roll_legs(t)
    assert (close_leg["strike"], close_leg["expiration"]) == (150.0, "2026-01-16")
    assert (open_leg["strike"], open_leg["expiration"]) == (160.0, "2026-03-20")
    assert close_leg["ticker"] == open_leg["ticker"] == "AMD"


def test_roll_legs_falls_back_when_provenance_is_missing():
    # An older record without roll_from still yields usable legs rather than
    # raising — the close leg just mirrors the record's right.
    from options_scanner.tabs.rolls import roll_legs
    close_leg, open_leg = roll_legs({"ticker": "AMD", "option_type": "P",
                                     "strike": 30.0, "expiration": "2026-09-18"})
    assert close_leg["option_type"] == "P" and close_leg["strike"] is None
    assert open_leg["strike"] == 30.0
