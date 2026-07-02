"""Buy-mode ranking for the cross-ticker leaderboard.

Sell mode ranks the richest IV+pp first; buy mode must flip so the
cheapest (most-negative IV+pp) floats to the top — both for each
ticker's guaranteed #1 pick and for the final table order.
"""
import itertools

import pandas as pd

from options_scanner.display.leaderboard import (build_leaderboard,
                                                 contract_from_row)


def _row(opt_type, strike, iv_excess, oi=500, volume=100, dte=45):
    return {
        "type": opt_type,
        "strike": strike,
        "expiration": "2026-08-21",
        "dte": dte,
        "bid": 1.0,
        "ask": 1.2,
        "mid": 1.1,
        "iv": 0.5,
        "iv_excess": iv_excess,
        "delta": -0.40,
        "ann_yield_pct": 25.0,
        "open_interest": oi,
        "volume": volume,
        "earnings_count": 0,
        "last": 1.1,
        "spot": 150.0,
    }


def _results():
    """Two tickers, each with a rich and a cheap put."""
    return [
        {"error": None, "position": {"ticker": "AAA"},
         "df": pd.DataFrame([_row("put", 140, 0.05),    # rich
                             _row("put", 130, -0.06)])},  # cheapest overall
        {"error": None, "position": {"ticker": "BBB"},
         "df": pd.DataFrame([_row("put", 90, 0.02),
                             _row("put", 80, -0.03)])},
    ]


def test_sell_ranks_richest_put_first():
    board = build_leaderboard(_results(), "put", min_oi=25, top_n=5,
                              min_vol=10, buy=False)
    assert round(board.iloc[0]["iv_excess"], 4) == 0.05


def test_buy_ranks_cheapest_put_first():
    board = build_leaderboard(_results(), "put", min_oi=25, top_n=5,
                              min_vol=10, buy=True)
    assert round(board.iloc[0]["iv_excess"], 4) == -0.06


def test_buy_flips_each_ticker_top_pick():
    board = build_leaderboard(_results(), "put", min_oi=25, top_n=5,
                              min_vol=10, buy=True)
    tops = board[board["_is_ticker_top"]].set_index("ticker")["iv_excess"]
    # each ticker's guaranteed #1 is its cheapest contract
    assert round(tops["AAA"], 4) == -0.06
    assert round(tops["BBB"], 4) == -0.03


def _ticker(ticker, iv_excesses):
    """One ticker whose chain has a qualifying put per iv_excess given."""
    rows = [_row("put", 100 + i, iv, oi=500 - i)
            for i, iv in enumerate(iv_excesses)]
    return {"error": None, "position": {"ticker": ticker},
            "df": pd.DataFrame(rows)}


def test_each_ticker_capped_at_three_and_grouped_by_best():
    """Every ticker contributes at most 3 rows (no earnings-name flooding),
    rows are grouped per ticker, and tickers are ordered by their single
    richest IV+pp."""
    results = [
        _ticker("RICH", [0.20, 0.18, 0.16, 0.14, 0.12]),  # 5 qualify → capped
        _ticker("MID",  [0.10, 0.08]),                    # only 2
        _ticker("CALM", [0.05, 0.04, 0.03]),
    ]
    board = build_leaderboard(results, "put", min_oi=25, top_n=5,
                              min_vol=10, buy=False)

    counts = board["ticker"].value_counts()
    assert counts["RICH"] == 3          # capped at 3, not 5
    assert counts["MID"] == 2           # takes what it has
    assert counts["CALM"] == 3

    # Each ticker's rows are contiguous (one run per ticker), and the runs are
    # ordered by the ticker's best contract: RICH (0.20) > MID (0.10) > CALM.
    runs = [tk for tk, _ in itertools.groupby(board["ticker"])]
    assert runs == ["RICH", "MID", "CALM"]

    # Within a ticker, rows run best-first.
    rich = board[board["ticker"] == "RICH"]["iv_excess"].round(2).tolist()
    assert rich == [0.20, 0.18, 0.16]

    # Exactly one shaded #1 per ticker, each its richest contract.
    assert board["_is_ticker_top"].sum() == 3
    tops = board[board["_is_ticker_top"]].set_index("ticker")["iv_excess"]
    assert round(tops["RICH"], 2) == 0.20
    assert round(tops["MID"], 2) == 0.10
    assert round(tops["CALM"], 2) == 0.05


def test_contract_from_row_uses_explicit_ticker_and_spot_fallback():
    """Shared dialog contract builder: the per-ticker scan tables pass `ticker`
    explicitly (no ticker column) and a spot fallback (their subset may omit
    a spot column)."""
    # No `spot`/`ticker` columns — mimics a scan-results subset.
    row = pd.Series({"strike": 35.0, "expiration": "2026-08-21", "dte": 52,
                     "bid": 1.4, "ask": 1.6, "mid": 1.5, "last": 1.45,
                     "iv": 0.42, "delta": 0.28, "ann_yield_pct": 18.0,
                     "volume": 12, "open_interest": 800})
    c = contract_from_row(row, "call", "CPNG", spot_fallback=31.9)
    assert c["ticker"] == "CPNG" and c["side"] == "call"
    assert c["strike"] == 35.0 and c["spot"] == 31.9      # fallback used
    assert c["mid"] == 1.5 and c["ann_pct"] == 18.0

    # A `spot` column on the row wins over the fallback.
    row2 = row.copy()
    row2["spot"] = 32.5
    assert contract_from_row(row2, "put", "AMD", spot_fallback=99)["spot"] == 32.5
