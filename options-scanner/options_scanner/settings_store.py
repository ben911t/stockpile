"""User-editable app settings — a single JSON file under
``options-scanner/settings/``.

The writable companion to ``config.toml``. The two layers are deliberately
**disjoint**, so there is never a precedence question about which one wins:

  ``config.toml``   machine + secrets layer — Schwab credentials, the ``paper``
                    live-order gate, the default data provider. Hand-edited and
                    never written by the app: ``tomllib`` is read-only, and a
                    round-trip through a TOML writer would drop every comment in
                    a file whose comments are its documentation (and would
                    silently discard whatever line the lenient loader in
                    ``config.py`` had merely warned about).
  ``settings.json`` preference layer — written by the ⚙️ Settings dialog. Holds
                    nothing security- or safety-critical, so a mis-click in the
                    UI can't arm live trading or lose a credential.

**Never** put a credential or the ``paper`` flag in here.

Gitignored — personal preferences, not shipped state.

Schema (version 1):
  version           schema int, for future migrations
  mask_balances     bool — hide account balance figures behind "•••••" wherever
                    they're shown (default False). Display-only, like
                    hidden_positions: nothing reads it to decide what an order
                    can afford, so a mis-click can't change what a trade does.
  hidden_positions  match rules that hide live broker option legs from the
                    Positions tab. Omitted fields are wildcards, so
                    ``{"ticker": "WPC"}`` hides every WPC leg while
                    ``{"ticker": "UBER", "option_type": "C", "strike": 120.0,
                    "expiration": "2026-06-18"}`` hides exactly one leg:
                      ticker       underlying symbol (required)
                      option_type  "C" | "P"      (optional)
                      strike       float          (optional)
                      expiration   "YYYY-MM-DD"   (optional)
                      note         free text shown in the UI (optional)
                      added_at     ISO-8601, set on write (optional)

Hiding is **display-only** — see ``position_filters`` for why these rules must
never reach coverage or sizing math.

Reading is cheap (one small file) and happens per Streamlit rerun, so an edit —
from the dialog or by hand — takes effect on the next interaction with no
restart.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).parents[1] / "settings"
_FILE = _DIR / "settings.json"

SCHEMA_VERSION = 1

# Keys the loader owns; anything else in the file is preserved untouched so a
# newer version's settings survive a round-trip through an older build.
_MANAGED_KEYS = ("version", "hidden_positions", "mask_balances")


def _defaults() -> dict:
    return {"version": SCHEMA_VERSION, "hidden_positions": [],
            "mask_balances": False}


def _normalize_rule(raw, errors: list[str]) -> dict | None:
    """One validated hidden-position rule, or None when it can't be trusted.

    A malformed *narrowing* field (option_type / strike / expiration) drops the
    whole rule rather than the field: dropping just the field would widen the
    rule — a bad strike on ``{"ticker": "UBER", "strike": "abc"}`` would hide
    every UBER leg instead of one. Rejecting the rule fails in the safe
    direction (the position stays visible) and the caller surfaces the note.
    """
    if not isinstance(raw, dict):
        errors.append(f"Ignored a hidden-position entry that isn't an object: "
                      f"{raw!r}")
        return None
    ticker = str(raw.get("ticker", "") or "").strip().upper()
    if not ticker:
        errors.append("Ignored a hidden-position entry with no ticker.")
        return None

    rule: dict = {"ticker": ticker}

    opt_raw = raw.get("option_type")
    if opt_raw not in (None, ""):
        opt = str(opt_raw).strip().upper()[:1]
        if opt not in ("C", "P"):
            errors.append(f'{ticker}: ignored the whole rule — option_type '
                          f'{opt_raw!r} is not "C" or "P".')
            return None
        rule["option_type"] = opt

    strike_raw = raw.get("strike")
    if strike_raw not in (None, ""):
        try:
            rule["strike"] = float(strike_raw)
        except (TypeError, ValueError):
            errors.append(f"{ticker}: ignored the whole rule — strike "
                          f"{strike_raw!r} is not a number.")
            return None

    exp_raw = raw.get("expiration")
    if exp_raw not in (None, ""):
        exp = str(exp_raw).strip()
        try:
            datetime.strptime(exp, "%Y-%m-%d")
        except ValueError:
            errors.append(f"{ticker}: ignored the whole rule — expiration "
                          f"{exp_raw!r} is not YYYY-MM-DD.")
            return None
        rule["expiration"] = exp

    for k in ("note", "added_at"):
        v = raw.get(k)
        if v not in (None, ""):
            rule[k] = str(v)
    return rule


def load() -> dict:
    """Settings with defaults filled in. Never raises.

    An unreadable or malformed file degrades to "nothing hidden" — the safe
    direction, since the alternative would hide live positions for a reason the
    user can't see — and records human-readable notes under ``_errors``. Mirrors
    the ``_warnings`` convention in :mod:`config`; see :func:`get_errors`.
    """
    out = _defaults()
    if not _FILE.exists():
        return out

    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        out["_errors"] = [
            f"Couldn't read {_FILE.name} ({e.__class__.__name__}) — no "
            f"positions are hidden. Fix or delete the file, or re-save from "
            f"the Settings dialog."]
        return out
    if not isinstance(raw, dict):
        out["_errors"] = [
            f"{_FILE.name} doesn't contain a settings object — no positions "
            f"are hidden."]
        return out

    errors: list[str] = []
    for k, v in raw.items():  # forward compat: keep keys we don't manage
        if k not in _MANAGED_KEYS and not k.startswith("_"):
            out[k] = v
    try:
        out["version"] = int(raw.get("version", SCHEMA_VERSION))
    except (TypeError, ValueError):
        errors.append(f"{_FILE.name}: version {raw.get('version')!r} isn't a "
                      f"number — treating the file as version "
                      f"{SCHEMA_VERSION}.")

    mask_raw = raw.get("mask_balances", False)
    if isinstance(mask_raw, bool):
        out["mask_balances"] = mask_raw
    else:
        # Anything else (a string "true", a number) is a hand-edit we won't
        # guess at. Default to showing: a preference file that got mangled
        # shouldn't leave you staring at "•••••" with no obvious cause.
        errors.append(f"mask_balances isn't true/false ({mask_raw!r}) — "
                      f"showing balances.")

    rules_raw = raw.get("hidden_positions", [])
    if not isinstance(rules_raw, list):
        errors.append("hidden_positions isn't a list — no positions are "
                      "hidden.")
        rules_raw = []
    out["hidden_positions"] = [
        r for r in (_normalize_rule(x, errors) for x in rules_raw) if r]

    if errors:
        out["_errors"] = errors
    return out


def save(settings: dict) -> None:
    """Write `settings` atomically (temp file + ``os.replace``).

    The atomicity matters more here than for the trade log: a truncated
    settings.json reads back as "nothing hidden", so a crash mid-write would
    silently unhide positions. Keys starting with ``_`` (``_errors``) are
    runtime-only and never persisted.
    """
    payload = {k: v for k, v in settings.items() if not k.startswith("_")}
    payload.setdefault("version", SCHEMA_VERSION)
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_name(_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, _FILE)


def get_errors(settings: dict) -> list[str]:
    """Human-readable notes recorded while loading (malformed file, rejected
    rule, …). Empty when the file loaded cleanly."""
    return list(settings.get("_errors", []))


def get_hidden_positions(settings: dict | None = None) -> list[dict]:
    """The validated hidden-position rules. Loads the file when not given one
    already read this rerun."""
    s = load() if settings is None else settings
    rules = s.get("hidden_positions", [])
    return list(rules) if isinstance(rules, list) else []


def get_mask_balances(settings: dict | None = None) -> bool:
    """Whether account balance figures are masked. Loads the file when not
    given one already read this rerun."""
    s = load() if settings is None else settings
    return bool(s.get("mask_balances", False))


def set_mask_balances(masked: bool) -> dict:
    """Persist the balance-masking preference. Returns the reloaded settings."""
    settings = load()
    settings["mask_balances"] = bool(masked)
    save(settings)
    return load()


def set_hidden_positions(rules: list[dict]) -> dict:
    """Replace the hidden-position list and persist. Returns the reloaded
    settings so the caller sees exactly what was stored (and any note about a
    rule that didn't survive validation)."""
    settings = load()
    stamped = []
    for r in rules or []:
        r = dict(r)
        r.setdefault("added_at", datetime.now().isoformat(timespec="seconds"))
        stamped.append(r)
    settings["hidden_positions"] = stamped
    save(settings)
    return load()
