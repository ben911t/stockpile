"""The ⚙️ Settings dialog's reconcile step, driven through a Streamlit stub.

The dialog hides whole underlyings (one tick per ticker) and turns every
checkbox on screen into a single ``set_hidden_positions`` write. That makes one
failure mode expensive: a rule the dialog never *rendered* being reconciled away
— opening Settings while Schwab is down, or with a narrower rule hand-written
into settings.json, must not wipe it. Regression-tested below.

The stub mirrors the one Streamlit behavior that matters for this logic: a key
already in ``session_state`` wins over the widget's ``value=`` default.
"""

import pytest

from options_scanner import settings_store as ss
from options_scanner import settings_ui


class FakeSt:
    """Minimal stand-in for the Streamlit surface `_render_hidden_positions`
    uses. `session_state` seeded before the call plays the part of the user's
    previous clicks."""

    def __init__(self, state=None):
        self.session_state = dict(state or {})
        self.checkboxes = []   # (label, value, disabled)
        self.messages = []
        self.texts = []        # everything rendered as markdown/caption/info

    def checkbox(self, label, value=False, key=None, disabled=False,
                 help=None):
        val = self.session_state.get(key, value)
        self.session_state[key] = val
        self.checkboxes.append((label, val, disabled))
        return val

    def button(self, *a, **k):
        return False           # never "Done" — we only exercise the reconcile

    def warning(self, msg, *a, **k):
        self.messages.append(msg)

    def caption(self, *a, **k):
        if a:
            self.texts.append(str(a[0]))

    info = markdown = divider = caption

    def rerun(self):
        raise AssertionError("the reconcile must not rerun the app")


def _leg(ticker="UBER", opt="C", strike=120.0, exp="2026-06-18",
         qty=2, direction="short"):
    return {"underlying": ticker, "option_type": opt, "strike": strike,
            "expiration": exp, "quantity": qty, "direction": direction}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_DIR", tmp_path / "settings")
    monkeypatch.setattr(ss, "_FILE", tmp_path / "settings" / "settings.json")
    return ss


def _run(monkeypatch, legs, state=None):
    """Render the dialog section once. Returns (fake_st, stored tickers)."""
    fake = FakeSt(state)
    monkeypatch.setattr(settings_ui, "st", fake)
    settings_ui._render_hidden_positions(legs)
    return fake, ss.get_hidden_positions()


def _keys(rules):
    from options_scanner import position_filters as pf
    return sorted(pf.rule_key(r) for r in rules)


# ── opening the dialog changes nothing on its own ────────────────────────────

def test_opening_with_no_rules_writes_nothing(store, monkeypatch):
    _, stored = _run(monkeypatch, [_leg()])
    assert stored == []
    assert not store._FILE.exists()


def test_opening_leaves_existing_rules_intact(store, monkeypatch):
    store.set_hidden_positions([{"ticker": "UBER"}])
    before = store.load()["hidden_positions"]
    _, stored = _run(monkeypatch, [_leg()])
    assert stored == before      # same rule, same added_at — no churn


def test_opening_preserves_a_leg_level_rule(store, monkeypatch):
    # The dialog no longer writes leg-level rules, so this one arrives via the
    # carry path — it must still survive an open.
    from options_scanner import position_filters as pf
    store.set_hidden_positions([pf.rule_from_leg(_leg())])
    _, stored = _run(monkeypatch, [_leg()])
    assert _keys(stored) == _keys([pf.rule_from_leg(_leg())])


# ── rules the dialog can't render must survive ───────────────────────────────

def test_rules_survive_when_positions_cannot_be_read(store, monkeypatch):
    # Schwab unreachable → legs is None. Every rule is off-screen, so the
    # reconcile must not delete any of it.
    from options_scanner import position_filters as pf
    rules = [{"ticker": "UBER"}, pf.rule_from_leg(_leg(ticker="AMD"))]
    store.set_hidden_positions(rules)
    _, stored = _run(monkeypatch, None)
    assert _keys(stored) == _keys(rules)


def test_rule_for_a_closed_position_survives(store, monkeypatch):
    from options_scanner import position_filters as pf
    stale = pf.rule_from_leg(_leg(ticker="WPC", opt="P", strike=60.0))
    store.set_hidden_positions([stale])
    _, stored = _run(monkeypatch, [_leg()])       # WPC no longer held
    assert _keys(stored) == _keys([stale])


def test_hand_edited_partial_rule_survives(store, monkeypatch):
    # A type-wide rule can't be expressed as a ticker tick, so it's carried —
    # and the dialog offers no per-leg ticks that could fight it.
    store.set_hidden_positions([{"ticker": "UBER", "option_type": "C"}])
    fake, stored = _run(monkeypatch, [_leg()])
    assert [r.get("option_type") for r in stored] == ["C"]
    assert not any(label.startswith("UBER 2026")
                   for label, _, _ in fake.checkboxes)


def test_unticking_a_carried_rule_removes_it(store, monkeypatch):
    from options_scanner import position_filters as pf
    stale = pf.rule_from_leg(_leg(ticker="WPC", opt="P", strike=60.0))
    store.set_hidden_positions([stale])
    _, stored = _run(monkeypatch, [_leg()],
                     state={f"osc_keep_rule_{pf.rule_key(stale)}": False})
    assert stored == []


# ── ticking / unticking a whole position ─────────────────────────────────────

def test_ticking_hide_all_hides_the_whole_ticker(store, monkeypatch):
    from options_scanner import position_filters as pf
    legs = [_leg(), _leg(strike=130.0, exp="2026-09-18"), _leg(ticker="AMD")]
    _, stored = _run(monkeypatch, legs, state={"osc_hide_all_UBER": True})
    assert [r["ticker"] for r in stored] == ["UBER"]
    assert pf.is_ticker_wide(stored[0])


def test_ticking_hide_all_subsumes_narrower_rules_on_that_ticker(store,
                                                                 monkeypatch):
    # A leg-level rule under a now-whole-ticker hide does nothing, so it goes
    # rather than lingering as clutter.
    from options_scanner import position_filters as pf
    legs = [_leg(), _leg(strike=130.0, exp="2026-09-18")]
    store.set_hidden_positions([pf.rule_from_leg(legs[0])])
    _, stored = _run(monkeypatch, legs, state={"osc_hide_all_UBER": True})
    assert [r["ticker"] for r in stored] == ["UBER"]
    assert all(pf.is_ticker_wide(r) for r in stored)


def test_unticking_hide_all_unhides_the_ticker(store, monkeypatch):
    legs = [_leg(), _leg(strike=130.0, exp="2026-09-18")]
    store.set_hidden_positions([{"ticker": "UBER"}])
    _, stored = _run(monkeypatch, legs, state={"osc_hide_all_UBER": False})
    assert stored == []


def test_hiding_one_ticker_leaves_another_visible(store, monkeypatch):
    legs = [_leg(), _leg(ticker="AMD", opt="P", strike=200.0)]
    store.set_hidden_positions([{"ticker": "UBER"}, {"ticker": "AMD"}])
    _, stored = _run(monkeypatch, legs, state={"osc_hide_all_AMD": False})
    assert [r["ticker"] for r in stored] == ["UBER"]


def test_unticking_one_carried_rule_leaves_the_others(store, monkeypatch):
    from options_scanner import position_filters as pf
    legs = [_leg(), _leg(strike=130.0, exp="2026-09-18")]
    rules = [pf.rule_from_leg(leg) for leg in legs]
    store.set_hidden_positions(rules)
    _, stored = _run(
        monkeypatch, legs,
        state={f"osc_keep_rule_{pf.rule_key(rules[0])}": False})
    assert _keys(stored) == _keys([rules[1]])


def test_leg_lines_never_carry_an_unescaped_dollar_sign(store, monkeypatch):
    # The AMD case: a ticker holding a put AND a call put two "$" into one
    # markdown string, which Streamlit reads as LaTeX math — it eats the
    # delimiters and reflows everything between them into a serif math run, so
    # the two legs rendered in visibly different type.
    legs = [_leg(ticker="AMD", opt="P", strike=150.0),
            _leg(ticker="AMD", opt="C", strike=200.0)]
    fake, _ = _run(monkeypatch, legs)
    rendered = [t for t in fake.texts if "$" in t]
    assert rendered, "expected the leg lines to be rendered"
    for text in rendered:
        assert text.count("$") == text.count("\\$"), text


def test_rule_labels_are_escaped_too(store, monkeypatch):
    from options_scanner import position_filters as pf
    store.set_hidden_positions([pf.rule_from_leg(_leg(ticker="AMD",
                                                      strike=200.0))])
    fake, _ = _run(monkeypatch, [_leg(ticker="AMD", opt="P", strike=150.0)])
    for label, _v, _d in fake.checkboxes:
        assert label.count("$") == label.count("\\$"), label


def test_malformed_settings_file_is_reported_in_the_dialog(store, monkeypatch):
    store._DIR.mkdir(parents=True, exist_ok=True)
    store._FILE.write_text("{broken", encoding="utf-8")
    fake, stored = _run(monkeypatch, [_leg()])
    assert stored == [] and fake.messages, "expected the error surfaced"


# ── the privacy section ──────────────────────────────────────────────────────
# Second section in the same dialog, writing the same file. Its own reconcile,
# so ticking it must not disturb the hidden-position rules beside it.

def _run_privacy(monkeypatch, state=None):
    fake = FakeSt(state)
    monkeypatch.setattr(settings_ui, "st", fake)
    settings_ui._render_privacy()
    return fake


def test_the_tick_reflects_the_stored_preference(store, monkeypatch):
    store.set_mask_balances(True)
    fake = _run_privacy(monkeypatch)
    assert fake.checkboxes[0][1] is True


def test_ticking_it_persists(store, monkeypatch):
    _run_privacy(monkeypatch, {"osc_mask_balances": True})
    assert store.get_mask_balances() is True


def test_unticking_it_persists(store, monkeypatch):
    store.set_mask_balances(True)
    _run_privacy(monkeypatch, {"osc_mask_balances": False})
    assert store.get_mask_balances() is False


def test_it_leaves_the_hidden_position_rules_alone(store, monkeypatch):
    store.set_hidden_positions([{"ticker": "WPC"}])
    _run_privacy(monkeypatch, {"osc_mask_balances": True})
    assert [r["ticker"] for r in store.get_hidden_positions()] == ["WPC"]


def test_changing_the_setting_clears_a_stale_session_reveal(store, monkeypatch):
    # Otherwise the old 👁 override would outrank the preference just set, and
    # ticking "mask" would appear to do nothing.
    store.set_mask_balances(False)
    fake = _run_privacy(monkeypatch, {"osc_mask_balances": True,
                                      settings_ui._REVEAL_KEY: True})
    assert settings_ui._REVEAL_KEY not in fake.session_state


# ── hiding by symbol: shares as well as legs ─────────────────────────────────
# Hiding used to enumerate option legs only, so a symbol you held nothing but
# shares of (no options on it at all) could not be picked — it simply never
# appeared in the dialog. Now the Positions tab shows stock, so the tick covers
# both and the list has to offer every symbol you hold either way.

def _stock(ticker="PLNH", shares=1500.0):
    return {"underlying": ticker, "asset": "stock", "shares": shares}


def test_a_symbol_you_only_hold_shares_of_can_be_hidden(store, monkeypatch):
    # The whole point: PLNH has no options, so it was previously unpickable.
    _, stored = _run(monkeypatch, [_leg(), _stock()],
                     state={"osc_hide_all_PLNH": True})
    assert [r["ticker"] for r in stored] == ["PLNH"]


def test_a_shares_only_symbol_is_offered_at_all(store, monkeypatch):
    fake, _ = _run(monkeypatch, [_stock()])
    assert any("PLNH" in lbl for lbl, _v, _d in fake.checkboxes)


def test_the_tick_covers_legs_and_shares_together(store, monkeypatch):
    # One rule, both asset types — "all or none, based on symbol".
    from options_scanner import position_filters as pf
    _, stored = _run(monkeypatch, [_leg("AAPL"), _stock("AAPL", 400)],
                     state={"osc_hide_all_AAPL": True})
    assert len(stored) == 1
    assert pf.matches(stored[0], _leg("AAPL"))
    assert pf.matches(stored[0], _stock("AAPL", 400))


def test_the_label_says_what_the_tick_covers(store, monkeypatch):
    fake, _ = _run(monkeypatch, [_leg("AAPL"), _stock("AAPL", 400),
                                 _stock("PLNH", 1500)])
    labels = " | ".join(lbl for lbl, _v, _d in fake.checkboxes)
    # Options + shares on one symbol, shares only on the other. A shares-only
    # symbol must never read as "hide all 0 leg(s)".
    assert "1 leg(s) + 400 shares" in labels
    assert "1,500 shares" in labels
    assert "0 leg(s)" not in labels


def test_a_shares_only_rule_still_renders_as_a_tick(store, monkeypatch):
    # split_rules_for_ui decides tick-vs-carry from the symbols on screen. If
    # stock rows weren't passed in, an existing PLNH rule would be exiled to
    # "Other rules" and read as matching nothing you hold.
    from options_scanner import position_filters as pf
    tick, carry = pf.split_rules_for_ui([{"ticker": "PLNH"}], [_stock()])
    assert len(tick) == 1 and carry == []


def test_stock_rows_never_reach_the_option_leg_formatter(store, monkeypatch):
    # leg_label would render one as "PLNH ? — ?": no strike, right or expiry.
    fake, _ = _run(monkeypatch, [_stock()])
    text = " ".join(fake.texts)
    assert "?" not in text
    assert "1,500 shares" in text


def test_hiding_one_symbol_leaves_a_shares_only_neighbour_visible(store,
                                                                 monkeypatch):
    _, stored = _run(monkeypatch, [_stock("PLNH"), _stock("WPC", 700)],
                     state={"osc_hide_all_PLNH": True})
    assert [r["ticker"] for r in stored] == ["PLNH"]


# ── what the dialog is handed ────────────────────────────────────────────────
# The tests above prove the renderer copes with stock rows; these prove it is
# actually given them. Both halves are needed for PLNH to show up in the app.

class _Cache:
    """Stands in for positions_cache — records the args, returns canned reads."""

    def __init__(self, legs, stocks):
        self._legs, self._stocks = legs, stocks

    def option_positions(self, *a):
        return self._legs

    def stock_positions(self, *a):
        return self._stocks


def _held(monkeypatch, legs, stocks):
    fake = FakeSt()
    fake.session_state = {}
    monkeypatch.setattr(settings_ui, "st", fake)
    fake.session_state["schwab_config"] = {"app_key": "k"}
    monkeypatch.setattr(settings_ui, "positions_cache", _Cache(legs, stocks))
    return settings_ui._held_positions()


def test_it_offers_option_legs_and_stock_together(monkeypatch):
    got = _held(monkeypatch, [_leg("AAPL")], [{"ticker": "PLNH",
                                               "shares": 1500.0}])
    assert {r["underlying"] for r in got} == {"AAPL", "PLNH"}


def test_stock_rows_carry_underlying_so_the_filter_can_match_them(monkeypatch):
    # position_filters keys on `underlying`; equity_positions calls it `ticker`.
    from options_scanner import position_filters as pf
    got = _held(monkeypatch, [], [{"ticker": "plnh", "shares": 10.0}])
    assert got[0]["underlying"] == "PLNH"      # and upper-cased
    assert pf.matches({"ticker": "PLNH"}, got[0])


def test_no_schwab_key_means_nothing_to_offer(monkeypatch):
    fake = FakeSt()
    monkeypatch.setattr(settings_ui, "st", fake)
    fake.session_state["schwab_config"] = {}
    assert settings_ui._held_positions() is None


def test_both_reads_failing_is_reported_as_unreadable(monkeypatch):
    # None (not []) so the dialog says "connect Schwab" and carries every rule
    # instead of reconciling them away as unmatched.
    assert _held(monkeypatch, None, None) is None


def test_one_read_failing_still_offers_the_other(monkeypatch):
    # A half-failure shouldn't hide everything — the missing half's rules are
    # carried untouched by split_rules_for_ui either way.
    assert [r["underlying"] for r in _held(monkeypatch, None,
                                           [{"ticker": "PLNH",
                                             "shares": 5.0}])] == ["PLNH"]
    assert [r["underlying"] for r in _held(monkeypatch, [_leg("AAPL")],
                                           None)] == ["AAPL"]
