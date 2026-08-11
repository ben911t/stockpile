"""Opening the Sell dialog from a table row, and clearing the row afterwards.

Streamlit gives no dialog-dismissed callback, so the row selection is cleared at
*open* time: `open_investigate` reports whether it opened one, and the caller
bumps its table key so the next full run — which is what dismissing the dialog
triggers — rebuilds the table with nothing selected.

Without that, the row stayed checked, the guard saw no new selection, and
re-picking the same contract needed an uncheck/recheck.
"""

import pytest

from options_scanner.display import leaderboard as lb


class FakeSt:
    """Just the session_state surface open_investigate touches."""

    def __init__(self):
        self.session_state = {}


@pytest.fixture
def st(monkeypatch):
    fake = FakeSt()
    monkeypatch.setattr(lb, "st", fake)
    # The dialog itself renders Streamlit widgets; count calls instead.
    opened = []
    monkeypatch.setattr(lb, "_investigate_put_dialog",
                        lambda c, **k: opened.append(c["ticker"]))
    fake.opened = opened
    return fake


def _contract(ticker="AMD", strike=150.0, expiration="2026-01-16"):
    return {"ticker": ticker, "strike": strike, "expiration": expiration}


def _open(contract, guard="g1"):
    return lb.open_investigate(contract, ticker_df=None, min_oi=25, top_n=5,
                               min_vol=0, provider="schwab", guard_key=guard)


def test_a_new_selection_opens_and_reports_it(st):
    assert _open(_contract()) is True
    assert st.opened == ["AMD"]


def test_the_same_selection_does_not_reopen(st):
    # The guard is what stops a dismissed dialog from popping straight back up
    # while the row is still checked.
    _open(_contract())
    assert _open(_contract()) is False
    assert st.opened == ["AMD"]


def test_a_different_contract_opens_again(st):
    _open(_contract())
    assert _open(_contract(strike=160.0)) is True
    assert len(st.opened) == 2


def test_a_rebuilt_table_can_reopen_the_same_contract(st):
    # After a dialog opens, the caller bumps its table key, which changes the
    # guard key too — so the same contract is pickable again immediately.
    _open(_contract(), guard="g_gen0")
    assert _open(_contract(), guard="g_gen1") is True
    assert len(st.opened) == 2


def test_opening_clears_stale_confirm_and_result_state(st):
    # A previous attempt's armed confirm or result banner must not greet the
    # user inside a freshly opened dialog.
    st.session_state["place_confirm_AMD_150_2026-01-16"] = {"values": [1, 2]}
    st.session_state["place_result_AMD_150_2026-01-16"] = {"ok": False}
    _open(_contract())
    assert "place_confirm_AMD_150_2026-01-16" not in st.session_state
    assert "place_result_AMD_150_2026-01-16" not in st.session_state


def test_two_tables_track_their_own_guards(st):
    # Calls and Puts boards render at once; one must not gate the other.
    assert _open(_contract(), guard="calls") is True
    assert _open(_contract(), guard="puts") is True
