"""Trades tab — what the assisted put-seller has placed, and managing it.

Lists every recorded short-put trade (``trades_store``), estimates live
cost-to-close and unrealized P/L from a Schwab re-quote, and closes a
position via a market-gated, confirm-step BUY_TO_CLOSE order. A *live*
position is closed with a real order (config ``paper=false`` + market open);
a *paper* trade is closed in the tracker only. Schwab-only.
"""

from __future__ import annotations

import concurrent.futures
import functools
import time
from datetime import datetime

import streamlit as st

try:  # internal API, but stable across recent Streamlit — degrade gracefully
    from streamlit.runtime.scriptrunner import (
        add_script_run_ctx, get_script_run_ctx,
    )
except Exception:  # pragma: no cover - shields against Streamlit layout drift
    add_script_run_ctx = None

    def get_script_run_ctx():
        return None

from options_scanner import (
    confirm_gate, positions_cache, settings_ui, trade_actions, trades_store,
)
from options_scanner.ui_theme import df_height, metric_card, section_header

# Max wall-clock the Trades tab waits for all per-trade Schwab reads (order
# status + cost-to-close re-quote), which run in parallel. A slower or hung
# call past this leaves that trade's data unavailable rather than freezing the
# tab. Kept under the client's own HTTP timeout (SCHWAB_HTTP_TIMEOUT_S).
_TRADES_FETCH_TIMEOUT_S = 8.0


def _kv_table_html(rows: "list[tuple[str, str]]") -> str:
    """Borderless key/value HTML table — matches the Sell Put snapshot. ($ is
    safe here: inside raw HTML it's not parsed as LaTeX.)"""
    cells = "".join(
        "<tr>"
        f"<td style='padding:4px 14px;color:#808495'>{f}</td>"
        f"<td style='padding:4px 14px;font-variant-numeric:tabular-nums'>{v}</td>"
        "</tr>"
        for f, v in rows
    )
    return ("<table style='border-collapse:collapse;font-size:1rem'>"
            f"{cells}</table>")


def _fmt_spot_day(spot, pct):
    """``(spot_text, pct_text, is_up)`` for a spot + day-change readout, or None
    when spot is unusable (missing, NaN, non-positive, unparseable).

    One formatting rule, two renderers (`_day_head_px` for HTML, `_day_head_md`
    for markdown), so the collapsed row header and the chart header can't
    disagree. Matches the Spot cell and the Spot/Day% table columns ($x.xx,
    ±x.x%), and `is_up` matches `_day_chart`'s `last >= open` test so a flat
    session isn't red text under a green line. `pct_text` is "" when the change
    is unknown.
    """
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        return None
    if not (spot == spot and spot > 0):  # NaN or nonsense
        return None
    pct_text, is_up = "", True
    try:
        pct = float(pct)
        if pct == pct:
            pct_text, is_up = f"{pct:+.1f}%", pct >= 0
    except (TypeError, ValueError):
        pass
    return f"${spot:,.2f}", pct_text, is_up


def _day_head_px(spot, pct) -> str:
    """The price segment of the day-chart header, as HTML. Empty string when spot
    is unknown — better a bare "TODAY · TICKER" than a dash pretending to be a
    quote.

    The quote AND the change are colored together, green up / red down, matching
    the chart line: color here means one thing only — which way the underlying is
    today. With no change available there's no direction to state, so the price
    stays plain rather than defaulting to green.
    """
    parts = _fmt_spot_day(spot, pct)
    if parts is None:
        return ""
    spot_text, pct_text, is_up = parts
    body = f"{spot_text} {pct_text}".strip()
    style = ("font-size:0.82rem;font-weight:600;"
             "font-variant-numeric:tabular-nums;")
    if pct_text:
        style += f"color:{'#16a34a' if is_up else '#dc2626'};"
    return f"<span style='{style}'>{body}</span>"


def _day_head_md(spot, pct) -> str:
    """The same readout as markdown, for the collapsed row header — a
    ``st.button`` label renders markdown, not HTML.

    Quote + change colored as one unit via Streamlit's ``:green[…]`` /
    ``:red[…]`` directives (theme-aware, unlike a hex). The word "spot" stays
    plain: it's a label, and labels on that line carry no color. Dollar signs are
    backslash-escaped because a *pair* of them anywhere in a markdown string is
    read as LaTeX math — the strike earlier in the header supplies the other one.
    Empty string when spot is unknown, so the caller can drop the segment.
    """
    parts = _fmt_spot_day(spot, pct)
    if parts is None:
        return ""
    spot_text, pct_text, is_up = parts
    body = f"{spot_text} {pct_text}".strip().replace("$", "\\$")
    if not pct_text:
        return f"spot {body}"
    return f"spot " + (f":green[{body}]" if is_up else f":red[{body}]")


def _day_chart(series: "list | None"):
    """A small, minimal intraday line for the trade's ticker — green up / red
    down on the session, with a faint dashed line at the open. Returns an Altair
    chart, or None when there's too little data to draw.

    `series` is a list of (unix_seconds, close), oldest first (see `_intraday`).
    """
    if not series or len(series) < 2:
        return None
    import altair as alt
    import pandas as pd
    from datetime import datetime, timezone

    # Convert each bar to the viewer's local time, then drop tzinfo so Altair
    # renders the value verbatim (no browser re-offset) — the axis/tooltip then
    # read as market-local clock time instead of UTC.
    def _local(ts):
        return (datetime.fromtimestamp(ts, tz=timezone.utc)
                .astimezone().replace(tzinfo=None))

    df = pd.DataFrame({
        "t": [_local(ts) for ts, _ in series],
        "price": [px for _, px in series],
    })
    open_px, last_px = series[0][1], series[-1][1]
    color = "#16a34a" if last_px >= open_px else "#dc2626"
    line = alt.Chart(df).mark_line(color=color, strokeWidth=1.7).encode(
        # Sparse HH:MM ticks so the line has a time reference without cluttering
        # this small chart (was axis=None → no time shown at all).
        x=alt.X("t:T", axis=alt.Axis(title=None, format="%H:%M", labelFontSize=9,
                                     tickCount=4, grid=False, domain=False,
                                     ticks=False, labelColor="#9ca3af")),
        y=alt.Y("price:Q", scale=alt.Scale(zero=False),
                axis=alt.Axis(title=None, labelFontSize=9, tickCount=3,
                              grid=False, domain=False, ticks=False)),
        tooltip=[alt.Tooltip("t:T", title="Time", format="%H:%M"),
                 alt.Tooltip("price:Q", title="Price", format="$.2f")],
    )
    open_rule = alt.Chart(pd.DataFrame({"y": [open_px]})).mark_rule(
        color="#9ca3af", strokeDash=[3, 3], size=1).encode(y="y:Q")
    # usermeta.embedOptions.actions=False strips the vega "⋮" menu (Save/View
    # Source/Compiled Vega) — it only dumps raw JSON with no way back, and this
    # sparkline has nothing to export.
    return ((open_rule + line)
            .properties(height=104,
                        usermeta={"embedOptions": {"actions": False}})
            .configure_view(strokeWidth=0))


@st.cache_data(ttl=30, show_spinner=False)
def _close_quote(app_key: str, app_secret: str, callback_url: str,
                 token_file: str, ticker: str, expiration: str,
                 strike: float, option_type: str = "P") -> dict | None:
    """Cached (30s) read-only re-quote for one option leg (put or call, per
    `option_type`). Returns dict or None."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
    except Exception:
        return None
    return trade_actions.requote_option(client, ticker, expiration, strike,
                                        option_type=option_type)


@st.cache_data(ttl=60, show_spinner=False)
def _intraday(app_key: str, app_secret: str, callback_url: str,
              token_file: str, ticker: str) -> "list | None":
    """Cached (60s) intraday closes for the ticker's most recent session →
    [(unix_seconds, close), …] oldest first, or None when unavailable.

    Schwab 5-minute bars, filtered to the last session present so it reads as
    'today' while the market is open (and the prior close otherwise). Feeds the
    Trades tab's small day chart."""
    from stocks_shared.schwab_live import get_client, fetch_price_history_schwab
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
        bars = fetch_price_history_schwab(client, ticker, "5m", limit=500)
    except Exception:
        return None
    if not bars:
        return None
    from datetime import datetime, timezone

    def _localdate(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().date()

    last_day = _localdate(bars[-1]["time"])
    series = [(b["time"], float(b["close"])) for b in bars
              if b.get("close") and _localdate(b["time"]) == last_day]
    return series or None


@st.cache_data(ttl=60, show_spinner=False)
def _market_open(app_key: str, app_secret: str, callback_url: str,
                 token_file: str) -> bool | None:
    """Cached (60s) read-only: is the equity-options market open? None when
    Schwab is unreachable (caller keeps live closing disabled — fail safe)."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
    except Exception:
        return None
    return trade_actions.market_is_open(client)


@st.cache_data(ttl=15, show_spinner=False)
def _order_status(app_key: str, app_secret: str, callback_url: str,
                  token_file: str, order_id, last4: str) -> dict | None:
    """Cached (15s) read-only broker status for one order. None if
    unavailable."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
    except Exception:
        return None
    return trade_actions.get_order_status(client, order_id, last4 or None)


@st.cache_data(ttl=3600, show_spinner=False)
def _fill_snapshot(app_key: str, app_secret: str, callback_url: str,
                   token_file: str, ticker: str, expiration: str,
                   strike: float, fill_price: float,
                   filled_at_iso: str) -> dict | None:
    """Cached (1h) reconstruction of the spot + delta at an order's fill.

    Keyed by the fill's identity so it computes at most once per trade; None
    when the fill can't be located in intraday history. See
    ``trade_actions.fill_snapshot``."""
    from datetime import datetime
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
        filled_at = datetime.fromisoformat(filled_at_iso)
    except Exception:
        return None
    return trade_actions.fill_snapshot(client, ticker, expiration, strike,
                                       fill_price, filled_at)


def _cancel_order(scfg: dict, trade: dict) -> dict:
    """Cancel a tracked trade's working order; flip the tracker to canceled.
    Returns {ok, msg}."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    last4 = (trade.get("account") or "")[-4:]
    res = trade_actions.cancel_order(client, trade.get("order_id"),
                                     last4 or None)
    if not res["ok"]:
        return {"ok": False, "msg": f"Cancel failed: {res['error']}"}
    trades_store.update(trade["id"], status="canceled",
                        canceled_at=datetime.now().isoformat(timespec="seconds"))
    return {"ok": True, "msg": "✅ Cancel sent. Verify at your broker."}


def _book_close(trade: dict, closed_n: int, close_cost, closed_at: str) -> None:
    """Record ``closed_n`` contracts of ``trade`` as closed at ``close_cost``.

    Full close (closed_n ≥ quantity): the trade itself is marked closed.
    Partial close: the closed contracts become a *new* "closed" record (so they
    carry their own realized P/L) and the original trade's quantity is reduced
    to the open remainder, reverting it to "open" so it stays tracked and can be
    closed again. Closing-order fields are cleared either way.
    """
    total = int(trade.get("quantity", 1))
    closed_n = max(1, min(int(closed_n), total))
    if closed_n >= total:
        trades_store.update(trade["id"], status="closed",
                            close_cost=close_cost, closed_at=closed_at,
                            close_order_id=None, close_limit_px=None,
                            close_qty=None)
        return
    # Partial: split the closed contracts into their own closed record (keeping
    # the opening identity/credit/fill snapshot so its realized P/L is right)…
    _closed = {k: v for k, v in trade.items()
               if k not in ("id", "status", "close_order_id",
                            "close_limit_px", "close_qty")}
    _closed.update({"quantity": closed_n, "status": "closed",
                    "close_cost": close_cost, "closed_at": closed_at})
    trades_store.add(_closed)
    # …and shrink the original to the still-open remainder.
    trades_store.update(trade["id"], quantity=total - closed_n, status="open",
                        close_order_id=None, close_limit_px=None,
                        close_qty=None)


def _settle_closing_trade(t: dict, cbs: "dict | None") -> "tuple[bool, str|None]":
    """Settle a trade sitting in "closing" against its polled broker status.

    Returns ``(changed, note)`` — `changed` True when the store was updated (the
    caller should refresh/rerun), `note` a one-time message when the order ended
    without fully filling. A still-working order, or an unavailable status,
    leaves the trade alone → ``(False, None)``.

    Pure store mutation: it deliberately touches no session_state, so the two
    callers can surface the note where each needs it (per-row on the Trades tab,
    top-of-tab for the on-load reconciliation). Shared so a fill or an expiry
    settles identically wherever it's first noticed."""
    if not cbs:
        return False, None
    qty = int(t.get("quantity", 1))
    _lim = t.get("close_limit_px")
    _cqty = int(t.get("close_qty") or qty)
    _cstat = cbs.get("status")

    def _at(v):
        return (v.isoformat() if v
                else datetime.now().isoformat(timespec="seconds"))

    def _cost():
        _px = cbs.get("fill_price")
        return round(_px, 2) if _px is not None else _lim

    if _cstat == "FILLED":
        _book_close(t, int(cbs.get("filled") or _cqty), _cost(),
                    _at(cbs.get("filled_at")))
        return True, None
    if cbs.get("cancelable"):
        return False, None            # still working at the broker
    # Terminal but not FILLED → the buy-to-close never (fully) executed: a day
    # order that EXPIRED at the close, or was CANCELED/REJECTED at the broker.
    # Don't leave the trade stuck in "closing" — book any filled contracts and
    # return the rest to a normal open position.
    _filled_n = int(float(cbs.get("filled") or 0))
    _external = t.get("opened_from") == "schwab_position"
    if _filled_n > 0:
        # Partial fill then terminal: book the filled contracts (a split, like a
        # partial close).
        _book_close(t, _filled_n, _cost(), _at(cbs.get("filled_at")))
        if _external:
            # The remainder isn't app-tracked (it lives at the broker) — drop
            # it, don't leave a phantom open trade.
            trades_store.remove(t["id"])
            return True, (f"Closing order {_cstat} after filling {_filled_n} of "
                          f"{qty} — the rest stays open at your broker (see the "
                          "**Close** tab).")
        return True, (f"Closing order {_cstat} after filling {_filled_n} of "
                      f"{qty} — the rest is open again.")
    if _external:
        trades_store.remove(t["id"])
        return True, (f"Closing order {_cstat} without filling — the position is "
                      "unchanged at your broker (see the **Close** tab).")
    trades_store.update(t["id"], status="open", close_order_id=None,
                        close_limit_px=None, close_qty=None)
    return True, (f"Closing order {_cstat} without filling — the position is "
                  "open again; place a new closing order when ready.")


def _reconcile_closing_orders(scfg: dict) -> bool:
    """Poll every tracked working closing order and settle the ones that already
    resolved at the broker. Returns True when anything changed.

    Run on load by BOTH the Trades and Close tabs. Without it a closing order
    that expired overnight stayed "closing" in the store until the user happened
    to expand that specific row on the Trades tab (the per-trade Schwab reads are
    deferred behind row expansion) — which left the Close tab refusing to place a
    new close, insisting an order was "already working", for an order that had
    been dead since the previous session's close.

    Cheap enough to run unconditionally: only trades actually sitting in
    "closing" are polled (usually none or a couple), and `_order_status` is
    cached 15s — unlike the per-trade quote/chart prefetch that made deferring
    worthwhile in the first place. Paper trades never reach a broker, so they're
    skipped."""
    if not scfg.get("app_key"):
        return False
    pending = [t for t in trades_store.load()
               if t.get("status") == "closing" and t.get("close_order_id")
               and not t.get("paper")]
    if not pending:
        return False
    ak, sk = scfg.get("app_key", ""), scfg.get("app_secret", "")
    cb, tf = scfg.get("callback_url", ""), scfg.get("token_file", "")
    changed = False
    for t in pending:
        _chg, _note = _settle_closing_trade(
            t, _order_status(ak, sk, cb, tf, t.get("close_order_id"),
                             (t.get("account") or "")[-4:]))
        if not _chg:
            continue
        changed = True
        # Stash for top-of-tab display: the record may have been removed
        # entirely, so a per-row note would have nowhere to render.
        if _note:
            st.session_state.setdefault("_close_reconcile_notes", []).append(
                _note)
        st.session_state.pop(f"close_result_{t['id']}", None)
    if changed:
        _order_status.clear()
    return changed


def _render_reconcile_notes() -> None:
    """Show (once) any notes left by the on-load closing-order reconciliation."""
    for _n in st.session_state.pop("_close_reconcile_notes", []):
        st.info(_n)


def _submit_close(scfg: dict, trade: dict, limit: float, live: bool,
                  close_qty: int | None = None) -> dict:
    """Close a tracked put. `live` True → send a real BUY_TO_CLOSE order;
    False → record the close in the tracker only. `close_qty` contracts (≤ the
    position size, default all) are bought back. Updates the store; returns
    {ok, msg}."""
    from stocks_shared.schwab_live import get_client
    qty = int(trade.get("quantity", 1))
    close_qty = max(1, min(int(close_qty), qty)) if close_qty else qty
    debit = round(float(limit) * 100 * close_qty, 2)
    now = datetime.now().isoformat(timespec="seconds")
    _of = f" of {qty}" if close_qty < qty else ""
    if not live:
        # A tracker-only close is valid only for a paper trade. Never "paper
        # close" a real position — that would mark a still-open broker position
        # closed in the tracker. The UI blocks this; this is belt-and-suspenders.
        if not trade.get("paper"):
            return {"ok": False,
                    "msg": ("Live position can't be closed in paper mode — set "
                            "paper=false in config.toml and restart.")}
        _book_close(trade, close_qty, round(float(limit), 2), now)
        # Toast format: headline first, then ONE sentence per line (run_app
        # renders each following line as its own bullet).
        return {"ok": True,
                "msg": (f"📝 Close recorded in the tracker\n"
                        f"{close_qty}{_of} contract(s), debit ${debit:,.0f}.\n"
                        "No live order was sent.")}
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    last4 = (trade.get("account") or "")[-4:]
    resolved = trade_actions.resolve_account_hash(client, last4 or None)
    if not resolved:
        return {"ok": False,
                "msg": "Couldn't resolve the account — close NOT sent."}
    account_hash, mask = resolved
    res = trade_actions.place_option_close_order(
        client, ticker=trade.get("ticker"),
        strike=float(trade.get("strike", 0)),
        expiration=trade.get("expiration", ""), limit=float(limit),
        quantity=close_qty, account_hash=account_hash,
        option_type=trade.get("option_type", "P"))
    if not res["ok"]:
        return {"ok": False, "msg": f"Close rejected: {res['error']}"}
    # The buy-to-close is accepted but may sit working before it fills. Track it
    # as "closing" (not yet "closed") so the tab polls its status and offers a
    # Cancel — the trade is finalized (and split, if a partial close) only once
    # the close fills.
    trades_store.update(trade["id"], status="closing",
                        close_order_id=res["order_id"],
                        close_limit_px=round(float(limit), 2),
                        close_qty=close_qty)
    _oid = f" (id {res['order_id']})" if res["order_id"] else ""
    # Toast format: headline first, then ONE sentence per line (run_app renders
    # each following line as its own bullet).
    return {"ok": True,
            "msg": (f"✅ LIVE closing order sent to {mask}{_oid}\n"
                    f"Buying back {close_qty}{_of} contract(s).\n"
                    "It shows as closing until it fills.\n"
                    "Cancel it from this tab if you change your mind.\n"
                    "Verify at your broker.")}


def _cancel_close_order(scfg: dict, trade: dict) -> dict:
    """Cancel a working BUY_TO_CLOSE order and revert the trade to open.

    The buy-to-close never filled, so the position is still open — flip the
    tracker back to "open" and clear the closing fields. Returns {ok, msg}."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    last4 = (trade.get("account") or "")[-4:]
    res = trade_actions.cancel_order(client, trade.get("close_order_id"),
                                     last4 or None)
    if not res["ok"]:
        return {"ok": False, "msg": f"Cancel failed: {res['error']}"}
    trades_store.update(trade["id"], status="open",
                        close_order_id=None, close_limit_px=None)
    return {"ok": True,
            "msg": "✅ Closing order canceled — the position is open again."}


# ── Close Options section (live Schwab) — its own "Close" tab ─────────────────

# Cached (60s) read-only reader for every live option leg — it lives in
# positions_cache so the Roll tab and the ⚙️ Settings dialog share one cache and
# one Schwab round-trip. Aliased here so existing `_option_positions(...)` calls
# and `_option_positions.clear()` keep working (clearing the alias clears the
# shared cache, which is what 🔄 should do).
_option_positions = positions_cache.option_positions


def _tracked_open_leg(underlying: str, option_type: str, strike: float,
                      expiration: str) -> dict | None:
    """The still-open/closing tracked trade matching a held leg, or None — so a
    Schwab position can be tagged 'tracked' and its close routed through the
    tracked flow instead of a standalone order (no tracker desync)."""
    for t in trades_store.load():
        if t.get("status") not in ("open", "closing"):
            continue
        if (str(t.get("ticker", "")).upper() == str(underlying).upper()
                and str(t.get("option_type", "P")).upper()
                    == str(option_type).upper()
                and str(t.get("expiration", "")) == str(expiration)
                and abs(float(t.get("strike", 0) or 0) - float(strike)) < 1e-6):
            return t
    return None


def _submit_position_close(scfg: dict, pos: dict, limit: float,
                           close_qty: int) -> dict:
    """Submit a LIVE close for an *untracked* Schwab option leg. Short →
    BUY_TO_CLOSE, long → SELL_TO_CLOSE (per pos['direction']). Returns {ok, msg}.

    For a SHORT leg the close is also logged to the Trades store as a "closing"
    record (marked ``opened_from="schwab_position"``) so it shows in the Trades
    section and finalizes to "closed" with realized P/L once the buy-to-close
    fills — using the broker's open average price as the credit. A LONG leg is
    NOT logged: the Trades P/L model is credit-received (short premium), so a
    long's realized P/L would come out inverted; its order still goes out."""
    from stocks_shared.schwab_live import get_client
    qty = int(pos.get("quantity", 1))
    close_qty = max(1, min(int(close_qty), qty))
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    resolved = trade_actions.resolve_account_hash(client, None)
    if not resolved:
        return {"ok": False,
                "msg": "Couldn't resolve the account — close NOT sent."}
    account_hash, mask = resolved
    direction = pos.get("direction", "short")
    res = trade_actions.place_option_close_order(
        client, ticker=pos.get("underlying", ""),
        strike=float(pos.get("strike", 0)), expiration=pos.get("expiration", ""),
        limit=float(limit), quantity=close_qty, account_hash=account_hash,
        option_type=pos.get("option_type", "P"), direction=direction)
    if not res["ok"]:
        return {"ok": False, "msg": f"Close rejected: {res['error']}"}
    _action = "SELL TO CLOSE" if direction == "long" else "BUY TO CLOSE"
    _of = f" of {qty}" if close_qty < qty else ""
    _oid = f" (id {res['order_id']})" if res["order_id"] else ""
    if direction != "long":
        # Log as a "closing" record so the Trades section polls the order and
        # finalizes it to "closed" (with realized P/L) on fill. `opened_from`
        # marks it broker-sourced so an UNFILLED close is dropped, not adopted
        # as a phantom open trade (the leg lives at the broker, not the app).
        trades_store.add({
            "ticker": pos.get("underlying", ""),
            "strike": float(pos.get("strike", 0)),
            "expiration": pos.get("expiration", ""),
            "option_type": pos.get("option_type", "P"),
            "quantity": close_qty,
            "credit": round(float(pos.get("avg_price", 0)), 2),
            "status": "closing",
            "close_order_id": res["order_id"],
            "close_limit_px": round(float(limit), 2),
            "close_qty": close_qty,
            "account": mask,
            "paper": False,
            "opened_from": "schwab_position",
        })
        _extra = "It appears on the Trades tab and finalizes once filled."
    else:
        _extra = "Long closes aren't logged to the Trades tab."
    # Toast format: headline first, then ONE sentence per line (run_app renders
    # each following line as its own bullet).
    return {"ok": True,
            "msg": (f"✅ LIVE {_action} sent to {mask}{_oid}\n"
                    f"Closing {close_qty}{_of} contract(s).\n"
                    f"{_extra}\n"
                    "Verify at your broker.")}


def _scroll_into_view() -> None:
    """Scroll the just-opened close builder into view after a position select.
    Same technique as the Roll tab: Streamlit strips injected anchor ids, so the
    component scrolls its own iframe (window.frameElement) into view."""
    st.iframe(
        """
        <script>
        (function() {
          const el = window.frameElement;
          if (!el) return;
          window.parent.setTimeout(function() {
            el.style.scrollMarginTop = "100px";
            el.scrollIntoView({behavior: "smooth", block: "start"});
          }, 150);
        })();
        </script>
        """,
        height=1, width=1,
    )


def _render_option_close(pos: dict, scfg: dict, market_open,
                         config_paper: bool, spot: float | None = None) -> None:
    """Close builder for one selected Schwab option leg: re-quote, editable
    limit + contracts (partial close), LIVE + market-hours gate, 2-step confirm.
    A tracked leg routes through _submit_close (so it shows 'closing' up top); an
    untracked leg uses _submit_position_close.

    `spot` is the underlying's live price, passed down from the caller's already-
    fetched spot map (None when unavailable) so the quote line can lead with it
    without a second fetch."""
    tkr = str(pos.get("underlying", ""))
    opt = str(pos.get("option_type", "P"))
    strike = float(pos.get("strike", 0))
    exp = str(pos.get("expiration", ""))
    qty = int(pos.get("quantity", 1))
    direction = pos.get("direction", "short")
    is_long = direction == "long"
    _word = "SELL TO CLOSE" if is_long else "BUY TO CLOSE"
    _right = "Call" if opt == "C" else "Put"
    tracked = _tracked_open_leg(tkr, opt, strike, exp)
    posid = f"{tkr}_{opt}_{strike:g}_{exp}"

    st.markdown(f"**Close {tkr} ${strike:g} {_right} {exp}** — {direction} "
                f"×{qty}" + ("  ·  tracked on the Trades tab" if tracked
                             else ""))

    # A working close already exists for this leg → don't offer a second one
    # (that would place a duplicate order); point to where it's tracked. The
    # status was re-checked against the broker on tab load
    # (_reconcile_closing_orders), so a filled/expired order has already been
    # settled — but say so, since an unreachable Schwab leaves it unverified and
    # the user would otherwise have no way to tell a stale block from a real one.
    if tracked and tracked.get("status") == "closing":
        st.info("⏳ A closing order is already working for this position — "
                "track it on the **Trades** tab. If you expect it to have "
                "filled or expired, hit 🔄 above to re-check with Schwab.")
        return

    # Live-only: these are real broker positions.
    if config_paper:
        st.warning("🔴 These are **live** broker positions. Closing needs "
                   "`paper = false` in config.toml (then restart). In paper "
                   "mode this section is view-only.")
        return

    # Re-quote for a suggested limit (reuses the tracked-trade cache).
    q = _close_quote(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                     scfg.get("callback_url", ""), scfg.get("token_file", ""),
                     tkr, exp, strike, opt)
    mid = q.get("mid") if q else None
    default_lim = trade_actions.round_to_tick(mid) if mid else 0.05
    _seed_key, _wid_key = f"opt_close_seed_{posid}", f"opt_close_limit_{posid}"
    if st.session_state.get(_seed_key) != default_lim:
        st.session_state[_wid_key] = float(default_lim)
        st.session_state[_seed_key] = default_lim

    # No min_value/max_value on either input: Streamlit won't commit an
    # out-of-range entry — it keeps the last valid value and shows its own
    # message — which would arm Place for a number the user never typed. Both
    # are validated by trade_actions.close_input_error below.
    _il, _iq, _ = st.columns([1.3, 1, 2], vertical_alignment="center")
    with _il:
        limit = st.number_input(
            f"{_word} limit ($/share)",
            step=float(trade_actions.tick_for(default_lim)), format="%.2f",
            key=_wid_key)
    # Re-seeded only when the held size changes — clamping on every rerun would
    # swallow an over-max entry before it could be reported (see reseed_on_change).
    _qk = f"opt_close_qty_{posid}"
    confirm_gate.reseed_on_change(_qk, f"opt_close_qty_seed_{posid}", qty)
    with _iq:
        # Left uncast — an emptied box returns None, and int(None) would raise
        # before the validity check below can report it.
        close_n = st.number_input(
            f"Contracts (of {qty})", step=1,
            format="%d", key=_qk, disabled=(qty == 1))
    if q:
        # Bold + full-strength (st.markdown, not the muted st.caption) so the
        # live quote reads at a glance while setting the limit. Spot leads; the
        # last print trails with its ET timestamp (regular weight — it's
        # metadata) so a stale print is obvious. Unavailable segments are
        # dropped rather than shown as a dash, keeping the line short.
        _qp = []
        if spot is not None and spot == spot and float(spot) > 0:  # not NaN
            _qp.append(f"**Spot \\${float(spot):,.2f}**")
        _qp += [f"**Bid \\${q.get('bid', 0):.2f}**",
                f"**Ask \\${q.get('ask', 0):.2f}**",
                f"**Mid \\${(q.get('mid') or 0):.2f}**"]
        _last_px = float(q.get("last") or 0)
        if _last_px > 0:
            _lt = trade_actions.fmt_last_trade_et(q.get("last_trade_ms"))
            _qp.append(f"**Last \\${_last_px:.2f}**"
                       + (f" ({_lt})" if _lt else ""))
        st.markdown("  ·  ".join(_qp))
    else:
        st.caption("Re-quote unavailable — set your own limit.")

    _confirm_key = f"opt_close_confirm_{posid}"
    _result_key = f"opt_close_result_{posid}"
    _result = st.session_state.get(_result_key)
    _val_keys = (_wid_key, _qk)
    _input_err = trade_actions.close_input_error(limit, close_n, qty)
    _valid = _input_err is None
    if _valid:
        limit, close_n = float(limit), int(close_n)
    else:
        st.error(_input_err)
    _blocked = (None if market_open is True else
                ("Equity options trade 9:30–16:00 ET, Mon–Fri."
                 if market_open is False else "Can't confirm market hours."))
    # Two-step, matching the tracked-trade close: "Confirm Close" arms the
    # confirm panel below; the actual LIVE order is the "Place Close" button
    # there. Nothing is sent on this first click, and editing the limit or the
    # contract count afterwards disarms it (see confirm_gate).
    _armed = confirm_gate.armed(_confirm_key, _val_keys, valid=_valid)

    def _close_error(limit_v, n_v):
        return trade_actions.close_input_error(limit_v, n_v, qty)

    _bc, _ = st.columns([2, 3])
    with _bc:
        # An invalid limit/size does NOT disable this button — it stays clickable
        # so a correction can be confirmed in one click (the click commits the
        # field, then the callback re-validates). Only the market-hours gate,
        # which editing can't fix, disables it.
        if _blocked:
            st.button("Confirm Close · 🔴 LIVE", disabled=True,
                      key=f"opt_close_btn_{posid}", help=_blocked,
                      width="stretch", type="primary")
            if market_open is False:
                st.caption("⏸ Market closed")
        elif _armed:
            st.button("Confirm Close · 🔴 LIVE", disabled=True,
                      key=f"opt_close_btn_{posid}", type="primary",
                      help=confirm_gate.ARMED_HELP, width="stretch")
        else:
            st.button("Confirm Close · 🔴 LIVE", key=f"opt_close_btn_{posid}",
                      type="primary", width="stretch",
                      on_click=confirm_gate.arm(_confirm_key, _val_keys,
                                                clear_keys=(_result_key,),
                                                validate=_close_error))

    if _armed:
        _val = limit * 100 * close_n
        _verb = "credit" if is_long else "debit"
        _of = f" of {qty}" if close_n < qty else ""
        st.warning((f"**Confirm** — {_word} {close_n}{_of} {tkr} ${strike:g} "
                    f"{_right.upper()} @ ${limit:.2f} ({_verb} "
                    f"**${_val:,.0f}**) · 🔴 **LIVE**").replace("$", "\\$"))

        _cc1, _cc2, _ = st.columns([1, 1, 3])
        with _cc1:
            _do = st.button("Place Close · 🔴 LIVE",
                            key=f"opt_close_do_{posid}", type="primary",
                            width="stretch")
        with _cc2:
            _cbox = st.container(key=f"opt_close_cxlbox_{posid}")
            _cbox.button("Cancel", key=f"opt_close_cxl_{posid}",
                         width="stretch",
                         on_click=confirm_gate.disarm(_confirm_key))
        if _do:
            _result = (_submit_close(scfg, tracked, limit, True, close_n)
                       if tracked else
                       _submit_position_close(scfg, pos, limit, close_n))
            st.session_state[_result_key] = _result
            st.session_state[_confirm_key] = False
            if _result.get("ok"):
                # Confirm via the center banner (like the Sell Put / Roll
                # dialogs) rather than the inline st.success below: this panel
                # renders only while a position row is selected, and a close
                # drops that position out of the refetched list — so the inline
                # message had nowhere to render and the confirmation was never
                # seen. Drop the stored result so it can't double-render.
                st.session_state["_osc_toast"] = _result["msg"]
                st.session_state.pop(_result_key, None)
                _option_positions.clear()
                _close_quote.clear()
                _order_status.clear()
            # Rerun either way: disarming above only lands on the NEXT run, so
            # without this the panel the click came from stays up with Place
            # Close still live. On a failure the stored result re-renders as the
            # inline error below, one Confirm away from a retry.
            st.rerun()
    # Failures stay inline, next to the button that produced them (a success
    # leaves via the banner above).
    if _result and not _result.get("ok"):
        st.error(_result["msg"].replace("$", "\\$"))


def _pos_pkey(p: dict) -> tuple:
    """Stable identity for an option leg — keys the prefetched-quote map."""
    return (p.get("underlying", ""), p.get("option_type", "P"),
            float(p.get("strike", 0)), p.get("expiration", ""))


def _prefetch_position_quotes(positions: list, scfg: dict) -> dict:
    """Parallel per-leg re-quotes → {pkey: quote|None}, so the positions table
    shows each leg's live delta without N sequential round-trips. Reuses the
    cached `_close_quote`; a slow/hung leg leaves that entry None (delta blank)
    rather than blocking the tab. Mirrors the Roll tab's _prefetch_quotes — can't
    import it (rolls imports from this module → circular)."""
    if not positions:
        return {}
    ak, sk = scfg.get("app_key", ""), scfg.get("app_secret", "")
    cb, tf = scfg.get("callback_url", ""), scfg.get("token_file", "")

    def _job(p):
        return _close_quote(ak, sk, cb, tf, p.get("underlying", ""),
                            p.get("expiration", ""), float(p.get("strike", 0)),
                            p.get("option_type", "P"))

    out: dict = {}
    ctx = get_script_run_ctx()
    init = (functools.partial(add_script_run_ctx, ctx=ctx)
            if add_script_run_ctx is not None else None)
    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(positions)), initializer=init)
    futs = {ex.submit(_job, p): _pos_pkey(p) for p in positions}
    deadline = time.monotonic() + _TRADES_FETCH_TIMEOUT_S
    for fut, k in futs.items():
        try:
            out[k] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            out[k] = None
    ex.shutdown(wait=False, cancel_futures=True)
    return out


def _sign_color(v) -> str:
    """Green for positive, red for negative — colors the Spot/Day% cell.
    st.dataframe honors a pandas Styler's concrete-hex color (CSS vars don't
    resolve in the grid renderer)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f > 0:
        return "color: #16a34a"
    if f < 0:
        return "color: #dc2626"
    return ""


@st.fragment
def _render_option_positions(scfg: dict, provider: str, market_open) -> None:
    """The 'Close Options (Schwab)' section (the "Close" tab): list every live
    option leg (with live delta, remaining-time-value Ann%, and the underlying's
    spot / day change) and let the user close all or part of one."""
    import pandas as pd
    from options_scanner.display.spot_meta import fetch_spot_meta

    _hdr, _rf = st.columns([8, 1], vertical_alignment="bottom")
    with _hdr:
        section_header(title="Close Options (Schwab)")
    with _rf:
        if st.button("🔄", key="opt_pos_refresh",
                     help="Re-fetch positions, quotes, order status, and spot."):
            _option_positions.clear()
            _close_quote.clear()
            _order_status.clear()
            fetch_spot_meta.clear()

    if provider != "schwab" or not scfg.get("app_key"):
        st.info("Connect Schwab and select it as the data source (top bar) to "
                "see and manage your live option positions here.")
        return
    # Settle any closing order that already resolved at the broker BEFORE the
    # per-leg "a close is already working" guard reads the store — otherwise an
    # order that expired overnight keeps blocking a fresh close.
    _reconcile_closing_orders(scfg)
    _render_reconcile_notes()
    positions = _option_positions(scfg.get("app_key", ""),
                                  scfg.get("app_secret", ""),
                                  scfg.get("callback_url", ""),
                                  scfg.get("token_file", ""))
    if positions is None:
        st.warning("Couldn't reach Schwab — your token may have expired. "
                   "Re-run `schwab_auth.py`, then hit 🔄.")
        return
    if not positions:
        st.caption("No option positions in your Schwab account.")
        return
    # Hidden-position blacklist (⚙️ Settings), applied here rather than inside
    # the cached reader: the cache keeps the account's truth, and a settings
    # change lands on the next rerun instead of waiting out the 60s TTL.
    _held_n = len(positions)
    positions, _hidden = settings_ui.filter_hidden(positions, scope="close")
    if not positions:
        st.caption(f"All {_held_n} of your option positions are hidden by your "
                   f"⚙️ Settings — nothing to show.")
        # Still render the notice: it carries the toggle that reveals them.
        settings_ui.render_hidden_notice(_hidden, scope="close")
        return

    config_paper = bool(scfg.get("paper", True))
    positions = sorted(positions, key=lambda p: (str(p.get("underlying", "")),
                                                 str(p.get("expiration", "")),
                                                 float(p.get("strike", 0))))
    _today = datetime.now().date()

    def _dte(e):
        try:
            return (datetime.strptime(e, "%Y-%m-%d").date() - _today).days
        except Exception:
            return None

    # Live delta per leg (parallel) + one spot/day fetch per unique underlying.
    quotes = _prefetch_position_quotes(positions, scfg)
    meta: dict = {}
    for _tk in {str(p.get("underlying", "")) for p in positions}:
        try:
            meta[_tk] = fetch_spot_meta(_tk, provider)
        except Exception:
            meta[_tk] = {}

    def _spot_day(tk):
        m = meta.get(tk) or {}
        spot, pct = m.get("spot"), m.get("pct_change")
        if not (spot is not None and spot == spot and float(spot) > 0):  # NaN
            return "—", None
        s = f"${float(spot):,.2f}"
        if pct is not None:
            s += f"  {float(pct):+.1f}%"
        return s, (float(pct) if pct is not None else None)

    rows = []
    for p in positions:
        _opt = p.get("option_type", "P")
        _dir = str(p.get("direction", "")).lower()
        _tkr = str(p.get("underlying", ""))
        _strike = float(p.get("strike", 0))
        _qty = int(p.get("quantity", 1))
        _mv = float(p.get("market_value", 0) or 0)
        _mark = abs(_mv) / (100 * _qty) if _qty else None
        _dte_v = _dte(p.get("expiration", ""))
        _spot_v = (meta.get(_tkr) or {}).get("spot")
        _spot = (float(_spot_v) if (_spot_v is not None and _spot_v == _spot_v
                                    and float(_spot_v) > 0) else None)
        # Annualized % of the remaining time value (extrinsic): time value =
        # mark − intrinsic; annualized against strike (puts) or spot (calls),
        # matching the scanner's Ann% convention.
        _ann = None
        if _mark is not None and _spot is not None and _dte_v and _dte_v > 0:
            _intrinsic = (max(0.0, _spot - _strike) if _opt == "C"
                          else max(0.0, _strike - _spot))
            _tv = max(0.0, _mark - _intrinsic)
            _base = _spot if _opt == "C" else _strike
            if _base > 0:
                _ann = _tv / _base * (365.0 / _dte_v) * 100.0
        _delta = (quotes.get(_pos_pkey(p)) or {}).get("delta")
        _tracked = _tracked_open_leg(_tkr, _opt, _strike,
                                     p.get("expiration", "")) is not None
        _tags = ([("covered" if p.get("covered") else "naked")]
                 if _opt == "C" and _dir == "short" else [])
        if _tracked:
            _tags.append("tracked")
        _spotday, _pct = _spot_day(_tkr)
        rows.append({
            "Ticker": _tkr,
            "Spot/Day%": _spotday,
            "_pct": _pct,
            "Type": f"{'Call' if _opt == 'C' else 'Put'} - "
                    f"{'Short' if _dir == 'short' else 'Long'}",
            "Strike": _strike,
            "Exp": p.get("expiration", ""),
            "DTE": _dte_v,
            "Qty": _qty,
            "Delta": (round(_delta, 2) if _delta is not None else None),
            "Ann%": (round(_ann, 1) if _ann is not None else None),
            "Avg": round(float(p.get("avg_price", 0)), 2),
            "Mkt Val": round(_mv, 0),
            "Note": " · ".join(_tags),
        })
    disp = pd.DataFrame(rows)
    st.caption("🔍 **Select a position** to close all or part of it. To roll an "
               "existing position instead, use the **Roll** tab.")
    st.caption("view-only in paper mode." if config_paper
               else "**🔴 LIVE** (buy-to-close for short legs, sell-to-close "
                    "for long).")
    styled = disp.style.apply(
        lambda s: [_sign_color(disp.loc[i, "_pct"]) for i in s.index],
        subset=["Spot/Day%"])
    event = st.dataframe(
        styled, hide_index=True, width="stretch", on_select="rerun",
        selection_mode="single-row", key="opt_pos_table",
        height=df_height(styled),
        column_config={
            "Spot/Day%": st.column_config.TextColumn(
                "Spot/Day%", width=130,
                help="Underlying spot and today's change (green up / red down)."),
            "_pct": None,
            "Strike": st.column_config.NumberColumn("Strike", format="$%.2f"),
            "Delta": st.column_config.NumberColumn("Delta", format="%.2f"),
            "Ann%": st.column_config.NumberColumn(
                "Ann%", format="%.1f%%",
                help="Annualized return on the option's remaining time value "
                     "(extrinsic) — puts vs strike, calls vs spot. Low = little "
                     "premium left to decay, a cue to close."),
            "Avg": st.column_config.NumberColumn("Avg", format="$%.2f"),
            "Mkt Val": st.column_config.NumberColumn("Mkt Val", format="$%.0f"),
            "DTE": st.column_config.NumberColumn("DTE", format="%d"),
        })
    sel = event.selection.rows if hasattr(event, "selection") else []
    if sel:
        if st.session_state.get("_opt_pos_scroll") != sel[0]:
            st.session_state["_opt_pos_scroll"] = sel[0]
            _scroll_into_view()
        st.markdown("---")
        # Reuse the spot already fetched for the table above (same cached
        # source), so the close panel's quote line can't disagree with the row.
        _sel_pos = positions[sel[0]]
        _sel_spot = (meta.get(str(_sel_pos.get("underlying", ""))) or {}).get(
            "spot")
        _render_option_close(_sel_pos, scfg, market_open, config_paper,
                             _sel_spot)
        st.markdown("---")
    else:
        st.session_state.pop("_opt_pos_scroll", None)

    # Hidden-position note last, so it never pushes the table down.
    settings_ui.render_hidden_notice(_hidden, scope="close")


def _trades_context() -> tuple:
    """Per-render context shared by the Trades and Close tabs:
    (provider, scfg, market_open). market_open (None = unknown → fail safe) gates
    live closing; it's one cached (60s) Schwab read."""
    provider = st.session_state.get("data_source", "yahoo")
    scfg = st.session_state.get("schwab_config") or {}
    market_open = (_market_open(scfg.get("app_key", ""),
                                scfg.get("app_secret", ""),
                                scfg.get("callback_url", ""),
                                scfg.get("token_file", ""))
                   if (provider == "schwab" and scfg.get("app_key")) else None)
    return provider, scfg, market_open


def tab_trades() -> None:
    # The "Trades" tab: the scanner-trades list only. Managing live Schwab option
    # positions moved to its own "Close" tab (tab_close). _scanner_trades is a
    # fragment, so a trade-row toggle or the 🔄 reruns just this list, not the app.
    provider, scfg, market_open = _trades_context()
    _scanner_trades(provider, scfg, market_open)


def tab_close() -> None:
    # The "Close" tab: the Close Options (Schwab) section — list live option legs
    # and close all or part of one. _render_option_positions is a fragment, so
    # its 🔄 and row-select rerun just this section.
    provider, scfg, market_open = _trades_context()
    _render_option_positions(scfg, provider, market_open)


@st.fragment
def _scanner_trades(provider: str, scfg: dict, market_open) -> None:
    # A fragment so trade-row toggles and the 🔄 rerun just this list. The toggle
    # uses st.rerun(scope="fragment") (legal — we're always inside this fragment);
    # explicit Cancel / Remove / close actions keep a full st.rerun(), and run_app
    # preserves the active tab across it.
    # Current order mode (config `paper` flag) as a badge beside the Trades
    # title, so it's obvious at a glance — on first load too — whether placing
    # or closing a trade goes live or is simulated.
    _paper_mode = bool(scfg.get("paper", True))
    _mode_badge = (
        " <span style='font-size:0.5em;font-weight:700;vertical-align:middle;"
        "margin-left:6px;padding:2px 9px;border-radius:7px;"
        + ("background:#334155;color:#cbd5e1;'>📝 PAPER</span>"
           if _paper_mode else
           "background:#b91c1c;color:#fff;'>🔴 LIVE</span>"))

    # Settle any closing order that already resolved at the broker before the
    # list is read, so a trade whose close expired overnight shows as open again
    # on arrival — the per-row Schwab reads below are deferred behind row
    # expansion, so without this it stayed "closing" until the user drilled in.
    _reconcile_closing_orders(scfg)

    # "rolling" trades are in-flight net-price rolls managed on the Roll tab —
    # they surface here as normal open positions only once they fill.
    trades = [t for t in trades_store.load() if t.get("status") != "rolling"]
    if not trades:
        section_header(title=f"Trades made from Scanner{_mode_badge}")
        st.info(
            "No trades yet. Put-sells you place from the **Watchlist** "
            "leaderboard's *Sell Put* dialog appear here with live P/L and a "
            "cost-to-close estimate, and can be closed from here."
        )
        return

    # Title (with a small, muted trade count) + a compact 🔄 refresh on one
    # row. Refresh clears the cached status/quote/spot fetches so this rerun
    # (the button click reruns the fragment) re-pulls them — e.g. to catch a
    # fill — instead of taking its own row.
    _open_n = sum(1 for t in trades if t.get("status") == "open")
    _count = (f" <span style='font-size:0.5em;color:#94a3b8;font-weight:500;"
              f"vertical-align:middle;'>{len(trades)} trade(s) · {_open_n} "
              f"open</span>")
    _th, _tr = st.columns([8, 1], vertical_alignment="bottom")
    with _th:
        section_header(title=f"Trades made from Scanner{_mode_badge}{_count}")
    with _tr:
        if st.button("🔄", key="trades_refresh",
                     help="Re-fetch order status, quotes, and spot."):
            _order_status.clear()
            _close_quote.clear()
            from options_scanner.display.spot_meta import fetch_spot_meta
            fetch_spot_meta.clear()

    # Notes from the reconciliation above (e.g. "closing order EXPIRED without
    # filling"), shown once at the top — the record may have been removed, so a
    # per-row note could have nowhere to land.
    _render_reconcile_notes()

    # Per-trade styling, scoped to keyed containers: a stronger border on the
    # close-limit field (like the Sell Put dialog), and red Remove buttons
    # (Remove discards the tracked record). Primary buttons (Place Closing
    # Trade) already pick up the accent color from run_app's button CSS.
    st.markdown(
        "<style>"
        "[class*='st-key-close_box_'] div[data-baseweb='input']{"
        "border:2px solid #8a8f9c !important;border-radius:0.5rem;}"
        "[class*='st-key-rm_box_'] button{background-color:#d9534f !important;"
        "border-color:#d9534f !important;color:#fff !important;}"
        "[class*='st-key-rm_box_'] button:hover{background-color:#c9302c "
        "!important;border-color:#c9302c !important;}"
        "[class*='st-key-rm_box_'] button p{color:#fff !important;}"
        # Lazy-row header buttons: left-aligned, full-width, subtle border —
        # reads like the expander rows they replace, not a primary button.
        "[class*='st-key-trade_hdr_'] button{background:transparent "
        "!important;border:1px solid rgba(148,163,184,0.25) !important;}"
        "[class*='st-key-trade_hdr_'] button:hover{"
        "background:rgba(148,163,184,0.08) !important;"
        "border-color:rgba(148,163,184,0.45) !important;}"
        # Pin the header text to the plain ink color, in every state. The theme's
        # generic `.stButton > button:hover {color: var(--osc-primary)}` repaints
        # ALL of a button's text on hover, which turned a whole header row —
        # ticker, strike, expiry, size, status — accent-colored just from mousing
        # over it. On this row color is reserved for one meaning: whether the
        # underlying is up or down today. These rules don't touch the spot
        # segment's own <span>, so its green/red survives.
        "[class*='st-key-trade_hdr_'] button,"
        "[class*='st-key-trade_hdr_'] button:hover,"
        "[class*='st-key-trade_hdr_'] button:active,"
        "[class*='st-key-trade_hdr_'] button:focus{"
        "color:var(--osc-ink-2) !important;}"
        "[class*='st-key-trade_hdr_'] button p,"
        "[class*='st-key-trade_hdr_'] button:hover p{"
        "color:var(--osc-ink-2) !important;}"
        # Quick-remove 🗑 at the right end of each header row: borderless and
        # muted so it reads as an icon rather than a second button competing with
        # the row toggle, and unmistakably destructive (red tint) on hover.
        "[class*='st-key-rm_quick_'] button{background:transparent !important;"
        "border:1px solid transparent !important;opacity:0.55;"
        "padding-left:0 !important;padding-right:0 !important;}"
        "[class*='st-key-rm_quick_'] button:hover{opacity:1;"
        "background:rgba(217,83,79,0.12) !important;"
        "border-color:rgba(217,83,79,0.45) !important;}"
        # The flex that centers the label sits on the button AND its inner
        # content wrapper (ButtonContentWithIcon) — push BOTH to flex-start, and
        # let the label's markdown container fill the row so text-align:left
        # actually bites. Targeting only the <button> left the inner div
        # centering the text.
        "[class*='st-key-trade_hdr_'] button,"
        "[class*='st-key-trade_hdr_'] button>div{justify-content:flex-start "
        "!important;text-align:left !important;}"
        "[class*='st-key-trade_hdr_'] button div[data-testid="
        "'stMarkdownContainer']{width:100% !important;text-align:left "
        "!important;}"
        "[class*='st-key-trade_hdr_'] button p{width:100% !important;"
        "text-align:left !important;font-weight:600 !important;}"
        "</style>",
        unsafe_allow_html=True,
    )

    # Prefetch every trade's Schwab reads (order status + cost-to-close
    # re-quote) concurrently, so the tab costs ~one round-trip instead of two
    # per trade in series. Bounded by _TRADES_FETCH_TIMEOUT_S: a slow/hung call
    # leaves that entry None (rendered "unavailable") rather than blocking the
    # whole tab. The render loop reads these maps instead of fetching inline.
    status_by_id: dict = {}
    close_status_by_id: dict = {}
    quote_by_id: dict = {}
    chart_by_ticker: dict = {}   # ticker -> intraday series for the day chart
    spot_by_ticker: dict = {}    # ticker -> {spot, pct_change} for the headers
    if provider == "schwab" and scfg.get("app_key"):
        _ak, _as = scfg.get("app_key", ""), scfg.get("app_secret", "")
        _cb, _tf = scfg.get("callback_url", ""), scfg.get("token_file", "")

        def _status_job(tr):
            return _order_status(_ak, _as, _cb, _tf, tr.get("order_id"),
                                 (tr.get("account") or "")[-4:])

        def _close_status_job(tr):
            return _order_status(_ak, _as, _cb, _tf, tr.get("close_order_id"),
                                 (tr.get("account") or "")[-4:])

        def _quote_job(tr):
            return _close_quote(_ak, _as, _cb, _tf, tr.get("ticker"),
                                tr.get("expiration", ""),
                                float(tr.get("strike", 0)),
                                tr.get("option_type", "P"))

        def _chart_job(tr):
            return _intraday(_ak, _as, _cb, _tf, tr.get("ticker"))

        def _spot_job(tr):
            from options_scanner.display.spot_meta import fetch_spot_meta
            try:
                return fetch_spot_meta(str(tr.get("ticker", "")), provider)
            except Exception:
                return None

        jobs = []  # (kind, key, trade) — key is trade_id, or ticker for charts
        _chart_seen = set()
        _spot_seen = set()
        for tr in trades:
            # Spot + day change for the COLLAPSED header, so an open position
            # shows its underlying's price without being expanded. The one read
            # here that ISN'T deferred — the header is on screen before any row
            # opens — but it's one cached fetch (60s TTL) per unique underlying,
            # in this same parallel batch, and only for a position that's still
            # open. A closed record's exposure is gone, so today's quote would be
            # noise; it isn't fetched and the header omits the segment.
            _stk = tr.get("ticker")
            if (_stk and _stk not in _spot_seen
                    and tr.get("status") in ("open", "closing")):
                _spot_seen.add(_stk)
                jobs.append(("spot", _stk, tr))
            # Deferred load: a collapsed trade fetches nothing else. Its Schwab
            # reads run only once the user opens its row (the toggle header below
            # sets this flag, then reruns so this loop picks the trade up). Tab
            # load starts with every row collapsed → no per-trade round-trips.
            if not st.session_state.get(f"trade_open_{tr.get('id')}", False):
                continue
            # Opening-order status only matters while the trade is still open;
            # a working closing order is polled via close_order_id instead.
            if (not tr.get("paper") and tr.get("order_id")
                    and tr.get("status") == "open"):
                jobs.append(("status", tr.get("id"), tr))
            if (not tr.get("paper") and tr.get("close_order_id")
                    and tr.get("status") == "closing"):
                jobs.append(("close_status", tr.get("id"), tr))
            jobs.append(("quote", tr.get("id"), tr))
            # One intraday fetch per unique ticker (keyed by ticker, deduped).
            _ctk = tr.get("ticker")
            if _ctk and _ctk not in _chart_seen:
                _chart_seen.add(_ctk)
                jobs.append(("chart", _ctk, tr))

        if jobs:
            # Attach this run's context to the worker threads so the cached
            # fetch helpers don't emit "missing ScriptRunContext" warnings.
            _ctx = get_script_run_ctx()
            _init = (functools.partial(add_script_run_ctx, ctx=_ctx)
                     if add_script_run_ctx is not None else None)
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(jobs)), initializer=_init)
            _job_fns = {"status": _status_job,
                        "close_status": _close_status_job,
                        "quote": _quote_job,
                        "chart": _chart_job,
                        "spot": _spot_job}
            _job_maps = {"status": status_by_id,
                         "close_status": close_status_by_id,
                         "quote": quote_by_id,
                         "chart": chart_by_ticker,
                         "spot": spot_by_ticker}
            fut_map = {}
            for kind, tid, tr in jobs:
                fut_map[ex.submit(_job_fns[kind], tr)] = (kind, tid)
            deadline = time.monotonic() + _TRADES_FETCH_TIMEOUT_S
            for fut, (kind, tid) in fut_map.items():
                try:
                    res = fut.result(
                        timeout=max(0.0, deadline - time.monotonic()))
                except Exception:
                    res = None
                _job_maps[kind][tid] = res
            # Don't block on stragglers — the client's HTTP timeout reaps them.
            ex.shutdown(wait=False, cancel_futures=True)

    for t in trades:
        exp = t.get("expiration", "")
        try:
            exp_disp = datetime.strptime(exp, "%Y-%m-%d").strftime("%b %d '%y")
        except Exception:
            exp_disp = exp or "?"
        qty = int(t.get("quantity", 1))
        credit_ps = float(t.get("credit", 0))          # per share
        total_credit = credit_ps * 100 * qty

        # Broker fill state for the title: the store status stays "open" from
        # acceptance until close/cancel, so reflect the live order status
        # (filled / working / …) when we have it. Fetched in the parallel
        # prefetch above; None when not applicable or the read timed out.
        bs = status_by_id.get(t.get("id"))
        # Opening-order fill state (bs = the opening order's broker status).
        # Computed here so both the cards and the action branches below can use
        # them. A still-working opening order isn't a position yet, so its cards
        # show the pending order rather than P/L (see _render_pending_cards).
        working = bool(bs and bs.get("cancelable"))
        filled = bool(bs and bs.get("status") == "FILLED")
        is_paper = bool(t.get("paper"))
        _store_status = t.get("status", "open")
        _pending_open = _store_status == "open" and working
        _disp_status = ((bs.get("status") or _store_status).lower()
                        if _store_status == "open" and bs else _store_status)
        _otw = "CALL" if t.get("option_type") == "C" else "PUT"
        # Underlying spot + today's change, just before the mode badge — which
        # stays the last element on every line. Absent for a closed record (no
        # live exposure, and no fetch was made) or when the quote didn't come
        # back, in which case the segment is simply dropped.
        _hmeta = spot_by_ticker.get(t.get("ticker")) or {}
        _spot_seg = _day_head_md(_hmeta.get("spot"), _hmeta.get("pct_change"))
        # The strike's "$" is escaped: two unescaped dollar signs in one markdown
        # string (strike + spot) get parsed as LaTeX math, which swallows them
        # and reflows the middle of the header into a serif math run.
        label = (f"{t.get('ticker', '?')} \\${t.get('strike', '?')} {_otw} — "
                 f"{exp_disp} · {qty}x · {_disp_status}"
                 + (f" · {_spot_seg}" if _spot_seg else "")
                 + ("  ·  📝 PAPER" if t.get("paper") else "  ·  🔴 LIVE"))

        # Lazy row (replaces st.expander): a collapsed trade renders only this
        # header and fetches nothing — its Schwab reads were skipped in the
        # prefetch above. Clicking toggles open/closed. An expander can't gate
        # the fetch because collapse/expand is client-side and never reruns the
        # script; a header button does, so on open the prefetch re-runs and
        # picks this trade up.
        _exp_key = f"trade_open_{t['id']}"
        is_open = st.session_state.get(_exp_key, False)
        _chev = "▼" if is_open else "▶"
        # Header row: the toggle spans the width, with a 🗑 at the far right (past
        # the PAPER/LIVE badge) so a record can be dropped without expanding it.
        # Same action as the in-row "Remove from Tracker" — it deletes the app's
        # record only and never touches a broker position.
        _hdr_c, _trash_c = st.columns([22, 1], vertical_alignment="center")
        with _hdr_c:
            if st.button(f"{_chev}  {label}", key=f"trade_hdr_{t['id']}",
                         width="stretch"):
                st.session_state[_exp_key] = not is_open
                # Fragment-scoped: opening a row reruns only this trades fragment
                # (so the prefetch picks the row up) and leaves the Close Options
                # fragment below untouched.
                st.rerun(scope="fragment")
        with _trash_c:
            if st.button("🗑", key=f"rm_quick_{t['id']}", width="stretch",
                         help=f"Remove {t.get('ticker', '?')} "
                              f"${t.get('strike', '?')} {_otw} from the tracker "
                              f"— deletes this record only; a broker position is "
                              f"untouched."):
                trades_store.remove(t["id"])
                # Full rerun (not scope="fragment"): the list has to be re-read
                # from the store, and the banner is rendered by run_app.
                st.session_state["_osc_toast"] = (
                    f"🗑 Removed {t.get('ticker', '?')} "
                    f"${t.get('strike', '?')} {_otw} from the tracker\n"
                    "This deleted the app's record only — any broker position is "
                    "untouched.")
                st.rerun()
        if is_open:
            # Live re-quote for cost-to-close (Schwab, read-only) — fetched in
            # the parallel prefetch above; None when unavailable or timed out.
            q = quote_by_id.get(t.get("id"))
            close_mid = q.get("mid") if q else None

            # Reconstruct the exact spot + delta at the fill the first time we
            # see the order filled: the underlying's 1-min bar at Schwab's fill
            # timestamp gives the spot, and the implied vol backed out of the
            # actual fill price gives a consistent delta. Persisted once, then
            # frozen; skipped silently when the fill predates available
            # intraday history.
            if (bs and bs.get("status") == "FILLED"
                    and t.get("fill_spot") is None
                    and bs.get("filled_at") is not None
                    and provider == "schwab" and scfg.get("app_key")):
                _snap = _fill_snapshot(
                    scfg.get("app_key", ""), scfg.get("app_secret", ""),
                    scfg.get("callback_url", ""), scfg.get("token_file", ""),
                    str(t.get("ticker", "")), exp, float(t.get("strike", 0)),
                    float(t.get("credit", 0)), bs["filled_at"].isoformat())
                if _snap and _snap.get("fill_spot") is not None:
                    trades_store.update(t["id"], **_snap)
                    t.update(_snap)

            # Contract snapshot (open positions) — two key/value tables, built
            # from the same re-quote `q` used for cost-to-close.
            _has_snapshot = bool(q and t.get("status") in ("open", "closing"))
            if _has_snapshot:
                _strike = float(t.get("strike", 0))
                _is_call = t.get("option_type") == "C"
                try:
                    _dte = (datetime.strptime(exp, "%Y-%m-%d").date()
                            - datetime.now().date()).days
                except Exception:
                    _dte = None
                _iv, _delta = q.get("iv"), q.get("delta")
                # Underlying spot + day-change % (one cached fetch_spot_meta
                # call) for the Spot row under Vol — and the Ann% base for a call.
                from options_scanner.display.spot_meta import fetch_spot_meta
                try:
                    _meta = fetch_spot_meta(str(t.get("ticker", "")), provider)
                except Exception:
                    _meta = {}
                _spot, _spct = _meta.get("spot"), _meta.get("pct_change")
                # Annualized yield: a covered call is measured against spot (the
                # shares are the capital); a cash-secured put against its strike
                # (the cash collateral). Mirrors the scanner's Ann% convention.
                _ann_base = _spot if _is_call else _strike
                _ann = (q["mid"] / _ann_base * (365.0 / _dte) * 100.0
                        if (_dte and _dte > 0 and _ann_base and q.get("mid"))
                        else None)

                # Delta cell: live value, plus the fill-time delta once captured.
                _delta_cell = f"{_delta:.2f}" if _delta is not None else "—"
                if t.get("fill_delta") is not None:
                    _delta_cell += ("<span style='color:#94a3b8'> · fill "
                                    f"{float(t['fill_delta']):.2f}</span>")
                # DTE cell: days remaining, plus how long the position has been
                # open — but only once it's a real (filled) position. A working
                # opening order isn't open yet, so don't tack "open 0d" onto it.
                _dte_cell = str(_dte) if _dte is not None else "—"
                _opened = t.get("opened_at")
                if _opened and not _pending_open:
                    try:
                        _days_open = (datetime.now()
                                      - datetime.fromisoformat(_opened)).days
                        _dte_cell += ("<span style='color:#94a3b8'> · open "
                                      f"{_days_open}d</span>")
                    except Exception:
                        pass
                _terms = [
                    ("Type", "Call" if _is_call else "Put"),
                    ("Strike", f"${_strike:g}"),
                    ("Expir", exp_disp),
                    ("DTE", _dte_cell),
                    ("IV", f"{_iv * 100:.1f}%" if _iv else "—"),
                    ("Delta", _delta_cell),
                    ("Ann%", f"{_ann:.1f}%" if _ann is not None else "—"),
                ]
                # Spot cell: live value, plus the fill-time spot once captured.
                # Colored by today's direction, same rule as the row header and
                # the chart caption — the same number shouldn't be green in one
                # place and plain in another. The fill-time suffix below stays
                # muted: it's history, not today's move.
                _sd = _fmt_spot_day(_spot, _spct)
                if _sd is None:
                    _spot_cell = "—"
                else:
                    _s_txt, _p_txt, _s_up = _sd
                    _spot_cell = f"{_s_txt}, {_p_txt}" if _p_txt else _s_txt
                    if _p_txt:
                        _spot_cell = (
                            f"<span style='color:"
                            f"{'#16a34a' if _s_up else '#dc2626'}'>"
                            f"{_spot_cell}</span>")
                if t.get("fill_spot") is not None:
                    _spot_cell += ("<span style='color:#94a3b8'> · fill "
                                   f"${float(t['fill_spot']):,.2f}</span>")
                # Last cell carries the print time (New York) on its own line
                # beneath the price when Schwab supplied it, so a stale last is
                # obvious while setting the close limit.
                _last_cell = f"${float(q.get('last', 0)):,.2f}"
                _lt = trade_actions.fmt_last_trade_et(q.get("last_trade_ms"))
                if _lt:
                    _last_cell += (f"<br><span style='color:#94a3b8'>{_lt}"
                                   "</span>")
                _prices = [
                    ("Bid", f"${float(q.get('bid', 0)):,.2f}"),
                    ("Ask", f"${float(q.get('ask', 0)):,.2f}"),
                    ("Mid", f"${float(q.get('mid', 0)):,.2f}"),
                    ("Last", _last_cell),
                    ("OI", f"{q.get('open_interest', 0):,}"),
                    ("Vol", f"{q.get('volume', 0):,}"),
                    ("Spot", _spot_cell),
                ]

            def _render_cards(cols):
                # A closed position shows the REALIZED close/P&L recorded at the
                # fill (`close_cost`), NOT the live re-quote mid — the position
                # is gone, and its mid keeps drifting with the market. An open
                # position uses the live mid as a cost-to-close estimate.
                _closed = t.get("status") in ("closed", "expired", "assigned")
                _cc = t.get("close_cost")
                _cost_ps = (float(_cc) if _cc is not None else None) if _closed \
                    else close_mid
                _cost_label = "CLOSE COST" if _closed else "COST TO CLOSE"
                _pl_kind = "REALIZED P/L" if _closed else "UNREALIZED P/L"
                with cols[0]:
                    metric_card("CREDIT RECEIVED", f"${total_credit:,.0f}",
                                delta=f"${credit_ps:.2f}/sh", delta_sign="neutral")
                with cols[1]:
                    if _cost_ps is not None:
                        metric_card(_cost_label,
                                    f"${_cost_ps * 100 * qty:,.0f}",
                                    delta=f"${_cost_ps:.2f}/sh",
                                    delta_sign="neutral")
                    else:
                        metric_card(_cost_label, "—",
                                    delta="re-quote unavailable",
                                    delta_sign="neutral")
                with cols[2]:
                    if _cost_ps is not None:
                        _close_cost = _cost_ps * 100 * qty
                        pnl = total_credit - _close_cost
                        # Formula on a small line just above the value: credit
                        # received − close cost.
                        _pl_label = (
                            f"{_pl_kind}<br><span style='font-weight:400;"
                            "text-transform:none;letter-spacing:0;"
                            "font-size:0.8em;color:#94a3b8;'>"
                            f"${total_credit:,.0f} − ${_close_cost:,.0f}</span>")
                        # Green when up, red when down — color the net amount.
                        _pl_color = ("var(--osc-success)" if pnl >= 0
                                     else "var(--osc-destructive)")
                        _pl_num = f"{'−' if pnl < 0 else ''}${abs(pnl):,.0f}"
                        metric_card(
                            _pl_label,
                            f"<span style='color:{_pl_color}'>{_pl_num}</span>")
                    else:
                        metric_card(_pl_kind, "—")
                with cols[3]:
                    metric_card("STATUS", _disp_status.upper())

            def _render_pending_cards(cols):
                # Working opening order: not a position yet, so show what the
                # order WOULD do on fill (credit collected, collateral tied up)
                # instead of cost-to-close / P/L, which don't apply until fill.
                # STATUS goes in cols[3] (under COLLATERAL) to match a filled
                # trade, where STATUS sits under COST TO CLOSE; cols[2]
                # (bottom-left) is left empty.
                _strike_v = float(t.get("strike", 0))
                _call = t.get("option_type") == "C"
                with cols[0]:
                    metric_card("CREDIT IF FILLED", f"${total_credit:,.0f}",
                                delta=f"${credit_ps:.2f}/sh", delta_sign="neutral")
                with cols[1]:
                    if _call:
                        # A covered call is collateralized by shares, not cash —
                        # 100 shares per contract must be held to cover it.
                        metric_card("SHARES TO COVER", f"{100 * qty:,}",
                                    delta=f"100 × {qty} shares",
                                    delta_sign="neutral")
                    else:
                        metric_card("COLLATERAL", f"${_strike_v * 100 * qty:,.0f}",
                                    delta=f"${_strike_v:g} × 100 × {qty}",
                                    delta_sign="neutral")
                with cols[3]:
                    metric_card("STATUS", _disp_status.upper())

            # Close-mode flags (pure from the trade + config), hoisted so the
            # broker-status line and close disclaimer can render up in the
            # details column while the close controls below reuse them.
            trade_live = not bool(t.get("paper"))
            config_paper = bool(scfg.get("paper", True))
            close_live = trade_live and not config_paper
            _live_in_paper = trade_live and config_paper

            # Broker-order status + close-mode disclaimer, as closures so the
            # snapshot layout (below the contract details, left of the chart)
            # and the no-snapshot layout share one definition.
            def _broker_status_line():
                if bs is None:
                    return
                if filled:
                    _fn, _qn = bs.get("filled"), bs.get("quantity")
                    _frac = (f" ({int(_fn)} of {int(_qn)} contracts)"
                             if _fn is not None and _qn is not None else "")
                    _fat = bs.get("filled_at")
                    _when = f" on {_fat:%b %d, %Y %I:%M %p}" if _fat else ""
                    st.caption(f"✅ Broker order **FILLED**{_frac}{_when}.")
                elif working:
                    st.caption(f"⏳ Broker order **{bs['status']}** — not yet "
                               "filled; the P/L above applies once it fills. "
                               "Cancel below, or wait for a fill.")
                else:
                    st.caption(f"Broker order: **{bs['status']}**.")

            def _close_disclaimer_line():
                if not (t.get("status") == "open" and filled):
                    return
                if _live_in_paper:
                    st.warning(
                        "⚠️ This is a **real (live)** position, but the app is "
                        "in **paper mode** (`paper = true`). Paper mode can't "
                        "send — or simulate — a closing order for a live "
                        "position (that would desync the tracker from your open "
                        "broker position). Set `paper = false` in config.toml "
                        "and restart to manage it.")
                else:
                    st.caption("🔴 LIVE close — sends a real buy-to-close order."
                               if close_live else
                               "📝 Records the close in the tracker; no live "
                               "order.")
                if close_live and market_open is False:
                    st.caption("⏸ Market closed")

            # Open position: details (two columns) + status/disclaimer left,
            # cards as a 2x2 grid + day chart right. No snapshot (closed/
            # canceled): cards span full width, status/disclaimer inline below. A
            # working opening order uses the same grid/row, just with order-
            # pending cards (3 of the 4 cells filled — P/L doesn't apply yet).
            _card_fn = _render_pending_cards if _pending_open else _render_cards
            if _has_snapshot:
                _details_col, _cards_col = st.columns([1, 1])
                with _details_col:
                    _s1, _s2 = st.columns(2)
                    with _s1:
                        st.markdown(_kv_table_html(_terms),
                                    unsafe_allow_html=True)
                    with _s2:
                        st.markdown(_kv_table_html(_prices),
                                    unsafe_allow_html=True)
                    # Broker status + close disclaimer, below the contract
                    # details (to the left of the day chart).
                    _broker_status_line()
                    _close_disclaimer_line()
                with _cards_col:
                    _row1 = st.columns(2)
                    _row2 = st.columns(2)
                    _card_fn([_row1[0], _row1[1], _row2[0], _row2[1]])
                    # Day chart of the underlying, below the cards and spanning
                    # the right column (just under UNREALIZED P/L / STATUS).
                    _chart = _day_chart(chart_by_ticker.get(t.get("ticker")))
                    _box = st.container(border=True)
                    # Header: the "TODAY · TICKER" eyebrow on the left, spot and
                    # today's change right-aligned on the same line — so the
                    # number the line is drawing is readable without hunting for
                    # the Spot row in the details table. `_spot`/`_spct` come from
                    # the fetch above (same _has_snapshot branch, no extra call);
                    # the price segment is dropped entirely when spot is
                    # unavailable rather than showing a dash next to the eyebrow.
                    # Green/red match the chart line and the Spot/Day% columns.
                    _box.markdown(
                        "<div style='display:flex;align-items:baseline;"
                        "justify-content:space-between;gap:8px;'>"
                        "<span style='font-size:0.62rem;font-weight:700;"
                        "letter-spacing:0.09em;color:#94a3b8;"
                        f"text-transform:uppercase;'>Today · "
                        f"{t.get('ticker', '')}</span>"
                        f"{_day_head_px(_spot, _spct)}</div>",
                        unsafe_allow_html=True)
                    if _chart is not None:
                        _box.altair_chart(_chart, use_container_width=True)
                    else:
                        _box.caption("Intraday chart unavailable.")
            else:
                _card_fn(st.columns(4))
                _broker_status_line()
                _close_disclaimer_line()

            # Closing order in flight: a live buy-to-close is working. Keep
            # tracking the position — poll the close order, offer Cancel, and
            # finalize to "closed" only once it fills (mirrors the opening-order
            # working→filled lifecycle). `continue` skips the open-order branches.
            if t.get("status") == "closing":
                cbs = close_status_by_id.get(t.get("id"))
                _lim = t.get("close_limit_px")
                _lim_txt = f" @ ${_lim:.2f}" if _lim else ""
                _cqty = int(t.get("close_qty") or qty)
                _qty_txt = f" ({_cqty} of {qty})" if _cqty < qty else ""
                # Fill / expiry handling lives in _settle_closing_trade, shared
                # with the on-load reconciliation both tabs run — so a closing
                # order settles the same way whether it's noticed here or on the
                # Close tab. A note means it ended without fully filling.
                _chg, _note = _settle_closing_trade(t, cbs)
                if _chg:
                    if _note:
                        st.session_state[f"close_note_{t['id']}"] = _note
                    st.session_state.pop(f"close_result_{t['id']}", None)
                    _order_status.clear()
                    st.rerun()
                _cstat = cbs.get("status") if cbs else None
                _close_working = bool(cbs and cbs.get("cancelable"))

                if _cstat:
                    st.caption(f"⏳ Closing order **{_cstat}**{_qty_txt}{_lim_txt}"
                               " — buy-to-close not yet filled. Cancel below to "
                               "keep the position open, or wait for a fill.")
                else:
                    st.caption(f"⏳ Closing order placed{_qty_txt}{_lim_txt}; "
                               "broker status unavailable — hit 🔄 to re-check, "
                               "or Cancel below.")
                _ccrk = f"close_cancel_result_{t['id']}"
                _xc1, _xc2, _ = st.columns([2, 2, 3])
                with _xc1:
                    if st.button("Cancel closing order",
                                 key=f"cancel_close_{t['id']}",
                                 disabled=not _close_working,
                                 help=("Cancels the unfilled buy-to-close at the "
                                       "broker; the position stays open."
                                       if _close_working else
                                       "Only a working order can be canceled — "
                                       "hit 🔄 to re-check."),
                                 width="stretch"):
                        st.session_state[_ccrk] = _cancel_close_order(scfg, t)
                        # Drop the stale "closing order sent" message so the
                        # reverted-to-open close panel doesn't resurface it.
                        st.session_state.pop(f"close_result_{t['id']}", None)
                        _order_status.clear()
                        st.rerun()
                with _xc2:
                    _rmbox = st.container(key=f"rm_box_{t['id']}")
                    _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                                  on_click=trades_store.remove,
                                  args=(t["id"],), width="stretch")
                _ccres = st.session_state.get(_ccrk)
                if _ccres:
                    (st.success if _ccres["ok"] else st.error)(
                        _ccres["msg"].replace("$", "\\$"))
                continue

            # Broker-order status now renders up in the details column (see
            # _broker_status_line, above the card layout).

            # A live order that hasn't filled yet → Cancel; it isn't a position
            # so there's nothing to close. Once it's FILLED the close controls
            # take over.
            #
            # A PAPER trade goes straight to the close controls: there is no
            # broker order to reach FILLED, so gating on `filled` (as this once
            # did) made a simulated close unreachable even though the whole path
            # exists — `_submit_close(live=False)` records it in the tracker with
            # realized P/L. Its discard button moves down there alongside them,
            # since discarding a trade you never took isn't the same outcome as
            # closing it.
            _cancel_branch = (t.get("status") == "open" and working)
            _close_branch = (t.get("status") == "open"
                             and (filled or is_paper))
            if _cancel_branch:
                _crk = f"cancel_result_{t['id']}"
                # Equal-width, adjacent on the left (spacer column on the right).
                _ac1, _ac2, _ = st.columns([2, 2, 3])
                with _ac1:
                    if st.button("Cancel working order",
                                 key=f"cancel_ord_{t['id']}",
                                 help="Cancels the unfilled order at the broker "
                                      "— no position changes.", width="stretch"):
                        st.session_state[_crk] = _cancel_order(scfg, t)
                        _order_status.clear()
                        # Re-run the fragment so the status reflects it now.
                        st.rerun()
                with _ac2:
                    _rmbox = st.container(key=f"rm_box_{t['id']}")
                    _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                                  on_click=trades_store.remove,
                                  args=(t["id"],), width="stretch")
                _cres = st.session_state.get(_crk)
                if _cres:
                    (st.success if _cres["ok"] else st.error)(
                        _cres["msg"].replace("$", "\\$"))
            elif _close_branch:
                # One-time note when a closing order just terminated unfilled and
                # the position was auto-reverted to open (see the closing block).
                _cnote = st.session_state.pop(f"close_note_{t['id']}", None)
                if _cnote:
                    st.info(_cnote)
                default_close = (trade_actions.ceil_to_tick(close_mid)
                                 if close_mid else 0.05)
                # Re-seed the Close-limit field to the live mid whenever the
                # re-quote moves. A keyed number_input ignores its value= arg
                # after first render, so without this the field would freeze at
                # the first quote's default and could sit above a since-
                # cheapened ask. Seeding via session_state re-proposes the mid
                # on each refresh; a manual edit survives until the mid changes.
                _wid_key = f"close_limit_{t['id']}"
                _seed_key = f"close_seed_{t['id']}"
                if st.session_state.get(_seed_key) != default_close:
                    st.session_state[_wid_key] = float(default_close)
                    st.session_state[_seed_key] = default_close
                _confirm_key = f"close_confirm_{t['id']}"
                _result_key = f"close_result_{t['id']}"
                _result = st.session_state.get(_result_key)
                # Badge reflects what the close would do; a live position in
                # paper mode is LIVE (and blocked), never "paper". The close-mode
                # flags are hoisted above the card, and the mode/market-gate
                # disclaimer renders up in the details column
                # (_close_disclaimer_line).
                _close_badge = ("🔴 LIVE" if (close_live or _live_in_paper)
                                else "📝 PAPER")

                # Inputs row: close limit + how many contracts to buy back —
                # always shown (locked at 1 for a single-lot) so the count is
                # never in doubt. Buttons sit on the row below.
                _il, _if, _ql, _qf, _ = st.columns(
                    [1, 1.2, 1.3, 0.9, 1.6], vertical_alignment="center")
                with _il:
                    st.markdown("Close limit")
                with _if:
                    # No min_value/max_value here or on Contracts: Streamlit
                    # won't commit an out-of-range entry (it keeps the last valid
                    # value and shows its own message), which would arm Place for
                    # a number the user never typed. Validated by
                    # trade_actions.close_input_error below.
                    _clbox = st.container(key=f"close_box_{t['id']}")
                    close_limit = _clbox.number_input(
                        "Close limit",
                        step=float(trade_actions.tick_for(default_close)),
                        format="%.2f", key=_wid_key,
                        label_visibility="collapsed",
                    )
                # Default to the whole position, re-seeded only when the position
                # size itself changes (e.g. a prior partial close shrank it). The
                # old version clamped any value above `qty` on every rerun, which
                # ate the user's own over-max entry on the same rerun the Confirm
                # click caused — the field snapped back to the max with no error.
                _qk = f"close_qty_{t['id']}"
                confirm_gate.reseed_on_change(_qk, f"close_qty_seed_{t['id']}",
                                              qty)
                with _ql:
                    st.markdown(f"Contracts (of {qty})")
                with _qf:
                    # Left uncast — an emptied box returns None, and int(None)
                    # would raise before the validity check below can report it.
                    close_n = st.number_input(
                        "Contracts", step=1,
                        format="%d", key=_qk, label_visibility="collapsed",
                        disabled=(qty == 1))

                # Editing the limit or the contract count after confirming
                # disarms Place (confirm_gate), so the panel below can only ever
                # describe the numbers Confirm was actually pressed on.
                _val_keys = (_wid_key, _qk)
                _input_err = trade_actions.close_input_error(
                    close_limit, close_n, qty)
                _valid = _input_err is None
                _armed = confirm_gate.armed(_confirm_key, _val_keys,
                                            valid=_valid)
                if _valid:
                    close_limit, close_n = float(close_limit), int(close_n)
                else:
                    st.error(_input_err)
                def _close_error(limit_v, n_v, _held=qty):
                    return trade_actions.close_input_error(limit_v, n_v, _held)

                # A paper trade gets a third action: discard it outright. Closing
                # books a realized P/L as though the trade ran its course, which
                # is the wrong record for a simulation you've decided against.
                if is_paper:
                    _bc, _dc, _rc, _ = st.columns(
                        [2, 2, 2, 1], vertical_alignment="center")
                else:
                    _bc, _rc, _ = st.columns([2, 2, 1],
                                             vertical_alignment="center")
                    _dc = None
                with _bc:
                    # An invalid limit/size does NOT disable Confirm — it stays
                    # clickable so a correction lands in one click (the click
                    # commits the field, then the callback re-validates). Only
                    # blocks editing can't fix (paper mode, market hours) disable.
                    if _live_in_paper:
                        _blocked = ("Live position — set paper=false in "
                                    "config.toml to send a closing order.")
                    elif close_live and market_open is not True:
                        _blocked = ("Equity options trade 9:30–16:00 ET, "
                                    "Mon–Fri." if market_open is False
                                    else "Can't confirm market hours.")
                    else:
                        _blocked = None
                    if _blocked:
                        st.button(f"Confirm Closing Trade · {_close_badge}", disabled=True,
                                  key=f"close_btn_{t['id']}", help=_blocked,
                                  width="stretch", type="primary")
                    elif _armed:
                        st.button(f"Confirm Closing Trade · {_close_badge}",
                                  disabled=True, key=f"close_btn_{t['id']}",
                                  help=confirm_gate.ARMED_HELP,
                                  width="stretch", type="primary")
                    else:
                        st.button(f"Confirm Closing Trade · {_close_badge}",
                                  key=f"close_btn_{t['id']}", width="stretch",
                                  type="primary",
                                  on_click=confirm_gate.arm(
                                      _confirm_key, _val_keys,
                                      clear_keys=(_result_key,),
                                      validate=_close_error))
                if _dc is not None:
                    with _dc:
                        if st.button("Cancel (discard paper trade)",
                                     key=f"cancel_ord_{t['id']}", width="stretch",
                                     help="Marks this simulated trade canceled — "
                                          "it was never placed, so it isn't a "
                                          "close and books no P/L."):
                            trades_store.update(
                                t["id"], status="canceled",
                                canceled_at=datetime.now().isoformat(
                                    timespec="seconds"))
                            # Toast, not an inline message: canceling moves the
                            # record out of this branch, so anything rendered
                            # here would have nowhere to land after the rerun.
                            st.session_state["_osc_toast"] = (
                                "📝 Paper trade canceled — no close was booked.")
                            st.rerun()
                with _rc:
                    _rmbox = st.container(key=f"rm_box_{t['id']}")
                    _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                                  on_click=trades_store.remove,
                                  args=(t["id"],), width="stretch")

                if _armed:
                    _debit = close_limit * 100 * close_n
                    _of2 = f" of {qty}" if close_n < qty else ""
                    _otw2 = "CALL" if t.get("option_type") == "C" else "PUT"
                    st.warning(
                        f"**Confirm close** — BUY TO CLOSE {close_n}{_of2} "
                        f"{t.get('ticker')} ${t.get('strike')} {_otw2} @ "
                        f"${close_limit:.2f} (debit **${_debit:,.0f}**) · "
                        + ("🔴 **LIVE**" if close_live else "📝 **PAPER**"))
                    # Red Cancel, mirroring the Sell Put confirm panel; CSS
                    # scoped to this trade's keyed container so other open rows
                    # aren't restyled.
                    _cancel_box_key = f"close_cancel_box_{t['id']}"
                    st.markdown(
                        ("<style>"
                         "[class*='st-key-KEY'] button{background-color:#d9534f "
                         "!important;border-color:#d9534f !important;"
                         "color:#fff !important;}"
                         "[class*='st-key-KEY'] button:hover{"
                         "background-color:#c9302c !important;"
                         "border-color:#c9302c !important;color:#fff !important;}"
                         "[class*='st-key-KEY'] button p{color:#fff !important;}"
                         "</style>").replace("KEY", _cancel_box_key),
                        unsafe_allow_html=True,
                    )
                    # Collapse via on_click (runs before the rerun body) so
                    # Cancel takes effect on the first click, like Sell Put.
                    bc1, bc2, _ = st.columns([1, 1, 3])
                    with bc1:
                        _do = st.button(f"Place Closing Trade · {_close_badge}",
                                        key=f"close_do_{t['id']}",
                                        type="primary", width="stretch")
                    with bc2:
                        _cbox = st.container(key=_cancel_box_key)
                        _cbox.button("Cancel", key=f"close_cancel_{t['id']}",
                                     width="stretch",
                                     on_click=confirm_gate.disarm(_confirm_key))
                    if _do:
                        _result = _submit_close(scfg, t, close_limit, close_live,
                                                close_n)
                        st.session_state[_result_key] = _result
                        st.session_state[_confirm_key] = False
                        if _result.get("ok"):
                            # Confirm via the center banner, and DROP the stored
                            # result so it can't also render inline below (one
                            # placement, one message).
                            st.session_state["_osc_toast"] = _result["msg"]
                            st.session_state.pop(_result_key, None)
                        # Rerun either way. Disarming above only takes effect on
                        # the NEXT run, so without this the panel the click came
                        # from stays on screen with Place Closing Trade still
                        # live — the paper path used to skip the rerun and did
                        # exactly that. A full rerun (not scope="fragment") is
                        # required: run_app renders the banner.
                        st.rerun()

                if _result:
                    (st.success if _result.get("ok") else st.error)(
                        _result["msg"].replace("$", "\\$"))
            elif t.get("status") == "open":
                # Live order, but its broker status couldn't be read — can't
                # tell working vs filled, so offer neither action automatically.
                st.caption("Order status unavailable — verify at your broker; "
                           "use **Remove from tracker** if it didn't fill.")

            # Else it already sits next to Cancel / Place Closing Trade above.
            if not _cancel_branch and not _close_branch:
                _rmbox = st.container(key=f"rm_box_{t['id']}")
                _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                              on_click=trades_store.remove, args=(t["id"],))

    st.caption("Estimates use a live Schwab mid; verify at your broker.")
