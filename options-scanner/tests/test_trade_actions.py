"""Tests for the assisted put-selling logic (pure functions)."""

import pytest

from options_scanner import trade_actions as ta


# ── tick rounding ────────────────────────────────────────────────────────────

def test_tick_for_split_at_three_dollars():
    assert ta.tick_for(2.99) == 0.01
    assert ta.tick_for(3.00) == 0.05
    assert ta.tick_for(7.5) == 0.05


def test_round_to_tick():
    assert ta.round_to_tick(2.453) == 2.45   # penny tick below $3
    assert ta.round_to_tick(5.27) == 5.25    # nickel tick at/above $3
    assert ta.round_to_tick(5.28) == 5.30


def test_ceil_to_tick():
    assert ta.ceil_to_tick(3.92) == 3.95     # nickel tick rounds UP
    assert ta.ceil_to_tick(2.451) == 2.46    # penny tick rounds UP
    assert ta.ceil_to_tick(3.90) == 3.90     # already on tick, unchanged
    assert ta.ceil_to_tick(3.95) == 3.95     # float-noise guard: no jump


def test_avg_fill_price_weights_by_quantity():
    order = {"orderActivityCollection": [
        {"activityType": "EXECUTION", "executionLegs": [
            {"price": 3.90, "quantity": 1},
            {"price": 4.00, "quantity": 3},
        ]},
    ]}
    # (3.90*1 + 4.00*3) / 4 = 3.975
    assert ta._avg_fill_price(order) == 3.975


def test_avg_fill_price_none_without_fills():
    assert ta._avg_fill_price({}) is None
    assert ta._avg_fill_price(
        {"orderActivityCollection": [{"activityType": "ORDER_ACTION"}]}
    ) is None


# ── fill-quality assessment ──────────────────────────────────────────────────

def test_assess_fill_liquid_uses_mid():
    a = ta.assess_fill(bid=2.40, ask=2.50, mid=2.45, volume=300,
                       open_interest=1800)
    assert a.liquid is True
    assert a.reasons == []
    assert a.suggested_limit == 2.45


def test_assess_fill_wide_and_thin_flags_both():
    a = ta.assess_fill(bid=1.00, ask=1.80, mid=1.40, volume=0, open_interest=12)
    assert a.liquid is False
    assert any("spread" in r for r in a.reasons)
    assert any("open interest" in r for r in a.reasons)
    # mid-anchored suggestion is still computed (illiquid path layers a model)
    assert a.suggested_limit == 1.40


def test_assess_fill_cheap_contract_rescued_by_absolute_spread():
    # 8c spread on a 14c mid is 57% but tiny in dollars → still liquid.
    a = ta.assess_fill(bid=0.10, ask=0.18, mid=0.14, volume=5, open_interest=900)
    assert a.liquid is True
    assert any("low volume" in n for n in a.notes)


def test_assess_fill_one_sided_market():
    a = ta.assess_fill(bid=0.0, ask=2.0, mid=None, volume=10, open_interest=500)
    assert a.liquid is False
    assert a.suggested_limit is None


# ── IV-aligned model limit ───────────────────────────────────────────────────

def test_model_limit_prices_a_put():
    # Near-ATM put with high IV → a clearly positive premium.
    m = ta.model_limit(spot=95.0, strike=90.0, dte=45, iv=0.55)
    assert m is not None and m > 0


def test_model_limit_prices_the_requested_side():
    """A covered call must price the CALL, not the (far pricier ITM) put at the
    same OTM-call strike — the bug where a $1.50 call got an $8+ put limit."""
    # Strike above spot: OTM call (cheap) vs ITM put (expensive).
    call = ta.model_limit(spot=95.0, strike=105.0, dte=45, iv=0.55,
                          option_type="call")
    put = ta.model_limit(spot=95.0, strike=105.0, dte=45, iv=0.55,
                         option_type="put")
    assert call is not None and put is not None
    assert call < put                                   # call ≪ ITM put
    # "C"/"P" aliases resolve the same way as call/put.
    assert ta.model_limit(spot=95.0, strike=105.0, dte=45, iv=0.55,
                          option_type="C") == call


def test_model_limit_missing_inputs():
    assert ta.model_limit(spot=None, strike=90, dte=45, iv=0.5) is None
    assert ta.model_limit(spot=95, strike=90, dte=0, iv=0.5) is None


# ── capacity / affordability ─────────────────────────────────────────────────

def test_puts_affordable():
    assert ta.puts_affordable(50_000, 90) == 5      # 90*100 = 9000 → 5
    assert ta.puts_affordable(8_000, 90) == 0
    assert ta.puts_affordable(None, 90) is None
    assert ta.puts_affordable(50_000, 0) is None


# ── order builder + validation ───────────────────────────────────────────────

def test_build_put_sell_order_ok():
    o = ta.build_put_sell_order(ticker="AAPL", strike=180, expiration="2026-01-16",
                                limit=2.35, quantity=2)
    assert o.credit == 470.0           # 2.35 * 100 * 2
    assert o.collateral == 36_000.0    # 180 * 100 * 2
    assert "SELL 2 AAPL" in o.describe()
    assert "$180 PUT" in o.describe()


def test_build_put_sell_order_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ta.build_put_sell_order(ticker="X", strike=10, expiration="2026-01-16",
                                limit=1.0, quantity=0)
    with pytest.raises(ValueError):
        ta.build_put_sell_order(ticker="X", strike=10, expiration="2026-01-16",
                                limit=0.0, quantity=1)


def test_build_put_sell_order_capacity_guard():
    # 2 contracts × $180 × 100 = $36,000 collateral, only $20k available.
    with pytest.raises(ValueError):
        ta.build_put_sell_order(ticker="AAPL", strike=180,
                                expiration="2026-01-16", limit=2.0, quantity=2,
                                capacity=20_000)


def test_build_option_sell_order_call():
    o = ta.build_option_sell_order(ticker="AAPL", strike=200,
                                   expiration="2026-01-16", limit=3.0,
                                   quantity=2, option_type="C")
    assert o.option_type == "C"
    assert o.shares_to_cover == 200       # 100 × 2
    assert o.credit == 600.0              # 3.0 × 100 × 2
    assert "$200 CALL" in o.describe()


def test_build_option_sell_order_coverage_guard():
    # 3 calls need 300 shares covered, but only 2 are coverable.
    with pytest.raises(ValueError):
        ta.build_option_sell_order(ticker="AAPL", strike=200,
                                   expiration="2026-01-16", limit=3.0,
                                   quantity=3, option_type="C", max_contracts=2)


def test_calls_coverable_nets_existing_short_calls():
    assert ta.calls_coverable(500) == 5            # 500 / 100
    assert ta.calls_coverable(500, 2) == 3         # minus 2 already written
    assert ta.calls_coverable(150, 1) == 0         # 1 coverable − 1 = 0
    assert ta.calls_coverable(50) == 0             # < 100 shares
    assert ta.calls_coverable(None) is None


def test_held_shares_map_aggregates_equity_and_short_calls():
    """One positions fetch → {TICKER: shares, short_calls}: multi-lot equity
    sums, short CALLs net, short PUTs are ignored (they don't cover a call)."""
    payload = {"securitiesAccount": {"positions": [
        {"instrument": {"assetType": "EQUITY", "symbol": "CPNG"},
         "longQuantity": 300},
        {"instrument": {"assetType": "EQUITY", "symbol": "CPNG"},
         "longQuantity": 200},                       # two lots → 500 shares
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "CPNG"}, "shortQuantity": 4},
        {"instrument": {"assetType": "EQUITY", "symbol": "AMD"},
         "longQuantity": 50},
        {"instrument": {"assetType": "OPTION", "putCall": "PUT",
                        "underlyingSymbol": "MSFT"}, "shortQuantity": 2},
    ]}}

    class _Acct:
        def get_account_numbers(self):
            return _Resp(200, [{"accountNumber": "1", "hashValue": "H"}])

        def get_account(self, account_hash, fields=None):
            return _Resp(200, payload)

    m = ta.held_shares_and_short_calls_map(_Acct())
    assert m["CPNG"] == {"shares": 500.0, "short_calls": 4}
    assert m["AMD"] == {"shares": 50.0, "short_calls": 0}
    assert "MSFT" not in m                           # short put ≠ call cover
    assert ta.calls_coverable(m["CPNG"]["shares"],
                              m["CPNG"]["short_calls"]) == 1   # 5 − 4
    assert ta.calls_coverable(m["AMD"]["shares"],
                              m["AMD"]["short_calls"]) == 0     # < 100 net
    # single-ticker view delegates to the same parse (case-insensitive)
    assert ta.held_shares_and_short_calls(_Acct(), "cpng") == (500.0, 4)
    assert ta.held_shares_and_short_calls(_Acct(), "TSLA") == (0.0, 0)


# ── market hours / LIVE placement (fake client, no network) ──────────────────

class _Resp:
    def __init__(self, code, payload=None, loc=None):
        self.status_code = code
        self._p = payload if payload is not None else {}
        self.headers = {"Location": loc} if loc else {}

    def json(self):
        return self._p


class _FakeClient:
    """Records place_order calls; returns canned market-hours / accounts."""

    def __init__(self, *, place=None, market=None, accounts=None,
                 order=None, cancel=None):
        self._place = place
        self._market = market
        self._accounts = accounts
        self._order = order
        self._cancel = cancel
        self.placed = None
        self.canceled = None

    def place_order(self, account_hash, order_spec):
        self.placed = (account_hash, order_spec)
        return self._place

    def get_market_hours(self, markets, date=None):
        return self._market

    def get_account_numbers(self):
        return self._accounts

    def get_order(self, order_id, account_hash):
        return self._order

    def cancel_order(self, order_id, account_hash):
        self.canceled = (order_id, account_hash)
        return self._cancel


def test_market_is_open_checks_session_window_not_just_isopen():
    """`isOpen` only means 'today is a trading day' (it stays True after the
    close), so market_is_open must also test now against the session window —
    timezone-correctly, via the tz-aware ISO timestamps Schwab returns."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    def sess(start, end):
        return {"option": {"EQO": {"isOpen": True, "sessionHours": {
            "regularMarket": [{"start": start.isoformat(),
                               "end": end.isoformat()}]}}}}

    inside = _Resp(200, sess(now - timedelta(hours=1),
                             now + timedelta(hours=1)))
    assert ta.market_is_open(_FakeClient(market=inside)) is True

    # trading day, but the session already ended (e.g. 10pm) → NOT open
    after = _Resp(200, sess(now - timedelta(hours=8),
                            now - timedelta(hours=2)))
    assert ta.market_is_open(_FakeClient(market=after)) is False

    # not a trading day (weekend / holiday) → closed
    holiday = _Resp(200, {"option": {"EQO": {"isOpen": False}}})
    assert ta.market_is_open(_FakeClient(market=holiday)) is False

    class _Boom:
        def get_market_hours(self, markets, date=None):
            raise RuntimeError("down")
    assert ta.market_is_open(_Boom()) is None          # fail safe → None


def test_resolve_account_hash_single_and_by_last4():
    accts = _Resp(200, [{"accountNumber": "12345678556", "hashValue": "H1"}])
    c = _FakeClient(accounts=accts)
    assert ta.resolve_account_hash(c) == ("H1", "...8556")        # lone account
    assert ta.resolve_account_hash(c, "8556") == ("H1", "...8556")  # matched

    two = _Resp(200, [{"accountNumber": "111118556", "hashValue": "A"},
                      {"accountNumber": "222229999", "hashValue": "B"}])
    c2 = _FakeClient(accounts=two)
    assert ta.resolve_account_hash(c2) is None                   # ambiguous
    assert ta.resolve_account_hash(c2, "9999") == ("B", "...9999")
    assert ta.resolve_account_hash(c2, "0000") is None           # no match


def test_place_put_sell_order_submits_sell_to_open_put():
    order = ta.build_put_sell_order(ticker="AMD", strike=100.0,
                                    expiration="2026-07-17", limit=1.25,
                                    quantity=2)
    c = _FakeClient(place=_Resp(201, loc=".../orders/55"))
    res = ta.place_put_sell_order(c, order, "HASH")
    assert res["ok"] is True and res["error"] is None
    sent_hash, spec = c.placed
    assert sent_hash == "HASH"                                   # right account
    leg = spec.build()["orderLegCollection"][0]
    assert leg["instruction"] == "SELL_TO_OPEN"
    assert leg["instrument"]["symbol"] == "AMD   260717P00100000"
    assert leg["quantity"] == 2


def test_place_put_sell_order_surfaces_schwab_error():
    order = ta.build_put_sell_order(ticker="AMD", strike=100.0,
                                    expiration="2026-07-17", limit=1.25,
                                    quantity=1)
    c = _FakeClient(place=_Resp(400, {"errors": [{"detail": "no buying power"}]}))
    res = ta.place_put_sell_order(c, order, "HASH")
    assert res["ok"] is False and res["error"] == "no buying power"


def test_place_put_sell_order_rejects_invalid_without_calling_broker():
    bad = ta.PutSellOrder(ticker="AMD", strike=100.0, expiration="2026-07-17",
                          limit=0.0, quantity=1)   # zero limit
    c = _FakeClient(place=_Resp(201))
    res = ta.place_put_sell_order(c, bad, "HASH")
    assert res["ok"] is False
    assert c.placed is None                                      # never sent


def test_place_put_close_order_submits_buy_to_close():
    c = _FakeClient(place=_Resp(201, loc=".../orders/77"))
    res = ta.place_put_close_order(c, ticker="AMD", strike=100.0,
                                   expiration="2026-07-17", limit=0.40,
                                   quantity=2, account_hash="HASH")
    assert res["ok"] is True
    sent_hash, spec = c.placed
    assert sent_hash == "HASH"
    leg = spec.build()["orderLegCollection"][0]
    assert leg["instruction"] == "BUY_TO_CLOSE"
    assert leg["instrument"]["symbol"] == "AMD   260717P00100000"
    assert leg["quantity"] == 2


def test_place_put_close_order_rejects_invalid():
    c = _FakeClient(place=_Resp(201))
    res = ta.place_put_close_order(c, ticker="AMD", strike=100.0,
                                   expiration="2026-07-17", limit=0.0,
                                   quantity=1, account_hash="HASH")
    assert res["ok"] is False and c.placed is None


def test_get_order_status_parses_and_flags_cancelable():
    accts = _Resp(200, [{"accountNumber": "111118556", "hashValue": "H"}])
    filled = _FakeClient(accounts=accts,
                         order=_Resp(200, {"status": "FILLED",
                                           "filledQuantity": 2, "quantity": 2}))
    s = ta.get_order_status(filled, "55", "8556")
    assert s["status"] == "FILLED" and s["cancelable"] is False

    working = _FakeClient(accounts=accts,
                          order=_Resp(200, {"status": "WORKING",
                                            "filledQuantity": 0, "quantity": 2}))
    s2 = ta.get_order_status(working, "55", "8556")
    assert s2["status"] == "WORKING" and s2["cancelable"] is True

    assert ta.get_order_status(filled, None, "8556") is None   # no order id


def test_cancel_order_ok_and_surfaces_error():
    accts = _Resp(200, [{"accountNumber": "111118556", "hashValue": "H"}])
    ok = _FakeClient(accounts=accts, cancel=_Resp(200))
    res = ta.cancel_order(ok, "55", "8556")
    assert res["ok"] is True
    assert ok.canceled == ("55", "H")                          # right account

    bad = _FakeClient(accounts=accts,
                      cancel=_Resp(400, {"errors": [{"detail": "too late"}]}))
    assert ta.cancel_order(bad, "55", "8556") == {"ok": False, "error": "too late"}


# ── OSI symbol parsing (inverse of _osi) ─────────────────────────────────────

def test_parse_option_symbol_round_trips_with_osi():
    for tk, strike, exp, right in [("AMD", 200, "2026-01-16", "C"),
                                   ("AAPL", 152.5, "2027-12-17", "P"),
                                   ("F", 7.5, "2025-08-15", "C")]:
        sym = ta._osi(tk, strike, exp, right)
        root, exp_iso, cp, k = ta._parse_option_symbol(sym)
        assert (root, exp_iso, cp) == (tk, exp, right)
        assert abs(k - strike) < 1e-9


def test_parse_option_symbol_rejects_non_options():
    assert ta._parse_option_symbol("") is None
    assert ta._parse_option_symbol("AAPL") is None          # too short
    assert ta._parse_option_symbol("AMD   260116X00200000") is None  # bad right


# ── live position readers ────────────────────────────────────────────────────

def _positions_client(positions):
    payload = {"securitiesAccount": {"positions": positions}}

    class _Acct:
        def get_account_numbers(self):
            return _Resp(200, [{"accountNumber": "1", "hashValue": "H"}])

        def get_account(self, account_hash, fields=None):
            return _Resp(200, payload)

    return _Acct()


def test_open_option_positions_parses_legs_and_coverage():
    c = _positions_client([
        {"instrument": {"assetType": "EQUITY", "symbol": "AMD"},
         "longQuantity": 200},
        # Covered call: 200 shares cover 2 calls.
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "AMD", "symbol": "AMD   260116C00200000"},
         "shortQuantity": 2, "averagePrice": 3.10, "marketValue": -900},
        # Naked short call: only 50 shares of MSFT (none, actually) → not covered.
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "MSFT", "symbol": "MSFT  260116C00500000"},
         "shortQuantity": 1, "averagePrice": 4.00, "marketValue": -420},
        # Short put.
        {"instrument": {"assetType": "OPTION", "putCall": "PUT",
                        "underlyingSymbol": "AMD", "symbol": "AMD   260116P00150000"},
         "shortQuantity": 3, "averagePrice": 2.00, "marketValue": -600},
        # Long call — kept by the general reader, excluded from rollable.
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "NVDA", "symbol": "NVDA  260116C00900000"},
         "longQuantity": 1, "averagePrice": 10.0, "marketValue": 1100},
    ])
    legs = ta.open_option_positions(c)
    by = {(l["underlying"], l["option_type"], l["strike"]): l for l in legs}

    amd_call = by[("AMD", "C", 200.0)]
    assert amd_call["direction"] == "short" and amd_call["quantity"] == 2
    assert amd_call["covered"] is True and amd_call["shares_held"] == 200.0
    assert amd_call["avg_price"] == 3.10 and amd_call["expiration"] == "2026-01-16"

    assert by[("MSFT", "C", 500.0)]["covered"] is False      # naked
    assert by[("AMD", "P", 150.0)]["direction"] == "short"
    assert by[("NVDA", "C", 900.0)]["direction"] == "long"


def test_rollable_positions_keeps_covered_calls_and_short_puts_only():
    c = _positions_client([
        {"instrument": {"assetType": "EQUITY", "symbol": "AMD"},
         "longQuantity": 200},
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "AMD", "symbol": "AMD   260116C00200000"},
         "shortQuantity": 2},                                 # covered call ✓
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "MSFT", "symbol": "MSFT  260116C00500000"},
         "shortQuantity": 1},                                 # naked call ✗
        {"instrument": {"assetType": "OPTION", "putCall": "PUT",
                        "underlyingSymbol": "AMD", "symbol": "AMD   260116P00150000"},
         "shortQuantity": 3},                                 # short put ✓
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "NVDA", "symbol": "NVDA  260116C00900000"},
         "longQuantity": 1},                                  # long call ✗
    ])
    got = {(p["underlying"], p["option_type"]) for p in ta.rollable_positions(c)}
    assert got == {("AMD", "C"), ("AMD", "P")}


def test_rollable_positions_lists_every_call_leg_of_a_share_backed_ticker():
    """One row per strike/expiration: a ticker's multiple call legs must all be
    listed as long as the underlying is share-backed — even a leg with more
    contracts than shares/100 (they share the pool). Only a truly naked call
    (no shares) is dropped."""
    c = _positions_client([
        {"instrument": {"assetType": "EQUITY", "symbol": "CPNG"},
         "longQuantity": 300},                                # partial coverage
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "CPNG",
                        "symbol": "CPNG  270115C00035000"},
         "shortQuantity": 4},                     # 4 > 300/100, still listed ✓
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "CPNG",
                        "symbol": "CPNG  270115C00025000"},
         "shortQuantity": 1},                                 # second leg ✓
        {"instrument": {"assetType": "OPTION", "putCall": "CALL",
                        "underlyingSymbol": "ZZZ",
                        "symbol": "ZZZ   270115C00010000"},
         "shortQuantity": 1},                        # no ZZZ shares → naked ✗
    ])
    got = {(p["underlying"], p["strike"]) for p in ta.rollable_positions(c)}
    assert ("CPNG", 35.0) in got and ("CPNG", 25.0) in got   # both legs listed
    assert all(u != "ZZZ" for u, _ in got)                   # naked call dropped


def test_open_option_positions_empty_on_failure():
    class _Boom:
        def get_account_numbers(self):
            raise RuntimeError("down")
    assert ta.open_option_positions(_Boom()) == []
    assert ta.rollable_positions(_Boom()) == []


# ── roll order model + placement ─────────────────────────────────────────────

def test_build_roll_order_and_sign():
    credit = ta.build_roll_order(
        ticker="AMD", option_type="C", close_strike=150,
        close_expiration="2026-01-16", open_strike=160,
        open_expiration="2026-06-18", quantity=2, net_limit=1.25)
    assert credit.is_credit is True
    assert credit.net_amount == 250.0        # 1.25 × 100 × 2
    assert "CALL" in credit.describe() and "net credit" in credit.describe()

    debit = ta.build_roll_order(
        ticker="AMD", option_type="P", close_strike=150,
        close_expiration="2026-01-16", open_strike=140,
        open_expiration="2026-06-18", quantity=1, net_limit=-0.40)
    assert debit.is_credit is False
    assert debit.net_amount == -40.0
    assert "net debit" in debit.describe()


def test_build_roll_order_rejects_bad_inputs():
    base = dict(ticker="AMD", option_type="C", close_strike=150,
                close_expiration="2026-01-16", open_strike=160,
                open_expiration="2026-06-18", quantity=1, net_limit=1.0)
    with pytest.raises(ValueError):                          # bad right
        ta.build_roll_order(**{**base, "option_type": "X"})
    with pytest.raises(ValueError):                          # qty < 1
        ta.build_roll_order(**{**base, "quantity": 0})
    with pytest.raises(ValueError):                          # non-positive strike
        ta.build_roll_order(**{**base, "open_strike": 0})
    with pytest.raises(ValueError):                          # identical legs
        ta.build_roll_order(**{**base, "open_strike": 150,
                               "open_expiration": "2026-01-16"})


def test_place_roll_order_submits_two_leg_net_credit():
    roll = ta.build_roll_order(
        ticker="AMD", option_type="C", close_strike=150,
        close_expiration="2026-01-16", open_strike=160,
        open_expiration="2026-06-18", quantity=2, net_limit=1.25)
    c = _FakeClient(place=_Resp(201, loc=".../orders/99"))
    res = ta.place_roll_order(c, roll, "HASH")
    assert res["ok"] is True and res["order_id"] == "99"
    sent_hash, spec = c.placed
    assert sent_hash == "HASH"
    built = spec.build()
    assert built["orderType"] == "NET_CREDIT"
    assert built["price"] == "1.25"
    legs = built["orderLegCollection"]
    assert len(legs) == 2
    close_leg = next(l for l in legs if l["instruction"] == "BUY_TO_CLOSE")
    open_leg = next(l for l in legs if l["instruction"] == "SELL_TO_OPEN")
    assert close_leg["instrument"]["symbol"] == "AMD   260116C00150000"
    assert open_leg["instrument"]["symbol"] == "AMD   260618C00160000"
    assert close_leg["quantity"] == 2 and open_leg["quantity"] == 2


def test_place_roll_order_net_debit_uses_debit_type():
    roll = ta.build_roll_order(
        ticker="AMD", option_type="P", close_strike=150,
        close_expiration="2026-01-16", open_strike=140,
        open_expiration="2026-06-18", quantity=1, net_limit=-0.40)
    c = _FakeClient(place=_Resp(201, loc=".../orders/12"))
    res = ta.place_roll_order(c, roll, "HASH")
    assert res["ok"] is True
    built = c.placed[1].build()
    assert built["orderType"] == "NET_DEBIT"
    assert built["price"] == "0.40"          # abs of the net


def test_place_roll_order_rejects_invalid_without_calling_broker():
    bad = ta.RollOrder(ticker="AMD", option_type="C", close_strike=150,
                       close_expiration="2026-01-16", open_strike=0,
                       open_expiration="2026-06-18", quantity=1, net_limit=1.0)
    c = _FakeClient(place=_Resp(201))
    res = ta.place_roll_order(c, bad, "HASH")
    assert res["ok"] is False and c.placed is None
