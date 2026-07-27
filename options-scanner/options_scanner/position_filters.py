"""Hide selected broker option legs from the Close and Roll tabs.

**Display only.** A hidden position is still held, still assignable, and still
counts everywhere risk is computed. Coverage and sizing read the account
directly (``trade_actions.held_shares_and_short_calls_map`` →
``calls_coverable``) and must never see these rules: hiding a short call would
otherwise free its shares for a second covered call and let you oversell
against the same 100 shares. Apply these filters at the point of *render*,
never inside the account readers.

Rules come from ``settings_store`` (the ⚙️ Settings dialog writes them).
Omitted fields are wildcards — ``{"ticker": "WPC"}`` matches every WPC leg;
adding ``option_type`` / ``strike`` / ``expiration`` narrows it toward a single
leg.

Legs are the dicts returned by ``trade_actions.open_option_positions``:
``underlying``, ``option_type`` ("C"/"P"), ``strike``, ``expiration``,
``quantity``, ``direction``, …

Pure functions only — no Streamlit import, so all of this is unit-testable.
"""

from __future__ import annotations

from options_scanner.format import fmt_strike

# Same tolerance used to match a tracked trade to a held leg elsewhere in the
# app: broker strikes round-trip through floats, so never compare with ==.
_STRIKE_TOL = 1e-6


def _norm_ticker(v) -> str:
    return str(v or "").strip().upper()


def _norm_type(v) -> str:
    return str(v or "").strip().upper()[:1]


def _fmt_strike(v) -> str:
    """Label-only strike text (the app's shared format). Never used for keys —
    `rule_key` needs a format-stable value, not a display one."""
    try:
        return fmt_strike(v)
    except (TypeError, ValueError):
        return "$?"


def matches(rule: dict, leg: dict) -> bool:
    """Whether `rule` hides `leg`. Fields absent from the rule are wildcards.

    A rule with no ticker matches nothing — a blank ticker must never become a
    hide-everything wildcard.
    """
    if not isinstance(rule, dict) or not isinstance(leg, dict):
        return False

    ticker = _norm_ticker(rule.get("ticker"))
    if not ticker or ticker != _norm_ticker(leg.get("underlying")):
        return False

    opt = rule.get("option_type")
    if opt not in (None, "") and _norm_type(opt) != _norm_type(
            leg.get("option_type")):
        return False

    exp = rule.get("expiration")
    if exp not in (None, "") and str(exp).strip() != str(
            leg.get("expiration") or "").strip():
        return False

    strike = rule.get("strike")
    if strike not in (None, ""):
        try:
            if abs(float(strike) - float(leg.get("strike"))) > _STRIKE_TOL:
                return False
        except (TypeError, ValueError):
            return False  # unusable strike on either side → no match (safe)

    return True


def is_hidden(leg: dict, rules: list[dict] | None) -> bool:
    """Whether any rule hides `leg`."""
    return any(matches(r, leg) for r in (rules or []))


def split_hidden(legs: list[dict],
                 rules: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """``(visible, hidden)``, each in the input order. No rules → everything
    visible (and the same list contents, so callers can filter unconditionally).
    """
    if not rules:
        return list(legs or []), []
    visible, hidden = [], []
    for leg in legs or []:
        (hidden if is_hidden(leg, rules) else visible).append(leg)
    return visible, hidden


def unmatched_rules(rules: list[dict] | None,
                    legs: list[dict]) -> list[dict]:
    """Rules that hide none of `legs` — typically left over from a position
    that has since been closed. The Settings dialog offers to remove these so
    stale rules don't pile up invisibly."""
    return [r for r in (rules or [])
            if not any(matches(r, leg) for leg in (legs or []))]


def is_ticker_wide(rule: dict) -> bool:
    """Whether `rule` hides every leg on its underlying (no narrowing fields)."""
    return not any(rule.get(k) not in (None, "")
                   for k in ("option_type", "strike", "expiration"))


def split_rules_for_ui(rules: list[dict] | None,
                       legs: list[dict]) -> tuple[list[dict], list[dict]]:
    """``(checkbox_rules, carry_rules)`` for the Settings dialog.

    `checkbox_rules` are the rules the dialog can represent as a tick on the
    positions it is showing — currently ticker-wide rules for an underlying you
    hold legs on, since the dialog hides whole underlyings. `carry_rules` is
    everything else: narrower rules (an exact leg, a type — hand-written into
    settings.json or left by an earlier version), leftovers from positions since
    closed, and **every** rule when the position read failed and `legs` is empty.

    The dialog reconciles its ticks into one write, so a rule it never rendered
    would be deleted. Carried rules get their own keep/remove tick instead.
    """
    held_tickers = {_norm_ticker(leg.get("underlying")) for leg in (legs or [])}
    checkbox, carry = [], []
    for rule in rules or []:
        on_screen = (is_ticker_wide(rule)
                     and _norm_ticker(rule.get("ticker")) in held_tickers)
        (checkbox if on_screen else carry).append(rule)
    return checkbox, carry


def rule_key(rule: dict) -> str:
    """Stable identity for a rule — dedupe and Streamlit widget keys."""
    return "|".join([
        _norm_ticker(rule.get("ticker")),
        _norm_type(rule.get("option_type")),
        ("" if rule.get("strike") in (None, "")
         else f"{float(rule['strike']):g}"),
        str(rule.get("expiration") or ""),
    ])


def leg_key(leg: dict) -> str:
    """`rule_key` for the exact-leg rule that would hide `leg` — identifies a
    single leg among the rules."""
    return rule_key(rule_from_leg(leg))


def rule_from_leg(leg: dict, note: str = "") -> dict:
    """An exact-leg rule for `leg` (ticker + type + strike + expiration).

    The Settings dialog writes ticker-wide rules only; leg-level rules stay part
    of the vocabulary because the matcher honors them wherever they come from —
    a hand-edited settings.json, or a future finer-grained UI.
    """
    rule = {
        "ticker": _norm_ticker(leg.get("underlying")),
        "option_type": _norm_type(leg.get("option_type")),
        "expiration": str(leg.get("expiration") or ""),
    }
    try:
        rule["strike"] = float(leg.get("strike"))
    except (TypeError, ValueError):
        pass
    if note:
        rule["note"] = note
    return rule


def rule_label(rule: dict) -> str:
    """Human-readable rule, e.g. ``UBER 2026-06-18 $120 C`` or
    ``WPC — all legs``. Partial rules read as what they actually cover."""
    ticker = _norm_ticker(rule.get("ticker")) or "?"
    parts = []
    if rule.get("expiration"):
        parts.append(str(rule["expiration"]))
    if rule.get("strike") not in (None, ""):
        parts.append(_fmt_strike(rule["strike"]))
    opt = _norm_type(rule.get("option_type"))
    if opt:
        parts.append(opt)
    if not parts:
        return f"{ticker} — all legs"
    if not (rule.get("expiration") and rule.get("strike") not in (None, "")
            and opt):
        return f"{ticker} {' '.join(parts)} — all matching legs"
    return f"{ticker} {' '.join(parts)}"


def leg_label(leg: dict) -> str:
    """Human-readable held leg, e.g. ``UBER 2026-06-18 $120 C ×2 short``."""
    bits = [_norm_ticker(leg.get("underlying")) or "?",
            str(leg.get("expiration") or "?"),
            _fmt_strike(leg.get("strike")),
            _norm_type(leg.get("option_type")) or "?"]
    try:
        qty = int(leg.get("quantity") or 0)
        if qty:
            bits.append(f"×{qty}")
    except (TypeError, ValueError):
        pass
    direction = str(leg.get("direction") or "").strip().lower()
    if direction:
        bits.append(direction)
    return " ".join(bits)
