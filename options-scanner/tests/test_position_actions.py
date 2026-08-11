"""Which held legs the Positions tab offers a Roll on.

The Close and Roll tabs merged into one Positions tab: one table of every live
option leg, with the action chosen after the row is selected. Roll can't be
offered on every row — it replaces premium you *sold* — so the panel asks
`trade_actions.is_rollable`, the same rule that used to decide which positions
the old Roll tab listed at all.
"""

import pytest

from options_scanner.trade_actions import is_rollable


def _leg(**over):
    leg = {"underlying": "CPNG", "option_type": "C", "strike": 30.0,
           "expiration": "2026-08-21", "quantity": 4, "direction": "short",
           "shares_held": 400}
    leg.update(over)
    return leg


def test_a_covered_short_call_can_be_rolled():
    assert is_rollable(_leg())


def test_a_short_put_can_be_rolled_without_any_shares():
    # Cash-secured, not share-backed — share coverage is a call concept.
    assert is_rollable(_leg(option_type="P", shares_held=0))


def test_a_naked_short_call_cannot_be_rolled():
    # Rolling one needs at least 100 shares of the underlying behind it.
    assert not is_rollable(_leg(shares_held=0))
    assert not is_rollable(_leg(shares_held=99))


def test_exactly_one_round_lot_is_enough():
    assert is_rollable(_leg(shares_held=100))


def test_several_call_legs_share_one_share_pool():
    # 400 shares against three open call legs: each is still rollable. The
    # strict per-leg `covered` flag double-counts the pool, so it must not gate
    # this — that would hide legs the user needs to act on.
    pool = _leg(shares_held=400)
    assert all(is_rollable(dict(pool, strike=k)) for k in (30.0, 35.0, 40.0))


@pytest.mark.parametrize("opt", ["C", "P"])
def test_a_long_leg_is_never_rollable(opt):
    # There's no premium to roll forward, and the Trades P/L model is
    # credit-received — a long's realized P/L would come out inverted.
    assert not is_rollable(_leg(option_type=opt, direction="long"))


def test_direction_is_read_case_insensitively():
    assert is_rollable(_leg(direction=" SHORT "))


def test_a_missing_direction_is_not_rollable():
    # Fail closed: an unknown direction must not open an order builder that
    # assumes you're short.
    assert not is_rollable(_leg(direction=None))
    assert not is_rollable({"option_type": "P"})


def test_junk_share_counts_do_not_pass_the_coverage_check():
    assert not is_rollable(_leg(shares_held="lots"))
    assert not is_rollable(_leg(shares_held=None))
