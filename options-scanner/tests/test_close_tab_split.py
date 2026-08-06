"""Splitting the Close tab's positions into Puts and Calls tables.

The tables hand back a row *index*, and that index picks which position gets
closed. If the displayed rows and the position list were filtered separately and
drifted apart, selecting a row would open the close builder on a different leg
than the one on screen — so both come out of one mask, and these pin that.
"""

import pandas as pd

from options_scanner.tabs.trades import _split_by_right as split
from options_scanner.tabs.trades import sort_by_moneyness


def _positions():
    # Deliberately interleaved, as the Close tab's sort (ticker, expiry, strike)
    # produces — puts and calls are not contiguous.
    return [
        {"option_type": "P", "underlying": "AMD", "strike": 150.0},
        {"option_type": "C", "underlying": "AMD", "strike": 200.0},
        {"option_type": "P", "underlying": "CPNG", "strike": 30.0},
        {"option_type": "C", "underlying": "NVDA", "strike": 900.0},
        {"option_type": "P", "underlying": "NVDA", "strike": 800.0},
    ]


def _frame(positions):
    return pd.DataFrame([{"Ticker": p["underlying"], "Strike": p["strike"]}
                         for p in positions])


def _aligned(subset, rows):
    return all(rows.loc[i, "Ticker"] == subset[i]["underlying"]
               and rows.loc[i, "Strike"] == subset[i]["strike"]
               for i in range(len(subset)))


def test_puts_side_is_aligned():
    pos = _positions()
    subset, rows = split(pos, _frame(pos), is_call=False)
    assert [p["underlying"] for p in subset] == ["AMD", "CPNG", "NVDA"]
    assert len(rows) == 3 and _aligned(subset, rows)


def test_calls_side_is_aligned():
    pos = _positions()
    subset, rows = split(pos, _frame(pos), is_call=True)
    assert [p["underlying"] for p in subset] == ["AMD", "NVDA"]
    assert len(rows) == 2 and _aligned(subset, rows)


def test_row_index_picks_the_leg_shown_on_that_row():
    # The failure this guards: row 1 of the Calls table must be the NVDA call,
    # not whatever sat at index 1 of the unsplit list (the AMD call).
    pos = _positions()
    subset, rows = split(pos, _frame(pos), is_call=True)
    assert rows.loc[1, "Ticker"] == "NVDA"
    assert subset[1]["underlying"] == "NVDA"
    assert subset[1]["option_type"] == "C"


def test_the_two_sides_partition_the_positions():
    pos = _positions()
    puts, _ = split(pos, _frame(pos), is_call=False)
    calls, _ = split(pos, _frame(pos), is_call=True)
    assert len(puts) + len(calls) == len(pos)
    assert not [p for p in puts if p in calls]


def test_an_empty_side_yields_nothing_to_render():
    pos = [{"option_type": "P", "underlying": "AMD", "strike": 150.0}]
    calls, rows = split(pos, _frame(pos), is_call=True)
    assert calls == [] and rows.empty


def test_option_type_is_read_case_insensitively():
    pos = [{"option_type": "c", "underlying": "AMD", "strike": 200.0}]
    calls, _ = split(pos, _frame(pos), is_call=True)
    assert len(calls) == 1


def test_a_missing_option_type_is_treated_as_a_put():
    # Matches the row builder's default; a leg must land in exactly one table.
    pos = [{"underlying": "AMD", "strike": 150.0}]
    puts, _ = split(pos, _frame(pos), is_call=False)
    calls, _ = split(pos, _frame(pos), is_call=True)
    assert len(puts) == 1 and calls == []


# ── …and the moneyness sort that runs on each side afterwards ───────────────
# Same alignment hazard, one step later: the sort permutes the rows, so it has
# to permute the position list with them.

def _itm_frame(positions, itms):
    df = _frame(positions)
    df["ITM%"] = itms
    return df


def test_the_sort_reorders_positions_with_their_rows():
    pos = [{"option_type": "P", "underlying": "AMD", "strike": 150.0},
           {"option_type": "P", "underlying": "CPNG", "strike": 30.0},
           {"option_type": "P", "underlying": "NVDA", "strike": 800.0}]
    rows = _itm_frame(pos, [4.0, -20.0, 12.0])
    subset, rows = sort_by_moneyness(pos, rows)
    assert [p["underlying"] for p in subset] == ["CPNG", "AMD", "NVDA"]
    assert _aligned(subset, rows)


def test_split_then_sort_still_selects_the_leg_on_the_row():
    # The Close tab's real sequence: split into puts/calls, then sort each side.
    pos = _positions()
    frame = _itm_frame(pos, [-2.0, 30.0, -18.0, -6.0, 9.0])
    subset, rows = split(pos, frame, is_call=True)
    subset, rows = sort_by_moneyness(subset, rows)
    assert [p["underlying"] for p in subset] == ["NVDA", "AMD"]
    assert _aligned(subset, rows)


def test_a_leg_with_no_moneyness_sorts_last_on_the_close_tab():
    pos = [{"option_type": "P", "underlying": "AMD", "strike": 150.0},
           {"option_type": "P", "underlying": "CPNG", "strike": 30.0}]
    rows = _itm_frame(pos, [None, -20.0])
    subset, rows = sort_by_moneyness(pos, rows)
    assert [p["underlying"] for p in subset] == ["CPNG", "AMD"]
    assert _aligned(subset, rows)
