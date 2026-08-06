"""settings.json round-trips, and degrades safely when hand-edited badly.

Every failure mode here has to fail in the same direction: **nothing hidden**.
A corrupt or ambiguous settings file must never hide a live position for a
reason the user can't see on screen.
"""

import json

import pytest

from options_scanner import settings_store as ss


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the module at a temp settings dir."""
    monkeypatch.setattr(ss, "_DIR", tmp_path / "settings")
    monkeypatch.setattr(ss, "_FILE", tmp_path / "settings" / "settings.json")
    return ss


def _write(store, text: str):
    store._DIR.mkdir(parents=True, exist_ok=True)
    store._FILE.write_text(text, encoding="utf-8")


# ── defaults / missing file ──────────────────────────────────────────────────

def test_missing_file_yields_defaults(store):
    s = store.load()
    assert s["hidden_positions"] == []
    assert s["version"] == store.SCHEMA_VERSION
    assert store.get_errors(s) == []


def test_get_hidden_positions_loads_when_not_given_settings(store):
    store.set_hidden_positions([{"ticker": "WPC"}])
    assert [r["ticker"] for r in store.get_hidden_positions()] == ["WPC"]


# ── round trip ───────────────────────────────────────────────────────────────

def test_save_load_round_trip(store):
    store.set_hidden_positions([
        {"ticker": "uber", "option_type": "c", "strike": 120,
         "expiration": "2026-06-18", "note": "at Fidelity"},
        {"ticker": "WPC"},
    ])
    rules = store.load()["hidden_positions"]
    assert len(rules) == 2
    assert rules[0] == {"ticker": "UBER", "option_type": "C", "strike": 120.0,
                        "expiration": "2026-06-18", "note": "at Fidelity",
                        "added_at": rules[0]["added_at"]}
    assert rules[1]["ticker"] == "WPC"


def test_set_hidden_positions_stamps_added_at_and_keeps_existing(store):
    store.set_hidden_positions([{"ticker": "WPC", "added_at": "2020-01-01T00:00:00"}])
    first = store.load()["hidden_positions"][0]
    assert first["added_at"] == "2020-01-01T00:00:00"
    store.set_hidden_positions([{"ticker": "AMD"}])
    assert store.load()["hidden_positions"][0]["added_at"]


def test_set_hidden_positions_replaces_the_list(store):
    store.set_hidden_positions([{"ticker": "WPC"}, {"ticker": "AMD"}])
    store.set_hidden_positions([{"ticker": "AMD"}])
    assert [r["ticker"] for r in store.load()["hidden_positions"]] == ["AMD"]


def test_clearing_all_rules_persists_as_empty(store):
    store.set_hidden_positions([{"ticker": "WPC"}])
    store.set_hidden_positions([])
    assert store.load()["hidden_positions"] == []


def test_save_leaves_no_temp_file_behind(store):
    store.set_hidden_positions([{"ticker": "WPC"}])
    assert [p.name for p in store._DIR.iterdir()] == ["settings.json"]


def test_runtime_error_keys_are_not_persisted(store):
    store.save({"hidden_positions": [{"ticker": "WPC"}],
                "_errors": ["should not be written"]})
    assert "_errors" not in json.loads(store._FILE.read_text(encoding="utf-8"))


def test_unknown_keys_survive_a_round_trip(store):
    # Forward compat: an older build must not eat a newer build's settings.
    _write(store, json.dumps({"version": 1, "hidden_positions": [],
                              "future_feature": {"enabled": True}}))
    store.save(store.load())
    assert json.loads(store._FILE.read_text(
        encoding="utf-8"))["future_feature"] == {"enabled": True}


# ── malformed files degrade to "nothing hidden" + a note ─────────────────────

def test_corrupt_json_hides_nothing_and_reports(store):
    _write(store, "{not json at all")
    s = store.load()
    assert s["hidden_positions"] == []
    assert store.get_errors(s), "expected a human-readable note"
    assert "settings.json" in store.get_errors(s)[0]


def test_non_object_file_hides_nothing_and_reports(store):
    _write(store, "[1, 2, 3]")
    s = store.load()
    assert s["hidden_positions"] == [] and store.get_errors(s)


def test_hidden_positions_wrong_type_hides_nothing_and_reports(store):
    _write(store, json.dumps({"hidden_positions": "WPC"}))
    s = store.load()
    assert s["hidden_positions"] == []
    assert any("hidden_positions" in e for e in store.get_errors(s))


def test_entry_without_ticker_is_dropped(store):
    _write(store, json.dumps({"hidden_positions": [
        {"option_type": "C"}, {"ticker": "WPC"}]}))
    s = store.load()
    assert [r["ticker"] for r in s["hidden_positions"]] == ["WPC"]
    assert store.get_errors(s)


def test_non_object_entry_is_dropped(store):
    _write(store, json.dumps({"hidden_positions": ["WPC", {"ticker": "AMD"}]}))
    s = store.load()
    assert [r["ticker"] for r in s["hidden_positions"]] == ["AMD"]
    assert store.get_errors(s)


@pytest.mark.parametrize("bad", [
    {"ticker": "UBER", "strike": "abc"},
    {"ticker": "UBER", "option_type": "X"},
    {"ticker": "UBER", "expiration": "06/18/2026"},
])
def test_bad_narrowing_field_drops_the_whole_rule(store, bad):
    # Dropping just the bad field would WIDEN the rule (hiding every UBER leg
    # instead of one), so the rule goes and the positions stay visible.
    _write(store, json.dumps({"hidden_positions": [bad]}))
    s = store.load()
    assert s["hidden_positions"] == []
    assert store.get_errors(s)


def test_bad_version_still_loads_rules(store):
    _write(store, json.dumps({"version": "one",
                              "hidden_positions": [{"ticker": "WPC"}]}))
    s = store.load()
    assert [r["ticker"] for r in s["hidden_positions"]] == ["WPC"]
    assert s["version"] == store.SCHEMA_VERSION
    assert store.get_errors(s)
