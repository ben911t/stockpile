"""The IV+pp of the leg you're closing, shown beside the roll target table.

A roll is two trades. The table ranks by the new leg's IV+pp, so the leg being
bought back needs a figure in the same units to be read against — and it only
means anything when it comes from the *same* surface fit as the targets, which
is what these guard.
"""

import pandas as pd
import pytest

from options_scanner import format as fmt
from options_scanner.tabs import rolls, trades


def _chain():
    """A scored chain: two expirations per side, IV+pp as fitted excess."""
    return pd.DataFrame({
        "type": ["call", "call", "call", "put", "put"],
        "expiration": ["2026-08-21", "2026-09-18", "2026-12-18",
                       "2026-08-21", "2026-09-18"],
        "strike": [30.0, 32.0, 35.0, 30.0, 28.0],
        "iv_excess": [0.032, 0.051, 0.018, 0.099, 0.041],
    })


def _pos(**over):
    p = {"underlying": "CPNG", "option_type": "C", "strike": 30.0,
         "expiration": "2026-08-21"}
    p.update(over)
    return p


def test_held_leg_iv_is_read_from_the_scan_frame():
    # 0.032 excess → +3.2 pp, in the same units the table prints.
    assert trades.held_iv_pp(_chain(), _pos()) == pytest.approx(3.2)


def test_held_leg_lookup_matches_side_strike_and_expiration():
    df = _chain()
    # Same strike + expiration, other side.
    assert trades.held_iv_pp(df, _pos(option_type="P")) == pytest.approx(9.9)
    # Same side + expiration, other strike.
    assert trades.held_iv_pp(df, _pos(strike=32.0)) is None
    # Same side + strike, other expiration.
    assert trades.held_iv_pp(df, _pos(expiration="2026-12-18")) is None


def test_strike_matched_with_float_tolerance():
    # Broker strikes round-trip through floats — 30.0 vs 30.000000001.
    assert trades.held_iv_pp(_chain(), _pos(strike=30.000000001)) is not None


def test_missing_leg_yields_none_not_zero():
    # None must not collapse to 0.0 — a phantom "0 pp" closing leg would read
    # as a perfectly fair buyback, wrong in a way that looks plausible.
    assert trades.held_iv_pp(_chain(), _pos(strike=999.0)) is None


def test_unscored_chain_yields_none():
    assert trades.held_iv_pp(_chain().drop(columns=["iv_excess"]), _pos()) is None


def test_nan_excess_yields_none():
    df = _chain()
    df.loc[0, "iv_excess"] = float("nan")
    assert trades.held_iv_pp(df, _pos()) is None


def test_bad_inputs_do_not_raise():
    assert trades.held_iv_pp(None, _pos()) is None
    assert trades.held_iv_pp(_chain(), {}) is None
    assert trades.held_iv_pp(_chain(), _pos(strike="junk")) is None


GREEN, YELLOW, ORANGE, RED = "#16a34a", "#ca8a04", "#ea580c", "#dc2626"


def test_iv_pp_shading_tracks_advantage_not_richness():
    # The leg is being BOUGHT BACK, so cheap-vs-surface is the favorable side.
    assert fmt.iv_pp_color(-8.0) == GREEN     # well below the surface: cheap
    assert fmt.iv_pp_color(-1.5) == YELLOW    # slightly below, inside noise
    assert fmt.iv_pp_color(1.5) == ORANGE     # slightly above, inside noise
    assert fmt.iv_pp_color(8.0) == RED        # paying well above the surface


def test_iv_pp_band_edges():
    # Breaks at the chain table's ±3 pp noise floor; each band owns its edge.
    assert fmt.iv_pp_color(-3.0) == GREEN
    assert fmt.iv_pp_color(-2.99) == YELLOW
    assert fmt.iv_pp_color(0.0) == YELLOW
    assert fmt.iv_pp_color(0.01) == ORANGE
    assert fmt.iv_pp_color(3.0) == ORANGE
    assert fmt.iv_pp_color(3.01) == RED


def test_iv_pp_shading_is_monotonic():
    # Never jump back toward green as the buyback gets more expensive.
    order = [GREEN, YELLOW, ORANGE, RED]
    seen = [fmt.iv_pp_color(v)
            for v in (-20, -5, -3, -2, -0.5, 0, 0.5, 2, 3, 5, 10)]
    assert seen == sorted(seen, key=order.index)


def test_scan_state_key_is_per_position():
    a = rolls._scan_state_key("CPNG_C_30_2026-08-21")
    b = rolls._scan_state_key("CPNG_C_32_2026-08-21")
    assert a != b and a.startswith("roll_scan_result_")
