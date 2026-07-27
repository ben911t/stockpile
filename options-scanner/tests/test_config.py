"""Config loading: graceful handling of a malformed config.toml.

A bad `paper` value (the common mistake `paper = yes`, which is not valid
TOML) used to crash the app with a raw TOMLDecodeError traceback. It should
instead fall back to safe defaults (Schwab in paper mode) and surface a
human-readable warning.
"""

import textwrap

from options_scanner import config


def _write_cfg(tmp_path, monkeypatch, text: str):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_PATH", p)
    return p


def test_malformed_toml_does_not_raise(tmp_path, monkeypatch):
    # `paper = yes` is invalid TOML — historically the whole file failed to
    # parse. The loader now drops just that line and keeps the rest.
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        app_key = "abc"
        paper = yes
    """)
    cfg = config.load_config()  # must not raise
    warnings = config.get_config_warnings(cfg)
    assert warnings, "expected a warning for the invalid line"
    # The note should point at the paper flag specifically.
    assert "paper" in warnings[0].lower()
    assert "true" in warnings[0].lower() and "false" in warnings[0].lower()
    # Safe default: paper mode on (no live orders).
    assert config.get_schwab_config(cfg)["paper"] is True


def test_bad_paper_preserves_other_config(tmp_path, monkeypatch):
    # The key point of the line-tolerant loader: a bad `paper` value must not
    # discard valid sibling settings (e.g. Schwab creds → token countdown).
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        app_key = "abc"
        app_secret = "def"
        token_file = "~/tok.json"
        paper = yes
    """)
    cfg = config.load_config()
    s = config.get_schwab_config(cfg)
    assert s["app_key"] == "abc"
    assert s["app_secret"] == "def"
    assert s["token_file"] == "~/tok.json"
    assert s["paper"] is True  # bad value dropped → safe default
    assert config.get_config_warnings(cfg)


def test_non_bool_paper_string_defaults_to_paper(tmp_path, monkeypatch):
    # Parses fine as TOML, but "yes" is a string, not a bool.
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        paper = "yes"
    """)
    cfg = config.load_config()
    assert config.get_schwab_config(cfg)["paper"] is True
    assert any("true or false" in w for w in config.get_config_warnings(cfg))


def test_non_bool_paper_int_defaults_to_paper(tmp_path, monkeypatch):
    # `paper = 0` would coerce to live under a naive bool(); the strict path
    # keeps it in paper mode and warns.
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        paper = 0
    """)
    cfg = config.load_config()
    assert config.get_schwab_config(cfg)["paper"] is True
    assert config.get_config_warnings(cfg)


def test_valid_paper_false_stays_live(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        paper = false
    """)
    cfg = config.load_config()
    assert config.get_schwab_config(cfg)["paper"] is False
    assert config.get_config_warnings(cfg) == []


def test_valid_paper_true_no_warnings(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        paper = true
    """)
    cfg = config.load_config()
    assert config.get_schwab_config(cfg)["paper"] is True
    assert config.get_config_warnings(cfg) == []


def test_missing_paper_defaults_true_no_warnings(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        app_key = "abc"
    """)
    cfg = config.load_config()
    assert config.get_schwab_config(cfg)["paper"] is True
    assert config.get_config_warnings(cfg) == []


def test_generic_invalid_line_is_dropped_and_rest_survives(tmp_path, monkeypatch):
    # A parse error unrelated to paper: the bad line is dropped, valid
    # siblings survive, and paper stays in its safe default.
    _write_cfg(tmp_path, monkeypatch, """
        [schwab]
        token_file = "~/tok.json"
        app_key =
    """)
    cfg = config.load_config()
    warnings = config.get_config_warnings(cfg)
    assert warnings
    assert "ignored" in warnings[0].lower()
    assert config.get_schwab_config(cfg)["token_file"] == "~/tok.json"
    assert config.get_schwab_config(cfg)["paper"] is True
