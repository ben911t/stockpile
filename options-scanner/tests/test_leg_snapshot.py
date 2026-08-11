"""The leg snapshot shown before you act on a position — close, roll, unwind.

All three screens describe the same kind of thing (one option leg you're about
to buy back), so they share one row builder in `format.leg_rows`. These pin what
it renders, and the two feeds behind it: the fields already riding on the
re-quote, and IV+pp, which costs a chain fetch and a surface fit.
"""

import pandas as pd
import pytest

from options_scanner import format as fmt
from options_scanner import trade_actions as ta
from options_scanner.tabs import trades


def _rows(**over):
    """Row list for a fully-quoted option leg, keyed by label."""
    kw = {"bid": 2.10, "ask": 2.35, "mid": 2.22, "last": 2.20,
          "oi": 1204, "vol": 318}
    kw.update(over)
    return dict(fmt.leg_rows(**kw))


# ── what a leg shows ────────────────────────────────────────────────────────

def test_the_always_present_rows_are_the_quote_itself():
    # Bid/Ask/Mid/Last/OI/Vol render on every screen, whatever else is known.
    labels = [f for f, _ in fmt.leg_rows(2.10, 2.35, 2.22, 2.20, 1204, 318)]
    assert labels == ["Bid", "Ask", "Mid", "Last", "OI", "Vol"]


def test_iv_delta_and_iv_pp_are_added_when_known():
    r = _rows(iv=0.342, delta=0.41, iv_pp=2.5)
    assert r["IV%"] == "34.2%"
    assert r["Delta"] == "0.41"
    assert "+2.5 pp" in r["IV+pp"]


def test_spot_leads_when_supplied():
    # The close builder passes it (it had a Spot on its old one-line quote and
    # dropping it would lose information); the roll's two legs don't.
    labels = [f for f, _ in fmt.leg_rows(2.10, 2.35, 2.22, 2.20, 1204, 318,
                                         spot=377.01)]
    assert labels[0] == "Spot"


def test_iv_pp_comes_last():
    # It reads as a verdict on the raw numbers above it, not another quote
    # field, so it sits at the bottom of the leg.
    labels = [f for f, _ in fmt.leg_rows(2.10, 2.35, 2.22, 2.20, 1204, 318,
                                         iv_pp=2.5, iv=0.342, delta=0.41,
                                         spot=377.01)]
    assert labels[-1] == "IV+pp"


def test_the_optional_rows_vanish_rather_than_showing_blank():
    # A caller that can't supply them gets a shorter table, not em dashes.
    r = _rows()
    for absent in ("Spot", "IV%", "Delta", "IV+pp"):
        assert absent not in r


def test_a_nan_is_treated_as_unknown():
    # pandas hands back NaN for a missing numeric far more often than None, and
    # `None != None` is False — so both need checking before a value is printed
    # as though it were a reading.
    r = _rows(iv=float("nan"), delta=float("nan"), iv_pp=float("nan"),
              spot=float("nan"))
    for absent in ("Spot", "IV%", "Delta", "IV+pp"):
        assert absent not in r


def test_a_missing_quote_renders_dashes_not_zeros():
    # A leg with no market must not read as bid $0.00 — that's a price.
    r = dict(fmt.leg_rows(None, None, None, None, None, None))
    assert set(r.values()) == {"—"}


def test_a_nan_price_is_a_dash_too():
    assert fmt.money_html(float("nan")) == "—"
    assert fmt.money_html(None) == "—"
    assert fmt.money_html(1234.5) == "$1,234.50"


def test_money_html_does_not_escape_the_dollar():
    # It lands in a raw-HTML cell where `$` is never a LaTeX delimiter; an
    # escaped `\\$` would render the backslash literally. This is the one
    # difference from money_md.
    assert "\\" not in fmt.money_html(10)


def test_the_last_print_time_rides_under_the_price():
    # A stale leg has to be obvious while you're pricing against it.
    r = _rows(last_ms=1754400000000, fmt_last_et=ta.fmt_last_trade_et)
    assert "$2.20" in r["Last"] and "<br>" in r["Last"]


def test_no_formatter_means_no_timestamp():
    # `fmt_last_et` is injected so `format` stays a leaf module; without it the
    # price still renders, just bare.
    r = _rows(last_ms=1754400000000)
    assert r["Last"] == "$2.20"


def test_iv_pp_is_colored_by_sign():
    # Every screen here is a BUYBACK, so below the surface (cheap to close) is
    # green and above it (paying up) is red — the opposite of the sell-side
    # reading, and consistent across close, roll and unwind.
    assert fmt.iv_pp_color(-6.0) == "#16a34a"
    assert fmt.iv_pp_color(6.0) == "#dc2626"
    assert fmt.iv_pp_color(0.0) != fmt.iv_pp_color(6.0)


def test_noise_never_renders_as_a_strong_signal():
    # ±3pp is the chain table's noise floor; inside it the color must not be
    # either extreme.
    for pp in (-2.9, 0.0, 2.9):
        assert fmt.iv_pp_color(pp) not in ("#16a34a", "#dc2626")


# ── laying the leg out in two pairs ─────────────────────────────────────────

def _lines(html):
    """Each <tr> as a flat list of its cell texts, tags stripped."""
    import re
    return [[re.sub(r"<[^>]+>", "", c)
             for c in re.findall(r"<td[^>]*>(.*?)</td>", tr)]
            for tr in re.findall(r"<tr>(.*?)</tr>", html)]


def test_one_pair_is_still_the_default():
    # Three callers outside the leg snapshots (the Sell Put terms/prices
    # tables) rely on the single-column shape.
    rows = [("A", "1"), ("B", "2"), ("C", "3")]
    assert len(_lines(fmt.kv_table_html(rows))) == 3


def test_two_pairs_halves_the_height():
    rows = [(chr(65 + i), str(i)) for i in range(10)]
    assert len(_lines(fmt.kv_table_html(rows, pairs=2))) == 5


def test_filling_is_column_major():
    # Row-major would zigzag A,B / C,D and interleave prices with analytics.
    # Column-major keeps each column in the order the caller built it.
    rows = [(chr(65 + i), str(i)) for i in range(6)]
    got = [[c for c in line] for line in _lines(fmt.kv_table_html(rows, pairs=2))]
    assert got[0] == ["A", "0", "D", "3"]
    assert got[1] == ["B", "1", "E", "4"]
    assert got[2] == ["C", "2", "F", "5"]


def test_the_prices_and_the_analytics_land_in_separate_columns():
    # The point of column-major on a full leg: Spot/Bid/Ask/Mid/Last down one
    # side, IV%/Delta/OI/Vol/IV+pp down the other.
    rows = fmt.leg_rows(2.10, 2.35, 2.22, 2.20, 1204, 318, iv_pp=2.5,
                        iv=0.342, delta=0.41, spot=377.01)
    left = [line[0] for line in _lines(fmt.kv_table_html(rows, pairs=2))]
    right = [line[2] for line in _lines(fmt.kv_table_html(rows, pairs=2))]
    assert left == ["Spot", "Bid", "Ask", "Mid", "Last"]
    assert right == ["IV%", "Delta", "OI", "Vol", "IV+pp"]


def test_an_odd_count_pads_rather_than_reflowing():
    # A short final column gets empty cells; it must not pull a value up from
    # the other column and break the reading order.
    rows = [(chr(65 + i), str(i)) for i in range(7)]
    lines = _lines(fmt.kv_table_html(rows, pairs=2))
    assert len(lines) == 4
    assert lines[3] == ["D", "3", "", ""]


def test_every_pair_after_the_first_gets_a_gutter():
    # Without extra left padding four columns read as one jumble.
    html = fmt.kv_table_html([("A", "1"), ("B", "2")], pairs=2)
    assert "4px 14px 4px 28px" in html      # second pair's label
    assert "4px 14px 4px 14px" in html      # first pair's label


def test_a_single_row_still_renders():
    assert len(_lines(fmt.kv_table_html([("A", "1")], pairs=2))) == 1


def test_no_rows_is_an_empty_table():
    assert _lines(fmt.kv_table_html([], pairs=2)) == []


# ── IV+pp: the one field that costs a fetch ─────────────────────────────────

def _pos(**over):
    p = {"underlying": "MRNA", "option_type": "C", "strike": 90.0,
         "expiration": "2099-12-15", "quantity": 2, "shares_held": 200}
    p.update(over)
    return p


def _patch_fetch(monkeypatch, fn):
    """leg_iv_pp imports fetch_and_enrich locally, so patch it at its source."""
    import options_scanner.fetch as f
    monkeypatch.setattr(f, "fetch_and_enrich", fn)


def test_an_expired_leg_never_reaches_the_network(monkeypatch):
    # Nothing to fit against, so don't spend ~2s finding that out.
    calls = []
    _patch_fetch(monkeypatch,
                 lambda *a, **k: calls.append(a) or (None, [], None))
    assert trades.leg_iv_pp(_pos(expiration="2020-01-01"),
                            "MRNA", "schwab", {}) is None
    assert calls == []


def test_a_failed_scan_drops_the_row_instead_of_the_panel(monkeypatch):
    # IV+pp is context, not a precondition for placing the order. A throttled
    # chain must not stop you closing a position.
    _patch_fetch(monkeypatch, lambda *a, **k: (None, [], "rate limited"))
    assert trades.leg_iv_pp(_pos(), "MRNA", "schwab", {}) is None


def test_a_raising_provider_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("Schwab unreachable")
    _patch_fetch(monkeypatch, _boom)
    assert trades.leg_iv_pp(_pos(), "MRNA", "schwab", {}) is None


def test_the_held_leg_is_found_in_the_fitted_chain(monkeypatch):
    df = pd.DataFrame({
        "type": ["call", "call"], "expiration": ["2099-12-15", "2099-12-15"],
        "strike": [90.0, 95.0], "iv_excess": [0.031, 0.010],
    })
    _patch_fetch(monkeypatch, lambda *a, **k: (df, [], None))
    assert trades.leg_iv_pp(_pos(), "MRNA", "schwab", {}) == pytest.approx(3.1)


def test_a_put_is_not_matched_against_a_call(monkeypatch):
    # Same strike and expiration on both sides of the chain — picking the wrong
    # side would report a number from the other wing of the smile.
    df = pd.DataFrame({
        "type": ["call", "put"], "expiration": ["2099-12-15"] * 2,
        "strike": [90.0, 90.0], "iv_excess": [0.031, -0.020],
    })
    _patch_fetch(monkeypatch, lambda *a, **k: (df, [], None))
    got = trades.leg_iv_pp(_pos(option_type="P"), "MRNA", "schwab", {})
    assert got == pytest.approx(-2.0)


def test_a_leg_missing_from_the_chain_is_unknown_not_zero(monkeypatch):
    df = pd.DataFrame({
        "type": ["call"], "expiration": ["2099-12-15"], "strike": [95.0],
        "iv_excess": [0.010],
    })
    _patch_fetch(monkeypatch, lambda *a, **k: (df, [], None))
    assert trades.leg_iv_pp(_pos(), "MRNA", "schwab", {}) is None


def test_the_fetch_window_reaches_past_the_held_leg(monkeypatch):
    # A LEAP sits beyond the 400-DTE default. Fetching a window that excludes
    # the leg would fit a surface it isn't on and silently return None.
    seen = {}

    def _spy(ticker, opt_type, min_dte, max_dte, **k):
        seen.update(opt_type=opt_type, min_dte=min_dte, max_dte=max_dte)
        return (None, [], "stop here")

    _patch_fetch(monkeypatch, _spy)
    trades.leg_iv_pp(_pos(expiration="2099-12-15"), "MRNA", "schwab", {})
    held_dte = (pd.Timestamp("2099-12-15").date() - pd.Timestamp.today().date()).days
    assert seen["min_dte"] == 0
    assert seen["max_dte"] > held_dte
    assert seen["opt_type"] == "calls"


def test_a_put_leg_scans_the_put_side(monkeypatch):
    seen = {}
    _patch_fetch(monkeypatch,
                 lambda t, ot, lo, hi, **k: seen.update(ot=ot) or (None, [], "x"))
    trades.leg_iv_pp(_pos(option_type="P"), "MRNA", "schwab", {})
    assert seen["ot"] == "puts"


# ── the share leg of an unwind ──────────────────────────────────────────────

class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _equity_quote_from(quote: dict, monkeypatch) -> dict | None:
    """Run the real Schwab-quote parser over one canned response."""
    class _Client:
        def get_quote(self, ticker):
            return _Resp({ticker: {"quote": quote}})

    import stocks_shared.schwab_live as sl
    monkeypatch.setattr(sl, "get_client", lambda *a, **k: _Client())
    # The helper is @st.cache_data; __wrapped__ is the undecorated function, so
    # each case parses fresh instead of hitting a cache keyed on the args.
    fn = getattr(trades._equity_quote, "__wrapped__", trades._equity_quote)
    return fn("k", "s", "cb", "tok", "MRNA")


def test_mid_is_the_midpoint_not_schwabs_mark(monkeypatch):
    # Real numbers off the panel: a mark of 376.95 is NOT the middle of
    # 376.94/377.07. Reporting it as "Mid" made the net credit read ~5.5c/share
    # light against the very bid/ask printed above it.
    q = _equity_quote_from({"bidPrice": 376.94, "askPrice": 377.07,
                            "mark": 376.95, "lastPrice": 376.95}, monkeypatch)
    assert q["mid"] == pytest.approx(377.005)
    assert q["mark"] == pytest.approx(376.95)


def test_the_mark_is_reported_rather_than_discarded(monkeypatch):
    # It's still useful — Schwab's own fair value — just not a midpoint.
    q = _equity_quote_from({"bidPrice": 10.0, "askPrice": 10.10, "mark": 10.02},
                           monkeypatch)
    assert (q["mid"], q["mark"]) == (pytest.approx(10.05), pytest.approx(10.02))


def test_a_quote_with_no_mark_still_prices(monkeypatch):
    q = _equity_quote_from({"bidPrice": 10.0, "askPrice": 10.10}, monkeypatch)
    assert q["mid"] == pytest.approx(10.05) and q["mark"] is None


def test_the_net_mid_sits_exactly_between_the_two_bounds(monkeypatch):
    # End to end over the real parser, because that's where the bug lived: the
    # panel shows mid between "crossing both spreads" and "both come to you",
    # so if mid isn't their center those three numbers describe no coherent
    # market. Feeding a hand-built dict here would pass either way.
    stock = _equity_quote_from({"bidPrice": 376.94, "askPrice": 377.07,
                                "mark": 376.95, "lastPrice": 376.95},
                               monkeypatch)
    opt = {"bid": 2.10, "ask": 2.35, "mid": 2.225}
    net = ta.unwind_net_quote(stock, opt)
    assert net["mid"] == pytest.approx((net["worst"] + net["best"]) / 2, abs=0.01)


def test_the_share_leg_reports_a_down_day(monkeypatch):
    # The price helper rejects non-positive values, which is right for a bid
    # and wrong for a day change: a red day would have vanished from the panel
    # exactly when it matters most to whoever is about to sell the shares.
    q = _equity_quote_from({"bidPrice": 100.0, "askPrice": 100.1, "mark": 100.05,
                            "lastPrice": 100.02, "totalVolume": 5_200_000,
                            "netPercentChange": -3.75}, monkeypatch)
    assert q["pct_change"] == -3.75
    assert q["volume"] == 5_200_000


def test_the_share_leg_survives_a_quote_missing_the_day_fields(monkeypatch):
    # Day context is decoration; bid/ask are what price the unwind.
    q = _equity_quote_from({"bidPrice": 100.0, "askPrice": 100.1}, monkeypatch)
    assert q["bid"] == 100.0 and q["ask"] == 100.1
    assert q["volume"] is None and q["pct_change"] is None


def test_a_flat_day_is_reported_rather_than_dropped(monkeypatch):
    q = _equity_quote_from({"bidPrice": 100.0, "askPrice": 100.1,
                            "totalVolume": 0, "netPercentChange": 0.0},
                           monkeypatch)
    assert q["pct_change"] == 0.0 and q["volume"] == 0
