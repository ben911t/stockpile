"""The ⚙️ Settings dialog and the "you have positions hidden" indicators.

Two entry points:

``render_settings_button()``
    The gear pinned in the title bar (every tab). Its label carries the hidden
    count, so a hidden position is visible from anywhere in the app — the whole
    point being that you can't forget you hid something.

``filter_hidden(legs, scope=…)`` / ``render_hidden_notice(hidden, scope=…)``
    A pair used by the Close and Roll tabs. The first drops hidden legs from
    what the tab is about to render, keeping only risk notices at the top (a
    broken settings file; a hidden short leg near expiration). The second draws
    the routine "N hidden" note, its list, and the session-only reveal toggle —
    called at the bottom of the tab so it never pushes the tables down.

Everything in the dialog is a checkbox, reconciled into one write at the end of
the dialog body. No buttons that mutate mid-render, so what you see is always
what's stored — and rules a hand-edit put in ``settings.json`` that the UI can't
express (a type-wide rule, say) are preserved rather than clobbered.

Hiding is display-only; see ``position_filters`` for why it must never reach
coverage or sizing math.
"""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from options_scanner import position_filters as pf
from options_scanner import positions_cache, settings_store

# A hidden short leg this close to expiration gets a loud warning rather than a
# quiet caption — assignment risk is exactly when a forgotten position bites.
_NEAR_EXPIRY_DTE = 7


# ── helpers ──────────────────────────────────────────────────────────────────

def _dte(expiration: str) -> int | None:
    try:
        return (datetime.strptime(str(expiration), "%Y-%m-%d").date()
                - date.today()).days
    except (TypeError, ValueError):
        return None


def _held_legs() -> list | None:
    """Every live option leg, or None when Schwab isn't connected/reachable.

    Shares the Close/Roll tabs' 60s cache, so opening the dialog costs no extra
    Schwab round-trip.
    """
    scfg = st.session_state.get("schwab_config") or {}
    if not scfg.get("app_key"):
        return None
    return positions_cache.option_positions(
        scfg.get("app_key", ""), scfg.get("app_secret", ""),
        scfg.get("callback_url", ""), scfg.get("token_file", ""))


def _near_expiry_shorts(legs: list[dict]) -> list[dict]:
    out = []
    for leg in legs:
        if str(leg.get("direction", "")).lower() != "short":
            continue
        d = _dte(leg.get("expiration"))
        if d is not None and d <= _NEAR_EXPIRY_DTE:
            out.append(leg)
    return out


# ── the notice shown on the Close and Roll tabs ──────────────────────────────

def _show_key(scope: str) -> str:
    return f"_osc_show_hidden_{scope}"


def filter_hidden(legs: list[dict], *,
                  scope: str) -> tuple[list[dict], list[dict]]:
    """``(to_render, hidden)`` for a tab about to draw its positions.

    `to_render` is `legs` minus what Settings hides — or all of `legs` when the
    reveal toggle is on; `hidden` is what the rules matched, which the caller
    passes to :func:`render_hidden_notice` at the *bottom* of the tab.

    Renders only what shouldn't wait until the bottom of the page: a broken
    settings file, and a hidden **short** leg near expiration. Both are risk
    notices rather than page furniture, so they stay above the tables.

    The reveal state is read from session_state rather than from the checkbox's
    return value, because that checkbox is now rendered *after* this call — its
    value from the previous run is what should govern this one.
    """
    settings = settings_store.load()
    for err in settings_store.get_errors(settings):
        st.warning(f"⚙️ Settings: {err}")

    rules = settings_store.get_hidden_positions(settings)
    visible, hidden = pf.split_hidden(legs, rules)
    if not hidden:
        return visible, []

    urgent = _near_expiry_shorts(hidden)
    if urgent:
        st.warning(
            f"⚠️ {len(urgent)} hidden short position(s) expire within "
            f"{_NEAR_EXPIRY_DTE} days: "
            + ", ".join(pf.leg_label(l) for l in urgent)
            + ". Hidden positions are still live and still assignable.")

    showing = bool(st.session_state.get(_show_key(scope)))
    return (list(legs or []) if showing else visible), hidden


def render_hidden_notice(hidden: list[dict], *, scope: str) -> None:
    """The "N hidden" note, its list, and the reveal toggle.

    Called at the **bottom** of the Close and Roll tabs so it doesn't push the
    positions table down; no-op when nothing is hidden. The toggle is
    session-only (never persisted — a peek must not quietly become the permanent
    state) and takes effect on the next rerun, which its own click triggers.
    """
    if not hidden:
        return
    showing = bool(st.session_state.get(_show_key(scope)))
    st.caption(
        f"🙈 {len(hidden)} position(s) hidden by your ⚙️ Settings"
        + (" — shown above." if showing else " — not shown above."))
    with st.expander(f"Hidden positions ({len(hidden)})", expanded=False):
        for leg in hidden:
            d = _dte(leg.get("expiration"))
            dte_txt = f" · {d}d to expiry" if d is not None else ""
            st.markdown(f"- {pf.leg_label(leg)}{dte_txt}")
        st.checkbox("Show these in the table above (this session only)",
                    key=_show_key(scope))
        st.caption("Manage hidden positions in ⚙️ Settings, top right.")


# ── the dialog ───────────────────────────────────────────────────────────────

def _render_hidden_positions(legs: list | None) -> None:
    """The dialog's one section: choose which underlyings stay out of the Close
    and Roll tables.

    Hiding is whole-position — one tick per underlying, covering every leg on it
    including ones opened later. (The rule format supports narrower matches and
    the tables honor them; the dialog just doesn't write them.)

    Reconciles every checkbox into a single ``set_hidden_positions`` write. Rules
    the checkboxes can't express are carried through untouched (with their own
    keep/remove checkbox), so the UI never silently drops a hand-edited rule.
    """
    settings = settings_store.load()
    for err in settings_store.get_errors(settings):
        st.warning(err)
    rules = settings_store.get_hidden_positions(settings)
    by_key = {pf.rule_key(r): r for r in rules}

    st.caption(
        "Hidden positions stay out of the **Close** and **Roll** tables. This "
        "is display only — the positions are still held, still assignable, and "
        "still count toward covered-call coverage and buying power.")

    if legs is None:
        st.info("Connect Schwab and select it as the data source to pick from "
                "your live positions. Existing rules are still editable below.")
        legs = []

    # Which held legs each surviving rule covers, so the UI can explain itself.
    held_by_ticker: dict[str, list] = {}
    for leg in legs:
        held_by_ticker.setdefault(
            str(leg.get("underlying", "")).upper(), []).append(leg)

    # Rules this screen can render as a tick vs. rules that must be carried
    # through untouched (hand-edited partials, positions since closed, and every
    # rule when the position read failed) — the reconcile at the end writes only
    # what it rendered, so anything else would be silently deleted.
    _, other_rules = pf.split_rules_for_ui(rules, legs)

    desired: list[dict] = []

    def _carry(rule: dict) -> dict:
        """Keep an existing rule's note/added_at so re-saving doesn't churn."""
        return dict(by_key.get(pf.rule_key(rule), rule))

    # One tick per underlying — hiding is whole-position, not per-leg. The legs
    # are listed read-only underneath so it's clear what each tick covers.
    if held_by_ticker:
        st.markdown("**Your live option positions**")
    wide_tickers = set()
    for ticker in sorted(held_by_ticker):
        t_legs = held_by_ticker[ticker]
        t_rule = {"ticker": ticker}
        if st.checkbox(
                f"**{ticker}** — hide all {len(t_legs)} leg(s)",
                value=pf.rule_key(t_rule) in by_key,
                key=f"osc_hide_all_{ticker}",
                help="Keeps every leg on this underlying out of the Close and "
                     "Roll tables, including ones you open later."):
            desired.append(_carry(t_rule))
            wide_tickers.add(ticker)

        lines = []
        for leg in t_legs:
            d = _dte(leg.get("expiration"))
            covered = ("" if ticker in wide_tickers else next(
                (f" — hidden by rule: {pf.rule_label(r)}"
                 for r in other_rules if pf.matches(r, leg)), ""))
            lines.append(f"· {pf.leg_label(leg)}"
                         + (f" · {d}d" if d is not None else "") + covered)
        st.caption("  \n".join(lines))

    # Rules the tick above can't represent: narrower rules (hand-written, or
    # from an earlier version) and leftovers from positions since closed. Untick
    # to delete. Ones on a ticker that's now hidden whole are subsumed by it, so
    # they're dropped rather than kept as clutter that does nothing.
    keep_rules = [r for r in other_rules if r.get("ticker") not in wide_tickers]
    if keep_rules:
        st.markdown("**Other rules in settings.json**")
        for r in keep_rules:
            n = sum(1 for leg in legs if pf.matches(r, leg))
            note = f" · {r['note']}" if r.get("note") else ""
            suffix = (f"matches {n} held leg(s)" if n
                      else "matches nothing you hold now")
            if st.checkbox(f"{pf.rule_label(r)} — {suffix}{note}",
                           value=True, key=f"osc_keep_rule_{pf.rule_key(r)}",
                           help="Untick to delete this rule."):
                desired.append(r)

    if not held_by_ticker and not keep_rules:
        st.caption("Nothing hidden, and no live option positions to hide.")

    # One write, at the end, only when something actually changed.
    if {pf.rule_key(r) for r in desired} != set(by_key):
        settings_store.set_hidden_positions(desired)

    st.divider()
    if st.button("Done", key="osc_settings_done", type="primary"):
        st.rerun()  # closes the dialog; the tabs behind re-filter


@st.dialog("⚙️ Settings", width="large")
def _settings_dialog() -> None:
    _render_hidden_positions(_held_legs())


def render_settings_button() -> None:
    """The gear pinned top-right, with the hidden count as its badge.

    Rendered on every tab (not just the position tabs) so an active blacklist is
    always in view. ``settings_pill_on`` vs ``settings_pill`` lets styles.css
    tint it when something is hidden; both share the positioning rule.
    """
    settings = settings_store.load()
    n = len(settings_store.get_hidden_positions(settings))
    has_err = bool(settings_store.get_errors(settings))

    if n:
        label, tip = (f"⚙️ {n} hidden",
                      f"{n} position(s) hidden from the Close and Roll tabs — "
                      f"click to manage.")
    elif has_err:
        label, tip = "⚙️ ⚠️", "Your settings file couldn't be read — click for " \
                              "details. Nothing is hidden."
    else:
        label, tip = "⚙️", "Settings"

    with st.container(key="settings_pill_on" if (n or has_err)
                      else "settings_pill"):
        clicked = st.button(label, key="osc_settings_gear", help=tip)
    if clicked:
        _settings_dialog()
