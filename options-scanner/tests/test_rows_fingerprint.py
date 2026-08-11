"""Row fingerprint that scopes a selectable table's widget key.

The bug: a row selection on the Watchlist leaderboard survived a filter change
by row *index*. After re-filtering, that index pointed at a different contract,
which the open-guard correctly read as a new selection — so nudging the delta
slider popped the Sell dialog open on a contract the user never clicked.

Folding this fingerprint into the table key rebuilds the widget (clearing the
selection) exactly when the row → contract mapping changes, and leaves it alone
otherwise.
"""

import pandas as pd

from options_scanner.display.leaderboard import rows_fingerprint as fp


def _board(rows=None):
    rows = rows if rows is not None else [
        ("AMD", 150.0, "2026-01-16"),
        ("AMD", 160.0, "2026-01-16"),
        ("CPNG", 30.0, "2026-09-18"),
    ]
    return pd.DataFrame(rows, columns=["ticker", "strike", "expiration"])


def test_same_rows_keep_the_same_key():
    # An unrelated rerun must not throw away the user's selection.
    assert fp(_board()) == fp(_board())


def test_filtering_rows_out_changes_the_key():
    # THE regression: tightening the delta range drops rows, so row 1 becomes a
    # different contract. The table has to be rebuilt.
    full = _board()
    assert fp(full) != fp(full.iloc[1:].reset_index(drop=True))


def test_reordering_changes_the_key():
    # A different sort re-points every index without changing membership.
    full = _board()
    assert fp(full) != fp(full.iloc[::-1].reset_index(drop=True))


def test_adding_a_row_changes_the_key():
    extra = pd.concat([_board(), _board([("NVDA", 900.0, "2026-03-20")])],
                      ignore_index=True)
    assert fp(_board()) != fp(extra)


def test_a_changed_strike_changes_the_key():
    moved = _board()
    moved.loc[0, "strike"] = 155.0
    assert fp(_board()) != fp(moved)


def test_a_changed_expiration_changes_the_key():
    moved = _board()
    moved.loc[0, "expiration"] = "2026-02-20"
    assert fp(_board()) != fp(moved)


def test_unrelated_columns_do_not_change_the_key():
    # Live quotes move constantly; a re-quote must not rebuild the table and
    # silently drop the selection mid-decision.
    a, b = _board(), _board()
    a["mid"], b["mid"] = [1.10, 2.20, 3.30], [1.15, 2.25, 3.35]
    assert fp(a) == fp(b)


def test_empty_and_unusable_frames_are_safe():
    assert isinstance(fp(pd.DataFrame()), str)
    assert fp(None) == "na"


def test_key_is_short_enough_for_a_widget_key():
    assert len(fp(_board())) <= 12
