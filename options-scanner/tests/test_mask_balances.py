"""Masking account balances — the ⚙️ preference and the 👁 session override.

For screen-sharing and recording. Display-only, exactly like hidden positions:
sizing, coverage and affordability checks read the real figures either way, so
masking can never change what an order does.
"""

import json

import pytest
import streamlit as st

from options_scanner import settings_store, settings_ui


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """A settings file of our own, and no leftover session override."""
    monkeypatch.setattr(settings_store, "_DIR", tmp_path)
    monkeypatch.setattr(settings_store, "_FILE", tmp_path / "settings.json")
    st.session_state.clear()
    yield
    st.session_state.clear()


def _write(**payload):
    (settings_store._FILE).write_text(json.dumps(payload), encoding="utf-8")


# ── the stored preference ────────────────────────────────────────────────────

def test_balances_show_by_default():
    # A new install shows its numbers; masking is opt-in.
    assert settings_store.get_mask_balances() is False
    assert settings_ui.balances_masked() is False


def test_the_preference_round_trips():
    settings_store.set_mask_balances(True)
    assert settings_store.get_mask_balances() is True
    assert settings_ui.balances_masked() is True
    settings_store.set_mask_balances(False)
    assert settings_ui.balances_masked() is False


def test_setting_the_preference_leaves_hidden_positions_alone():
    # The two preferences share a file; writing one must not drop the other.
    settings_store.set_hidden_positions([{"ticker": "WPC"}])
    settings_store.set_mask_balances(True)
    kept = settings_store.get_hidden_positions()
    assert [r["ticker"] for r in kept] == ["WPC"]
    assert settings_store.get_mask_balances() is True


def test_a_non_boolean_preference_falls_back_to_showing():
    # A hand-edit we won't guess at. Defaulting to masked would leave the user
    # staring at "•••••" with no obvious cause.
    _write(version=1, mask_balances="yes")
    s = settings_store.load()
    assert settings_store.get_mask_balances(s) is False
    assert any("mask_balances" in e for e in settings_store.get_errors(s))


def test_an_unreadable_file_shows_balances():
    (settings_store._FILE).write_text("{not json", encoding="utf-8")
    assert settings_store.get_mask_balances() is False


# ── the session override ─────────────────────────────────────────────────────

def test_the_eye_reveals_for_the_session_without_changing_the_setting():
    settings_store.set_mask_balances(True)
    settings_ui._toggle_reveal()
    assert settings_ui.balances_masked() is False
    # The stored preference is untouched — the next session is masked again.
    assert settings_store.get_mask_balances() is True


def test_the_eye_toggles_back():
    settings_store.set_mask_balances(True)
    settings_ui._toggle_reveal()
    settings_ui._toggle_reveal()
    assert settings_ui.balances_masked() is True


def test_it_can_also_hide_an_unmasked_account_for_the_session():
    # Useful the other way round: masking is off, someone joins the call.
    assert settings_ui.balances_masked() is False
    settings_ui._toggle_reveal()
    assert settings_ui.balances_masked() is True
    assert settings_store.get_mask_balances() is False


# ── the placeholder ──────────────────────────────────────────────────────────

def test_amounts_are_replaced_not_merely_blanked():
    settings_store.set_mask_balances(True)
    assert settings_ui.mask_money("$12,500.00") == "\\$•••••"


def test_the_placeholder_keeps_its_dollar_sign_escaped():
    # Two bare "$" in one markdown string are LaTeX delimiters — a masked figure
    # sharing a line with a visible one must not swallow both.
    settings_store.set_mask_balances(True)
    assert settings_ui.mask_money("$1").startswith("\\$")


def test_a_percentage_is_masked_without_inventing_a_dollar_sign():
    settings_store.set_mask_balances(True)
    assert settings_ui.mask_money("42.50%") == "•••••"


def test_unmasked_text_passes_through_untouched():
    assert settings_ui.mask_money("$12,500.00") == "$12,500.00"
    assert settings_ui.mask_money("42.50%") == "42.50%"


# ── display-only ─────────────────────────────────────────────────────────────

def test_masking_never_reaches_the_numbers_an_order_is_built_from():
    # The guarantee: mask_money is a formatter over already-rendered text, and
    # nothing in the order path consults the preference. If a sizing or
    # affordability check ever imports it, that's a bug this pins.
    import inspect
    from options_scanner import trade_actions, position_filters
    for mod in (trade_actions, position_filters):
        src = inspect.getsource(mod)
        assert "mask_balances" not in src
        assert "mask_money" not in src
