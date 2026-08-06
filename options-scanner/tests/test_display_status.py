"""The one status word a trade shows, collapsed or expanded.

The bug this encodes: a collapsed row deliberately fetches nothing, so it fell
back to the store's lifecycle word ("open") while an expanded row substituted
the broker's ("working"). Same trade, two vocabularies, and the word appeared to
change just from opening the row.

The rule now: a known fill means the position is held; an unfilled opening order
says what the broker says; otherwise the store's word stands.

The live-position word is "held", not "open": on screen "open" reads as an open
ORDER (one still working), which is the opposite of what it means here.
"""

from options_scanner.tabs.trades import _display_status as ds


# ── the reported inconsistency ───────────────────────────────────────────────

def test_working_order_reads_the_same_collapsed_and_expanded():
    # Both views now have the broker status for an unresolved order (the
    # prefetch polls it even while collapsed), so both say "working".
    assert ds("open", "WORKING", False) == "working"


def test_filled_position_reads_the_same_collapsed_and_expanded():
    # Expanded (broker says FILLED) and collapsed (nothing fetched, but the fill
    # was recorded on the trade) must agree — this is where the header used to
    # flip between "filled" and "open".
    assert ds("open", "FILLED", True) == "held"
    assert ds("open", None, True) == "held"


# ── the individual rules ─────────────────────────────────────────────────────

def test_a_known_fill_means_the_position_is_held():
    # Even if a stale broker word is still around, a recorded fill wins.
    assert ds("open", "WORKING", True) == "held"


def test_an_unfilled_order_says_what_the_broker_says():
    for status in ("WORKING", "QUEUED", "PENDING_ACTIVATION"):
        assert ds("open", status, False) == status.lower()


def test_a_dead_order_is_not_called_held():
    # The whole point of surfacing the broker word: these must not read "held".
    for status in ("REJECTED", "EXPIRED", "CANCELED"):
        assert ds("open", status, False) == status.lower()


def test_nothing_known_falls_back_to_the_store():
    # A paper trade (no broker order) or an off-broker session. Still "held" —
    # a simulated position is held in the tracker, and "filled" would be a lie
    # since nothing was ever sent to a broker.
    assert ds("open", None, False) == "held"


def test_later_lifecycle_states_are_never_overridden():
    # A working CLOSING order must not relabel the record — that status belongs
    # to the opening order.
    assert ds("closing", "WORKING", True) == "closing"
    assert ds("closed", "FILLED", True) == "closed"
    assert ds("canceled", None, False) == "canceled"


def test_missing_store_status_defaults_to_held():
    assert ds(None, None, False) == "held"
    assert ds("", "WORKING", False) == "working"


def test_a_working_order_and_a_live_position_never_share_a_word():
    # The confusion this rename fixes: "open" read as "open order", so a
    # position you hold and an order still out there looked the same.
    assert ds("open", "WORKING", False) != ds("open", "FILLED", True)


def test_the_stored_lifecycle_word_is_not_rewritten():
    # Display-only. trades.json still says "open", and every filter in the app
    # (pending rolls, open legs, coverage) reads that raw value.
    from options_scanner.tabs.trades import _STATUS_WORDS
    assert set(_STATUS_WORDS) == {"open"}
