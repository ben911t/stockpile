"""Stock positions and their covered-call standing.

The Positions tab's stocks table answers one question — "where could I still
sell a call?" — so the classification has to be right at the boundaries: an
odd lot isn't writable, a position written to the last whole lot is done, and
a position with MORE calls than shares is the one genuinely risky state.
"""

import pytest

from options_scanner import trade_actions as ta
from options_scanner.tabs import trades


# ── classification ──────────────────────────────────────────────────────────

def test_no_calls_is_uncovered():
    c = ta.classify_coverage(400, 0)
    assert c["state"] == "uncovered"
    assert c["coverable"] == 4
    assert c["uncovered_shares"] == 400


def test_some_calls_with_a_lot_left_is_partial():
    c = ta.classify_coverage(400, 2)
    assert c["state"] == "partial"
    assert c["coverable"] == 2
    assert c["uncovered_shares"] == 200


def test_written_to_the_last_lot_is_covered():
    c = ta.classify_coverage(400, 4)
    assert c["state"] == "covered"
    assert c["coverable"] == 0
    assert c["uncovered_shares"] == 0


def test_a_stub_under_a_hundred_still_counts_as_covered():
    # 450 shares against 4 calls leaves 50 unwritten, but there is nothing you
    # can do with 50 shares — so it reads as covered, not partial.
    c = ta.classify_coverage(450, 4)
    assert c["state"] == "covered"
    assert c["coverable"] == 0
    assert c["uncovered_shares"] == 50


def test_more_calls_than_shares_is_over_written():
    # The state that matters: 150 shares back only one call, so the second is
    # naked. calls_coverable clamps at 0, which would otherwise make this
    # indistinguishable from fully covered.
    c = ta.classify_coverage(150, 2)
    assert c["state"] == "over_written"
    assert c["naked_calls"] == 1
    assert ta.calls_coverable(150, 2) == 0      # the clamp this guards against


def test_an_odd_lot_is_uncovered_but_unwritable():
    c = ta.classify_coverage(50, 0)
    assert c["state"] == "uncovered"
    assert c["coverable"] == 0


def test_junk_inputs_fail_closed():
    for shares, calls in ((None, 0), ("x", 0), (-100, 0), (100, None),
                          (100, "x"), (100, -3)):
        c = ta.classify_coverage(shares, calls)
        assert c["state"] in ta.COVERAGE_STATES
        assert c["coverable"] >= 0 and c["naked_calls"] >= 0


def test_every_state_is_reachable_and_declared():
    seen = {ta.classify_coverage(sh, n)["state"]
            for sh, n in ((400, 0), (400, 2), (400, 4), (150, 2))}
    assert seen == set(ta.COVERAGE_STATES)


# ── reading the account ─────────────────────────────────────────────────────

def _pos(symbol, qty, avg=10.0, mv=None, asset="EQUITY", **extra):
    inst = {"assetType": asset, "symbol": symbol}
    inst.update(extra.pop("instrument", {}))
    p = {"instrument": inst, "longQuantity": qty, "averagePrice": avg,
         "marketValue": mv if mv is not None else qty * avg}
    p.update(extra)
    return p


def _call(underlying, short_qty):
    return {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                           "underlyingSymbol": underlying,
                           "symbol": f"{underlying}  260918C00100000"},
            "shortQuantity": short_qty, "longQuantity": 0}


def _run(monkeypatch, positions):
    monkeypatch.setattr(ta, "_account_positions", lambda *a, **k: positions)
    return {r["ticker"]: r for r in ta.equity_positions(object())}


def test_it_pairs_shares_with_the_calls_written_on_them(monkeypatch):
    got = _run(monkeypatch, [_pos("AAPL", 400), _call("AAPL", 2)])
    assert got["AAPL"]["state"] == "partial"
    assert got["AAPL"]["coverable"] == 2


def test_options_are_not_listed_as_stock(monkeypatch):
    got = _run(monkeypatch, [_pos("AAPL", 100), _call("AAPL", 1)])
    assert list(got) == ["AAPL"]
    assert got["AAPL"]["shares"] == 100


def test_a_call_with_no_shares_does_not_invent_a_stock_row(monkeypatch):
    # A naked call has no equity position, so it belongs on the option table
    # only — inventing a 0-share row here would imply stock you don't hold.
    assert _run(monkeypatch, [_call("TSLA", 1)]) == {}


def test_two_lots_of_one_ticker_merge_with_a_weighted_cost(monkeypatch):
    got = _run(monkeypatch, [_pos("MSFT", 100, avg=300.0),
                             _pos("MSFT", 300, avg=400.0)])
    assert got["MSFT"]["shares"] == 400
    # (100*300 + 300*400) / 400 = 375
    assert got["MSFT"]["avg_price"] == pytest.approx(375.0)


def test_a_short_stock_position_is_skipped(monkeypatch):
    # Nothing here knows what to do with one, and it can't back a call.
    assert _run(monkeypatch, [_pos("GME", 0, mv=-500.0)]) == {}


def test_schwabs_pl_is_preferred_when_given(monkeypatch):
    got = _run(monkeypatch, [_pos("AAPL", 100, avg=150.0, mv=20000.0,
                                  longOpenProfitLoss=4321.0)])
    assert got["AAPL"]["pl"] == pytest.approx(4321.0)


def test_pl_falls_back_to_market_value_minus_cost(monkeypatch):
    got = _run(monkeypatch, [_pos("AAPL", 100, avg=150.0, mv=20000.0)])
    assert got["AAPL"]["pl"] == pytest.approx(5000.0)


def test_pl_is_unknown_rather_than_zero_without_a_market_value(monkeypatch):
    # Blank beats claiming the position is flat.
    got = _run(monkeypatch, [_pos("AAPL", 100, avg=150.0, mv=0.0)])
    assert got["AAPL"]["pl"] is None


def test_rows_come_back_sorted(monkeypatch):
    got = _run(monkeypatch, [_pos("TSLA", 100), _pos("AAPL", 100),
                             _pos("MSFT", 100)])
    assert list(got) == ["AAPL", "MSFT", "TSLA"]


def test_a_broker_failure_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(ta, "_account_positions", lambda *a, **k: [])
    assert ta.equity_positions(object()) == []


# ── the row shade ───────────────────────────────────────────────────────────

def test_each_state_has_its_own_shade():
    colors = {trades.coverage_color(s, coverable=1) for s in ta.COVERAGE_STATES}
    assert len(colors) == len(ta.COVERAGE_STATES)


def test_an_unwritable_odd_lot_is_not_shaded():
    # It's uncovered, but there's nothing to act on — coloring it like an
    # actionable row would be noise.
    assert trades.coverage_color("uncovered", coverable=0) == ""
    assert trades.coverage_color("uncovered", coverable=2) != ""


def test_the_shades_are_translucent_so_both_themes_work():
    # Same rule as MONEYNESS_BANDS: these overlay the cell's theme background.
    for color, _label, _help in trades.COVERAGE_BANDS.values():
        assert color.startswith("rgba(")


def test_no_shade_is_green():
    # These tables color P/L green; a green row would read as "profitable"
    # rather than "written". Same constraint MONEYNESS_BANDS documents.
    for color, _label, _help in trades.COVERAGE_BANDS.values():
        r, g, b, _a = (float(x) for x in
                       color[color.index("(") + 1:color.index(")")].split(","))
        assert not (g > r + 30 and g > b + 30), f"{color} reads as green"


def test_over_written_is_the_red_one():
    r, g, b, _a = (float(x) for x in
                   trades.COVERAGE_BANDS["over_written"][0]
                   .partition("(")[2].partition(")")[0].split(","))
    assert r > g + 60 and r > b + 60


def test_an_unknown_state_is_not_shaded():
    assert trades.coverage_bg("nonsense") == ""
    assert trades.coverage_bg("covered") .startswith("background-color:")


def test_every_state_has_a_human_label():
    for s in ta.COVERAGE_STATES:
        label = trades.coverage_label(s)
        assert label and label != s and "_" not in label


# ── render-path traps ───────────────────────────────────────────────────────

def _stock_src():
    import inspect
    from options_scanner.tabs import rolls
    return inspect.getsource(rolls._render_stock_positions)


def test_the_stocks_table_does_not_render_a_second_hidden_notice():
    # render_hidden_notice keys its "show these anyway" checkbox on the scope
    # alone, so calling it from both tables on this tab is a duplicate-key
    # crash that takes the whole tab down. The option table owns it; this one
    # respects the filter and points at it.
    assert "settings_ui.render_hidden_notice(" not in _stock_src()
    assert "settings_ui.filter_hidden(" in _stock_src()


def test_hiding_an_underlying_hides_its_shares_too():
    from options_scanner import position_filters as pf
    stock = {"underlying": "AAPL", "ticker": "AAPL", "shares": 400}
    assert pf.matches({"ticker": "AAPL"}, stock)


def test_a_narrower_option_rule_leaves_the_stock_row_alone():
    # "hide AAPL puts" or "hide the $200 strike" describes a leg, not shares.
    from options_scanner import position_filters as pf
    stock = {"underlying": "AAPL", "ticker": "AAPL", "shares": 400}
    assert not pf.matches({"ticker": "AAPL", "option_type": "P"}, stock)
    assert not pf.matches({"ticker": "AAPL", "strike": 200}, stock)


def test_a_blank_pl_does_not_break_the_sign_color():
    # equity_positions returns None when neither P/L nor market value is
    # usable, and that goes straight into the Styler.
    assert trades._sign_color(None) == ""


def test_only_writable_rows_get_a_checkbox():
    # Streamlit's dataframe selection is all-or-nothing per table, so the only
    # way to show a checkbox on some rows is to split them into two tables —
    # the same thing leaderboard._render_calls_by_coverage does.
    src = _stock_src()
    assert 'r["coverable"] >= 1' in src and 'r["coverable"] < 1' in src
    assert "selectable=True" in src and "selectable=False" in src


def test_the_two_tables_have_different_widget_keys():
    # Same key twice is a duplicate-element crash.
    src = _stock_src()
    assert '"stock_positions"' in src and '"stock_positions_locked"' in src


def test_the_picked_row_is_indexed_into_the_selectable_subset():
    # The selectable table renders only the writable rows, so a row index
    # means nothing against the full list. Indexing `rows` here would build a
    # call for whichever position happened to sit at the same index.
    src = _stock_src()
    assert "writable[sel[0]]" in src
    assert "rows[sel[0]]" not in src


def test_the_builder_leads_with_spot_and_the_day_change():
    # The point of showing it: which strikes are worth scanning depends on
    # where the stock is and which way it's going, so it has to come BEFORE
    # the filters — and before the coverable guard, since the quote is worth
    # seeing on a position you can't write against either.
    import inspect
    from options_scanner.tabs import rolls
    src = inspect.getsource(rolls._render_sell_call_detail)
    assert "_day_head_md(spot, pct)" in src
    assert src.index("_day_head_md") < src.index("if coverable < 1:")
    assert src.index("_day_head_md") < src.index("Min OI")


def test_a_missing_day_change_does_not_render_as_a_flat_green_quote():
    # _day_head_md stays plain when there's no change to state; a default of
    # green would claim the stock was up.
    from options_scanner.tabs.trades import _day_head_md
    assert ":green[" not in _day_head_md(377.01, None)
    assert ":green[" in _day_head_md(377.01, 1.2)
    assert ":red[" in _day_head_md(377.01, -1.2)
    # No spot at all → no segment, so the caller drops the line entirely.
    assert _day_head_md(None, None) == ""


def test_the_builder_refuses_a_position_it_cannot_cover():
    # The table now filters these out, so this is a guard — but it's the
    # invariant that actually matters, so it must stay reachable in code.
    import inspect
    from options_scanner.tabs import rolls
    src = inspect.getsource(rolls._render_sell_call_detail)
    assert "if coverable < 1:" in src
    # ...and it must bail before anything can scan or price a trade.
    assert src.index("if coverable < 1:") < src.index("Scan calls")


def test_the_shade_lookup_survives_every_real_state():
    # The Styler calls this per row; an unhandled state would raise mid-render
    # and blank the tab rather than the cell.
    for s in ta.COVERAGE_STATES:
        for coverable in (0, 1, 5):
            assert isinstance(trades.coverage_bg(s, coverable), str)
