"""The Positions table's money cells.

Covers the two figures a wrong number would actually cost you — the leg's
market value (signed like a statement) and the Open pair (what the underlying
and the option cost when the position was opened) — plus the trade-log lookups
that supply what the broker can't tell us.

These were the Roll tab's table before the Close and Roll tabs merged into
Positions; the surviving table is `tabs/trades`'s, so the helpers moved with it.
"""

import pytest

from options_scanner.format import days_since, open_prices_cell
from options_scanner.tabs import trades
from options_scanner.tabs.trades import _signed_value, _tracked_open_leg


# ── Signed market value ─────────────────────────────────────────────────────

def test_sold_is_negative_and_bought_is_positive():
    assert _signed_value(-220.0, 4, "short") == pytest.approx(-220.0)
    assert _signed_value(220.0, 4, "long") == pytest.approx(220.0)


def test_sign_comes_from_direction_not_from_the_brokers_sign():
    # A broker that reports a short's market value unsigned must not turn a
    # liability into an apparent asset — only the magnitude is taken from it.
    assert _signed_value(220.0, 4, "short") == pytest.approx(-220.0)
    assert _signed_value(-220.0, 4, "long") == pytest.approx(220.0)


def test_direction_is_read_case_insensitively():
    assert _signed_value(220.0, 1, " SHORT ") < 0


def test_unknown_direction_reads_as_long():
    # Nothing on this tab lacks a direction, but an asset is the safer default:
    # it can't understate what you owe.
    assert _signed_value(220.0, 1, None) > 0


def test_signed_value_is_none_without_a_position():
    assert _signed_value(-220.0, 0, "short") is None
    assert _signed_value("junk", 4, "short") is None
    assert _signed_value(float("nan"), 4, "short") is None


# ── The Open cell: underlying at open · option at open ──────────────────────
# The stock figure answers "did I write this into strength or weakness?", which
# the broker can't tell us — it only comes from a trade placed through the app.

def test_open_cell_pairs_the_underlying_with_the_option():
    assert open_prices_cell(27.40, 1.10) == "$27.40 · $1.10"


def test_underlying_at_open_is_blank_for_a_leg_opened_outside_the_app():
    # No trade record → no stock price, but the option's own open price is the
    # broker's and always renders. The em dash holds the slot so the number on
    # the right is always the option.
    assert open_prices_cell(None, 1.10) == "— · $1.10"


def test_open_cell_collapses_when_neither_price_is_known():
    assert open_prices_cell(None, None) == "—"
    assert open_prices_cell(0, 0) == "—"


def test_a_zero_price_is_treated_as_missing():
    # You don't open an option at $0.00 — a zero is an absent record, and
    # "$0.00" would read as a real fill.
    assert open_prices_cell(27.40, 0) == "$27.40 · —"


def test_thousands_are_grouped():
    assert open_prices_cell(1234.5, 12.75) == "$1,234.50 · $12.75"


# ── The trade-log lookups behind those cells ────────────────────────────────

def _record(**over):
    r = {"status": "open", "ticker": "CPNG", "option_type": "C", "strike": 30.0,
         "expiration": "2026-08-21", "opened_at": "2026-06-20T10:30:00",
         "fill_spot": 27.40}
    r.update(over)
    return r


def _lookup():
    return _tracked_open_leg("CPNG", "C", 30.0, "2026-08-21")


def test_the_record_supplies_both_the_open_spot_and_the_days(monkeypatch):
    from options_scanner import trades_store
    monkeypatch.setattr(trades_store, "load", lambda: [_record()])
    rec = _lookup()
    assert rec["fill_spot"] == pytest.approx(27.40)
    assert days_since(rec["opened_at"]) > 0


def test_an_older_record_without_a_fill_spot_still_gives_days(monkeypatch):
    # fill_spot postdates the earliest trade records — those legs keep their
    # days-open note and just lose the stock price.
    from options_scanner import trades_store
    rec = _record()
    del rec["fill_spot"]
    monkeypatch.setattr(trades_store, "load", lambda: [rec])
    found = _lookup()
    assert open_prices_cell(found.get("fill_spot"), 1.10) == "— · $1.10"
    assert days_since(found["opened_at"]) > 0


def test_a_position_with_no_record_reads_as_opened_elsewhere(monkeypatch):
    from options_scanner import trades_store
    monkeypatch.setattr(trades_store, "load", lambda: [])
    assert _lookup() is None


def test_a_closed_record_does_not_supply_an_open_price(monkeypatch):
    # Only a still-open (or closing) leg describes the position on screen; a
    # closed record for the same strike is a different, earlier trade.
    from options_scanner import trades_store
    monkeypatch.setattr(trades_store, "load", lambda: [_record(status="closed")])
    assert _lookup() is None


def test_a_leg_with_a_working_close_is_still_the_open_record(monkeypatch):
    # "closing" means a buy-to-close is working but unfilled — the position is
    # still held, so it still describes the row.
    from options_scanner import trades_store
    monkeypatch.setattr(trades_store, "load",
                        lambda: [_record(status="closing")])
    assert _lookup() is not None


def test_the_module_no_longer_carries_the_old_roll_table():
    # The merge deleted it; this fails loudly if it comes back by accident.
    assert not hasattr(trades, "_positions_table")
