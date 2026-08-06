"""Unwinding a covered call: close the option AND sell the shares, one order.

The atomicity is the point. As two orders there's a window where the call is
closed but the shares aren't — or worse, the shares are gone and the call is
left naked. As one net-credit order Schwab fills both legs or neither.

Validated against the live API by preview (`preview_order`, which validates
server-side without placing): the exact spec built here comes back ACCEPTED for
both a full and a partial unwind.
"""

import pytest

from options_scanner import trade_actions as ta


def _leg(**over):
    leg = {"underlying": "MRNA", "option_type": "C", "strike": 90.0,
           "expiration": "2028-12-15", "quantity": 2, "direction": "short",
           "shares_held": 200, "avg_price": 16.54}
    leg.update(over)
    return leg


# ── which positions can be unwound ──────────────────────────────────────────

def test_a_fully_covered_short_call_can_be_unwound():
    assert ta.is_unwindable(_leg())


def test_extra_shares_are_fine():
    assert ta.is_unwindable(_leg(shares_held=1000))


def test_a_partially_covered_call_is_not_offered():
    # 2 contracts need 200 shares. With 100, one call is already naked and
    # selling the shares would make both so — the opposite of an unwind.
    assert not ta.is_unwindable(_leg(shares_held=100))


def test_a_naked_call_cannot_be_unwound():
    assert not ta.is_unwindable(_leg(shares_held=0))


def test_a_put_cannot_be_unwound():
    # A cash-secured put has no shares behind it; unwinding one is just closing
    # it, which the Close action already does.
    assert not ta.is_unwindable(_leg(option_type="P"))


def test_a_long_call_cannot_be_unwound():
    assert not ta.is_unwindable(_leg(direction="long"))


def test_junk_inputs_fail_closed():
    assert not ta.is_unwindable(_leg(shares_held="lots"))
    assert not ta.is_unwindable(_leg(quantity=0))
    assert not ta.is_unwindable({})


# ── building the order ──────────────────────────────────────────────────────

def _order(**over):
    kw = {"ticker": "MRNA", "strike": 90.0, "expiration": "2028-12-15",
          "quantity": 2, "net_limit": 40.70, "shares_held": 200}
    kw.update(over)
    return ta.build_unwind_order(**kw)


def test_shares_are_one_hundred_per_contract():
    assert _order().shares == 200
    assert _order(quantity=1).shares == 100


def test_the_net_amount_is_the_whole_package():
    # $40.70/share × 100 × 2 contracts — matches the orderValue Schwab echoed
    # back on the preview ($8,140).
    assert _order().net_amount == pytest.approx(8140.0)


def test_describe_names_both_legs():
    d = _order().describe()
    assert "UNWIND 2 MRNA CALL $90" in d and "200 shares" in d


def test_selling_more_shares_than_are_held_is_refused():
    # THE check that matters: it would leave the remaining calls uncovered.
    with pytest.raises(ValueError, match="only 100 are held"):
        _order(quantity=2, shares_held=100)


def test_a_partial_unwind_within_the_share_count_is_allowed():
    o = _order(quantity=1, shares_held=200)
    assert o.quantity == 1 and o.shares == 100


def test_a_debit_limit_is_refused():
    # A covered call unwind is always a credit — the call can't be worth more
    # than the stock it's written on. A negative limit means a typo or a
    # sign bug, not a trade anyone wants.
    with pytest.raises(ValueError, match="must be a credit"):
        _order(net_limit=-40.70)
    with pytest.raises(ValueError, match="must be a credit"):
        _order(net_limit=0)


def test_zero_contracts_is_refused():
    with pytest.raises(ValueError, match="at least 1 contract"):
        _order(quantity=0)


# ── pricing the package ─────────────────────────────────────────────────────

STOCK = {"bid": 57.02, "ask": 57.40, "mid": 57.20}
OPT = {"bid": 16.10, "ask": 16.90, "mid": 16.50}


def test_the_net_is_stock_proceeds_minus_the_buyback():
    q = ta.unwind_net_quote(STOCK, OPT)
    assert q["mid"] == pytest.approx(57.20 - 16.50, abs=0.01)


def test_worst_case_crosses_both_spreads():
    # Sell the stock at the bid, buy the call back at the ask.
    q = ta.unwind_net_quote(STOCK, OPT)
    assert q["worst"] == pytest.approx(57.02 - 16.90, abs=0.01)


def test_best_case_has_both_sides_come_to_you():
    q = ta.unwind_net_quote(STOCK, OPT)
    assert q["best"] == pytest.approx(57.40 - 16.10, abs=0.01)


def test_the_three_prices_are_ordered():
    q = ta.unwind_net_quote(STOCK, OPT)
    assert q["worst"] < q["mid"] < q["best"]


def test_a_missing_mid_falls_back_to_the_midpoint():
    q = ta.unwind_net_quote({"bid": 57.0, "ask": 57.4},
                            {"bid": 16.0, "ask": 17.0})
    assert q["mid"] == pytest.approx(57.2 - 16.5, abs=0.01)


def test_no_two_sided_market_means_no_price():
    # Rather than pricing off one side and understating what you'd get.
    assert ta.unwind_net_quote({"bid": 57.0}, OPT) is None
    assert ta.unwind_net_quote(STOCK, {"ask": 16.9}) is None
    assert ta.unwind_net_quote(None, OPT) is None


# ── the order that goes to Schwab ───────────────────────────────────────────

class _FakeClient:
    """Captures the built spec instead of sending it."""

    def __init__(self):
        self.spec = None

    def place_order(self, account_hash, spec):
        self.spec = spec.build()
        raise AssertionError("unreachable — _submit_spec is stubbed")


def _build_spec(monkeypatch, order):
    captured = {}

    def _fake_submit(client, account_hash, spec):
        captured["spec"] = spec.build()
        return {"ok": True, "order_id": 1, "error": None}

    monkeypatch.setattr(ta, "_submit_spec", _fake_submit)
    ta.place_unwind_order(_FakeClient(), order, "hash")
    return captured["spec"]


def test_it_is_one_order_with_both_legs(monkeypatch):
    # The whole guarantee: a single order, not two sequential ones. Two legs,
    # one strategy, one price.
    spec = _build_spec(monkeypatch, _order())
    assert spec["orderStrategyType"] == "SINGLE"
    assert len(spec["orderLegCollection"]) == 2


def test_the_legs_are_the_option_buyback_and_the_share_sale(monkeypatch):
    spec = _build_spec(monkeypatch, _order())
    opt, eq = spec["orderLegCollection"]
    assert opt["instruction"] == "BUY_TO_CLOSE"
    assert opt["instrument"]["assetType"] == "OPTION"
    assert opt["instrument"]["symbol"] == "MRNA  281215C00090000"
    assert opt["quantity"] == 2
    assert eq["instruction"] == "SELL"
    assert eq["instrument"]["assetType"] == "EQUITY"
    assert eq["instrument"]["symbol"] == "MRNA"
    assert eq["quantity"] == 200


def test_it_is_priced_as_a_net_credit(monkeypatch):
    # NET_CREDIT is what makes it one package. A plain LIMIT would price the
    # legs independently and lose the all-or-nothing behavior.
    spec = _build_spec(monkeypatch, _order())
    assert spec["orderType"] == "NET_CREDIT"
    assert spec["price"] == "40.70"


def test_a_partial_unwind_keeps_the_hundred_to_one_ratio(monkeypatch):
    spec = _build_spec(monkeypatch, _order(quantity=1))
    opt, eq = spec["orderLegCollection"]
    assert (opt["quantity"], eq["quantity"]) == (1, 100)


def test_an_invalid_order_is_refused_before_it_reaches_the_broker(monkeypatch):
    sent = []
    monkeypatch.setattr(ta, "_submit_spec",
                        lambda *a: sent.append(a) or {"ok": True})
    bad = ta.UnwindOrder(ticker="MRNA", strike=90.0, expiration="2028-12-15",
                         quantity=0, net_limit=40.70)
    res = ta.place_unwind_order(_FakeClient(), bad, "hash")
    assert res["ok"] is False and not sent


# ── the time-value line in the unwind panel ─────────────────────────────────
# Unwinding early hands the call's remaining extrinsic back to whoever sells it
# to you. This line is what says whether that costs anything worth caring about.

from options_scanner.tabs.rolls import _time_value_line as tvl  # noqa: E402


def _line(opt_mid=16.50, spot=57.20, strike=90.0, exp="2028-12-15", n=2):
    return tvl(opt_mid, spot, strike, exp, n)


def test_an_out_of_the_money_call_is_all_time_value():
    # Spot 57.20 under a $90 strike: no intrinsic, so the whole mid is
    # extrinsic — every cent of it forfeited by buying the call back now.
    out = _line()
    assert "\$16.50" in out and "/share" in out


def test_the_whole_leg_figure_scales_with_contracts():
    assert "\$3,300" in _line(n=2)      # 16.50 × 100 × 2
    assert "\$1,650" in _line(n=1)


def test_the_contract_count_is_pluralized():
    assert "on 1 contract ·" in _line(n=1) or _line(n=1).endswith("on 1 contract")
    assert "on 2 contracts" in _line(n=2)


def test_intrinsic_is_excluded():
    # A $40 strike with spot 57.20 is $17.20 in the money; a $16.50 mid is
    # below intrinsic, so there is no time value left to give up.
    out = _line(strike=40.0)
    assert "\$0.00" in out


def _expected_yield(tv, base, exp):
    """The yield the line should print, computed independently of the DTE the
    test happens to run on (these dates move every day)."""
    from datetime import date, datetime
    dte = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days
    return f"{tv / base * (365.0 / dte) * 100:.1f}%/yr"


def test_the_yield_annualizes_over_the_capital_tied_up():
    # NOT against spot: a covered call's committed capital is its net
    # liquidation value, spot − mark = 57.20 − 16.50 = 40.70, which is exactly
    # what unwinding frees. Measuring against spot understates the return on
    # the money actually at work.
    assert _expected_yield(16.50, 40.70, "2028-12-15") in _line()


def test_the_spot_base_would_give_a_different_smaller_number():
    # Guards the change itself: the old convention divided by spot, which on a
    # deep-ITM call understates the yield by roughly spot/net-liq.
    assert _expected_yield(16.50, 57.20, "2028-12-15") not in _line()


def test_a_near_dated_leg_yields_far_more_for_the_same_premium():
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=45)).isoformat()
    assert _expected_yield(16.50, 40.70, soon) in _line(exp=soon)


def test_no_quote_means_no_line():
    # Rather than printing a zero that would read as "nothing left to wait for".
    assert _line(opt_mid=None) is None
    assert _line(spot=None) is None


def test_an_expiring_leg_keeps_the_dollars_and_drops_the_yield():
    # No days left to annualize over — but the extrinsic is still real, and
    # it's exactly the case where you most want to see it.
    from datetime import date
    out = _line(exp=date.today().isoformat())
    assert "\$16.50" in out and "%/yr" not in out


def test_the_line_explains_itself_on_hover():
    assert "cursor:help" in _line() and "Ann%" in _line()


# ── the yield base: what capital a held position actually ties up ───────────
# Changed from spot to net liquidation for covered calls. Candidate options in
# a chain scan deliberately keep the old base — nothing is held yet there.

from options_scanner.tabs.trades import yield_base  # noqa: E402


def test_a_covered_call_is_measured_against_what_unwinding_frees():
    # spot 379.76, call marked 196.28 → 183.48/share of committed capital.
    assert yield_base(379.76, 185.0, "C", 196.28, covered=True) == pytest.approx(
        183.48, abs=0.01)


def test_spot_would_overstate_the_base_on_a_deep_itm_call():
    # The reason for the change: spot is ~2x the capital actually at work, so
    # the old base halved the apparent yield.
    net_liq = yield_base(379.76, 185.0, "C", 196.28, covered=True)
    assert 379.76 / net_liq > 2


def test_an_uncovered_call_keeps_the_spot_base():
    # A naked call's committed capital is broker margin, which this app can't
    # see — better the old rule than an invented answer.
    assert yield_base(379.76, 185.0, "C", 196.28, covered=False) == 379.76


def test_a_put_is_measured_against_the_cash_securing_it():
    # Already a capital-committed base, so unchanged by this work.
    assert yield_base(57.20, 50.0, "P", 2.00, covered=True) == 50.0


def test_a_mark_above_spot_falls_back_rather_than_dividing_by_nothing():
    # Impossible for a real call (it can't be worth more than the stock), so
    # it means bad data — don't produce a wild yield off a near-zero base.
    assert yield_base(57.20, 40.0, "C", 60.0, covered=True) == 57.20


def test_a_missing_mark_falls_back_to_spot():
    assert yield_base(57.20, 40.0, "C", None, covered=True) == 57.20


def test_unusable_inputs_give_no_base():
    assert yield_base(None, 90.0, "C") is None
    assert yield_base(0, 90.0, "C") is None
    assert yield_base("n/a", 90.0, "C") is None


def test_the_chain_scan_is_left_on_the_collateral_base():
    # Candidates aren't held — the base there is what you WOULD commit, so
    # chain_common must not have been switched over with the held positions.
    import inspect
    from options_scanner import chain_common
    src = inspect.getsource(chain_common)
    assert "capital = spot if side == \"call\" else strike" in src
    assert "yield_base" not in src


def test_the_yield_names_the_base_it_is_measured_against():
    # Which capital a yield divides by is the whole question — a bare "2.2%/yr"
    # that only makes sense after hovering is a number that gets misread.
    out = _line()
    assert "yield on net liquidation" in out
    assert "(\$41/share)" in out      # spot 57.20 − mark 16.50


def test_the_base_shown_is_the_one_actually_used():
    # The printed base and the printed percentage must come from the same
    # number, or the line quietly explains itself wrongly.
    from options_scanner.tabs.trades import yield_base
    base = yield_base(57.20, 90.0, "C", 16.50, covered=True)
    out = _line()
    assert f"(\${base:,.0f}/share)" in out
    assert _expected_yield(16.50, base, "2028-12-15") in out
