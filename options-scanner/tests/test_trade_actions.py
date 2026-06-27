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
