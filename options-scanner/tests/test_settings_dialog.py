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
        pass

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


def test_malformed_settings_file_is_reported_in_the_dialog(store, monkeypatch):
    store._DIR.mkdir(parents=True, exist_ok=True)
    store._FILE.write_text("{broken", encoding="utf-8")
    fake, stored = _run(monkeypatch, [_leg()])
    assert stored == [] and fake.messages, "expected the error surfaced"
