"""The Close tab's derived money columns: Intrinsic | Time, and P/L.

Time value drives both its half of that column and the Ann% derived from it, so
an error shows up twice — once as a dollar figure and once as a yield. The cases
that matter are where intrinsic isn't zero: a deep ITM leg has almost no time
value left, which is precisely when holding stops paying and only carries
assignment risk.

P/L flips sign with direction, which is the kind of error that reads as
plausible on screen — a losing short shown as a winner.
"""

import pytest

from options_scanner.tabs.trades import _intrinsic_value as intrinsic
from options_scanner.tabs.trades import _position_pl as pl
from options_scanner.tabs.trades import _time_value as tv


# ── calls ────────────────────────────────────────────────────────────────────

def test_otm_call_is_all_time_value():
    # Spot 30, strike 35 → no intrinsic, so the whole mark is extrinsic.
    assert tv(1.20, 30.0, 35.0, "C") == pytest.approx(1.20)


def test_itm_call_nets_out_intrinsic():
    # Spot 40, strike 35 → $5 intrinsic; a $5.80 mark leaves $0.80 of time.
    assert tv(5.80, 40.0, 35.0, "C") == pytest.approx(0.80)


def test_deep_itm_call_has_almost_nothing_left():
    assert tv(10.05, 45.0, 35.0, "C") == pytest.approx(0.05)


# ── puts ─────────────────────────────────────────────────────────────────────

def test_otm_put_is_all_time_value():
    assert tv(0.90, 35.0, 30.0, "P") == pytest.approx(0.90)


def test_itm_put_nets_out_intrinsic():
    # Spot 27, strike 30 → $3 intrinsic; a $3.40 mark leaves $0.40.
    assert tv(3.40, 27.0, 30.0, "P") == pytest.approx(0.40)


def test_at_the_money_is_all_time_value_either_way():
    assert tv(1.50, 30.0, 30.0, "C") == pytest.approx(1.50)
    assert tv(1.50, 30.0, 30.0, "P") == pytest.approx(1.50)


# ── guards ───────────────────────────────────────────────────────────────────

def test_mark_below_intrinsic_floors_at_zero():
    # A wide spread or a stale print can mark an ITM leg under its intrinsic.
    # That's a quote artifact, not negative time value.
    assert tv(4.50, 40.0, 35.0, "C") == 0.0
    assert tv(2.50, 27.0, 30.0, "P") == 0.0


def test_missing_inputs_yield_none_not_zero():
    # None must not read as "no time value left" — that's a real signal here
    # (nothing to decay), so an unknown has to stay blank.
    assert tv(None, 30.0, 35.0, "C") is None
    assert tv(1.20, None, 35.0, "C") is None
    assert tv(float("nan"), 30.0, 35.0, "C") is None
    assert tv(1.20, float("nan"), 35.0, "C") is None


def test_nonpositive_spot_yields_none():
    assert tv(1.20, 0.0, 35.0, "C") is None


def test_unparseable_inputs_do_not_raise():
    assert tv("abc", 30.0, 35.0, "C") is None


def test_option_type_is_case_insensitive_and_defaults_to_put_math():
    # Only "C" takes the call branch; anything else is treated as a put, which
    # matches how the position rows carry option_type.
    assert tv(5.80, 40.0, 35.0, "c") == pytest.approx(0.80)
    assert tv(3.40, 27.0, 30.0, "p") == pytest.approx(0.40)


def test_whole_leg_scaling_is_per_share_times_100_times_contracts():
    # The column multiplies up; this pins the per-share contract it relies on.
    per_share = tv(0.55, 30.0, 35.0, "C")
    assert per_share * 100 * 4 == pytest.approx(220.0)


# ── intrinsic, the other half of the cell ────────────────────────────────────

def test_intrinsic_is_zero_out_of_the_money():
    assert intrinsic(30.0, 35.0, "C") == 0.0     # spot below a call strike
    assert intrinsic(35.0, 30.0, "P") == 0.0     # spot above a put strike


def test_intrinsic_in_the_money():
    assert intrinsic(40.0, 35.0, "C") == pytest.approx(5.0)
    assert intrinsic(27.0, 30.0, "P") == pytest.approx(3.0)


def test_intrinsic_and_time_add_back_to_the_mark():
    # The two halves of the cell must reconcile with the quote they came from.
    mark, spot, strike = 5.80, 40.0, 35.0
    assert (intrinsic(spot, strike, "C")
            + tv(mark, spot, strike, "C")) == pytest.approx(mark)


def test_intrinsic_none_without_a_spot():
    assert intrinsic(None, 35.0, "C") is None
    assert intrinsic(float("nan"), 35.0, "C") is None
    assert intrinsic(0.0, 35.0, "C") is None


# ── P/L (replaces the raw Avg column) ────────────────────────────────────────

def test_short_profits_when_the_mark_falls():
    # Sold 4 at $1.10, now worth $0.55 → (1.10 − 0.55) × 100 × 4.
    assert pl(1.10, 0.55, 4, "short") == pytest.approx(220.0)


def test_short_loses_when_the_mark_rises():
    assert pl(1.10, 2.00, 4, "short") == pytest.approx(-360.0)


def test_long_profits_when_the_mark_rises():
    # The sign flips with direction — a losing short shown as a winner is the
    # failure this guards.
    assert pl(1.10, 2.00, 4, "long") == pytest.approx(360.0)


def test_long_loses_when_the_mark_falls():
    assert pl(1.10, 0.55, 4, "long") == pytest.approx(-220.0)


def test_direction_is_case_insensitive_and_defaults_to_long_math():
    assert pl(1.10, 0.55, 1, " SHORT ") == pytest.approx(55.0)
    assert pl(1.10, 2.00, 1, None) == pytest.approx(90.0)


def test_pl_scales_with_contracts():
    assert pl(1.10, 0.55, 1, "short") == pytest.approx(55.0)
    assert pl(1.10, 0.55, 10, "short") == pytest.approx(550.0)


def test_pl_none_without_a_mark_or_position():
    assert pl(1.10, None, 4, "short") is None
    assert pl(1.10, float("nan"), 4, "short") is None
    assert pl(None, 0.55, 4, "short") is None
    assert pl(1.10, 0.55, 0, "short") is None
    assert pl("abc", 0.55, 4, "short") is None


# ── markdown money helpers (format.py) ───────────────────────────────────────
# Two bare "$" in one markdown string are LaTeX delimiters — Streamlit eats both
# and sets everything between them in math type. Any string carrying more than
# one amount goes through these.

def test_money_md_formats_and_escapes():
    from options_scanner.format import money_md
    assert money_md(12_500) == r"\$12,500"
    assert money_md(1_234_567.89) == r"\$1,234,568"
    assert money_md(1.5, decimals=2) == r"\$1.50"


def test_money_md_handles_junk():
    from options_scanner.format import money_md
    assert money_md(None) == "—"
    assert money_md("abc") == "—"


def test_md_escape_leaves_everything_else_alone():
    from options_scanner.format import md_escape
    assert md_escape("AMD 2026-01-16 $150 PUT") == r"AMD 2026-01-16 \$150 PUT"
    assert md_escape("no money here") == "no money here"


def test_two_amounts_in_one_string_are_both_escaped():
    from options_scanner.format import money_md
    line = f"Cash {money_md(9000)} · with margin {money_md(48000)}"
    assert line.count("$") == line.count("\\$") == 2
