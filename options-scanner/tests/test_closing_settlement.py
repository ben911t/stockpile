"""Tests for closing a tracked trade: settling a working broker order, and the
tracker-only close of a paper trade.

Regression cover for the state that stranded a re-close: a buy-to-close day
order placed one session EXPIRES overnight, but the trade stayed "closing" in
the store, so the Close tab kept refusing a fresh close ("an order is already
working") for an order that had been dead since the previous close.
``_settle_closing_trade`` is what returns such a trade to "open"; the tabs call
it on load via ``_reconcile_closing_orders``.

The paper-close section at the bottom covers test-plan rows C1/C2, which were
unreachable in the UI until the Trades tab stopped gating the close controls on a
broker FILL that a simulated trade can never have.
"""

from datetime import datetime

import pytest

from options_scanner import trades_store
from options_scanner.tabs import trades as tt


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the JSON trade log at a tmp file so tests never touch the real
    one."""
    monkeypatch.setattr(trades_store, "_DIR", tmp_path)
    monkeypatch.setattr(trades_store, "_FILE", tmp_path / "trades.json")
    return trades_store


def _closing_trade(store, **over):
    """A tracked short call with a working buy-to-close — the CPNG shape."""
    return store.add({
        "ticker": "CPNG", "strike": 30.0, "expiration": "2026-08-21",
        "quantity": 4, "credit": 1.10, "option_type": "C",
        "status": "closing", "close_order_id": "ORD1",
        "close_limit_px": 0.55, "close_qty": 4, "paper": False, **over})


def _only(store):
    trades = store.load()
    assert len(trades) == 1
    return trades[0]


# ── no change ────────────────────────────────────────────────────────────────

def test_unavailable_status_leaves_trade_closing(store):
    """Schwab unreachable → don't guess; the trade stays as-is."""
    t = _closing_trade(store)
    assert tt._settle_closing_trade(t, None) == (False, None)
    assert _only(store)["status"] == "closing"


def test_still_working_leaves_trade_closing(store):
    """A cancelable (live, unfilled) order is genuinely still working."""
    t = _closing_trade(store)
    cbs = {"status": "WORKING", "cancelable": True, "filled": 0}
    assert tt._settle_closing_trade(t, cbs) == (False, None)
    assert _only(store)["status"] == "closing"


# ── expired / terminal without filling ───────────────────────────────────────

def test_expired_unfilled_reverts_to_open(store):
    """THE BUG: a day order that expired overnight must free the position so it
    can be closed again."""
    t = _closing_trade(store)
    cbs = {"status": "EXPIRED", "cancelable": False, "filled": 0}
    changed, note = tt._settle_closing_trade(t, cbs)
    assert changed is True
    assert "EXPIRED" in note and "open again" in note
    rec = _only(store)
    assert rec["status"] == "open"
    # Stale closing fields must be cleared, or the next close would inherit them.
    assert rec["close_order_id"] is None
    assert rec["close_limit_px"] is None
    assert rec["close_qty"] is None
    assert rec["quantity"] == 4          # nothing filled → full size intact


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELED", "REJECTED"])
def test_all_terminal_unfilled_statuses_free_the_position(store, status):
    t = _closing_trade(store)
    changed, note = tt._settle_closing_trade(
        t, {"status": status, "cancelable": False, "filled": 0})
    assert changed is True and status in note
    assert _only(store)["status"] == "open"


def test_expired_unfilled_broker_sourced_record_is_dropped(store):
    """An untracked Schwab leg has no app-side position to return to — drop the
    bookkeeping record rather than leave a phantom open trade."""
    t = _closing_trade(store, opened_from="schwab_position")
    changed, note = tt._settle_closing_trade(
        t, {"status": "EXPIRED", "cancelable": False, "filled": 0})
    assert changed is True
    assert "unchanged at your broker" in note
    assert store.load() == []


# ── filled ───────────────────────────────────────────────────────────────────

def test_filled_books_the_close_at_the_execution_price(store):
    t = _closing_trade(store)
    cbs = {"status": "FILLED", "cancelable": False, "filled": 4,
           "fill_price": 0.48, "filled_at": datetime(2026, 7, 22, 15, 30)}
    changed, note = tt._settle_closing_trade(t, cbs)
    assert changed is True and note is None
    rec = _only(store)
    assert rec["status"] == "closed"
    assert rec["close_cost"] == 0.48          # true fill, not the 0.55 limit
    assert rec["closed_at"].startswith("2026-07-22T15:30")


def test_filled_without_a_fill_price_falls_back_to_the_limit(store):
    t = _closing_trade(store)
    changed, _ = tt._settle_closing_trade(
        t, {"status": "FILLED", "cancelable": False, "filled": 4,
            "fill_price": None, "filled_at": None})
    assert changed is True
    assert _only(store)["close_cost"] == 0.55


# ── partial fill, then terminal ──────────────────────────────────────────────

def test_partial_fill_then_expiry_books_fills_and_reopens_remainder(store):
    """1 of 4 filled before expiry → book that one closed, 3 open again."""
    t = _closing_trade(store)
    cbs = {"status": "EXPIRED", "cancelable": False, "filled": 1,
           "fill_price": 0.50, "filled_at": datetime(2026, 7, 22, 16, 0)}
    changed, note = tt._settle_closing_trade(t, cbs)
    assert changed is True
    assert "after filling 1 of 4" in note

    trades = store.load()
    assert len(trades) == 2               # split: closed piece + open remainder
    closed = [x for x in trades if x["status"] == "closed"]
    reopened = [x for x in trades if x["status"] == "open"]
    assert len(closed) == 1 and len(reopened) == 1
    assert closed[0]["quantity"] == 1 and closed[0]["close_cost"] == 0.50
    assert reopened[0]["quantity"] == 3
    assert reopened[0]["close_order_id"] is None


# ── paper close: tracker only, no broker order (test plan C1 / C2) ────────────

def _paper_trade(store, **over):
    """An open PAPER short call — 4 contracts sold for $1.10."""
    return store.add({
        "ticker": "CPNG", "strike": 30.0, "expiration": "2026-08-21",
        "quantity": 4, "credit": 1.10, "option_type": "C",
        "status": "open", "paper": True, **over})


def test_paper_full_close_books_it_in_the_tracker(store):
    # C1: closed at the limit, no broker order. Realized P/L is derived from
    # credit − close_cost at display time, so the cost is what must land.
    t = _paper_trade(store)
    res = tt._submit_close({}, t, 0.55, False)
    assert res["ok"] and "tracker" in res["msg"]
    assert "No live order was sent." in res["msg"]

    rec = _only(store)
    assert rec["status"] == "closed"
    assert rec["close_cost"] == 0.55
    assert rec["closed_at"] and rec["close_order_id"] is None
    # credit − cost, per contract-hundred: (1.10 − 0.55) × 100 × 4
    assert round((rec["credit"] - rec["close_cost"]) * 100
                 * rec["quantity"], 2) == 220.0


def test_paper_partial_close_splits_the_record(store):
    # C2: 2 of 4 closed → a closed record for 2, the other 2 still open.
    t = _paper_trade(store)
    res = tt._submit_close({}, t, 0.55, False, close_qty=2)
    assert res["ok"] and "2 of 4 contract(s)" in res["msg"]

    trades = store.load()
    assert len(trades) == 2
    closed = [x for x in trades if x["status"] == "closed"]
    still_open = [x for x in trades if x["status"] == "open"]
    assert len(closed) == 1 and len(still_open) == 1
    assert closed[0]["quantity"] == 2 and closed[0]["close_cost"] == 0.55
    assert closed[0]["paper"] is True          # provenance survives the split
    assert still_open[0]["quantity"] == 2


def test_paper_close_qty_above_the_position_closes_all_of_it(store):
    t = _paper_trade(store)
    assert tt._submit_close({}, t, 0.55, False, close_qty=99)["ok"]
    assert _only(store)["status"] == "closed"


def test_a_live_trade_cannot_be_closed_in_the_tracker_only(store):
    # C8's belt-and-suspenders: marking a real broker position closed without an
    # order would desync the tracker from the account.
    t = _paper_trade(store, paper=False)
    res = tt._submit_close({}, t, 0.55, False)
    assert res["ok"] is False
    assert "paper=false" in res["msg"]
    assert _only(store)["status"] == "open"    # untouched
