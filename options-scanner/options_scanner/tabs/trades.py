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
from datetime import date, datetime

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
from options_scanner.format import (
    days_since, dte_cell, kv_table_html, leg_rows, money_md,
    open_prices_cell,
)
from options_scanner.ui_theme import (
    df_height, metric_card, scroll_into_view, section_header,
)

# Max wall-clock the Trades tab waits for all per-trade Schwab reads (order
# status + cost-to-close re-quote), which run in parallel. A slower or hung
# call past this leaves that trade's data unavailable rather than freezing the
# tab. Kept under the client's own HTTP timeout (SCHWAB_HTTP_TIMEOUT_S).
_TRADES_FETCH_TIMEOUT_S = 8.0


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


@st.cache_data(ttl=30, show_spinner=False)
def _equity_quote(app_key: str, app_secret: str, callback_url: str,
                  token_file: str, ticker: str) -> dict | None:
    """Cached (30s) read-only quote for one stock, or None.

    The sell leg of an unwind. A stock needs a real two-sided quote for the same
    reason the option does — the net credit is the difference between them, so
    pricing off a last trade would misstate what the package is worth. `mid` is
    Schwab's mark when it gives one, else the bid/ask midpoint.

    Returns ``{bid, ask, mid, mark, last, volume, pct_change}``.

    ``mid`` is the true bid/ask midpoint, and Schwab's ``mark`` is reported
    separately rather than substituted for it. They are not the same number —
    on a liquid stock the mark tracks the last trade and can sit cents off the
    midpoint (bid 376.94 / ask 377.07 → mid 377.005, mark 376.95). Conflating
    them broke `unwind_net_quote`'s bracket: it prices `worst`/`best` off the
    raw bid and ask, so a `mid` taken from the mark wasn't the center of the
    two numbers the panel shows it between.

    volume/pct_change ride along free on the same response and let the unwind
    panel show what the shares are doing today — whether you're selling into
    strength or weakness — beside what they're worth. Everything but bid/ask is
    None-able; only bid/ask are load-bearing (no two-sided market → None, and
    the caller can't price the unwind).
    """
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
        resp = client.get_quote(ticker)
        if resp.status_code != 200:
            return None
        q = (resp.json().get(ticker) or {}).get("quote") or {}
    except Exception:
        return None

    def _f(key):
        try:
            v = float(q.get(key))
        except (TypeError, ValueError):
            return None
        return v if v == v and v > 0 else None

    bid, ask = _f("bidPrice"), _f("askPrice")
    if bid is None or ask is None:
        return None
    # The midpoint, computed — never Schwab's mark standing in for it. See the
    # docstring: the mark is a different number and callers price against the
    # spread, so substituting it silently decentered the net-credit bracket.
    mid = round((bid + ask) / 2, 4)

    # Day context, best-effort. `_f` rejects non-positive values, which is right
    # for prices but would silently drop a down day and a zero-volume session,
    # so these two parse straight.
    def _raw(key):
        try:
            v = float(q.get(key))
        except (TypeError, ValueError):
            return None
        return v if v == v else None

    _vol = _raw("totalVolume")
    return {"bid": bid, "ask": ask, "mid": mid, "mark": _f("mark"),
            "last": _f("lastPrice"),
            "volume": int(_vol) if _vol is not None else None,
            "pct_change": _raw("netPercentChange")}


def held_iv_pp(df, pos: dict) -> float | None:
    """IV+pp of the held leg, read out of an already-fitted chain frame.

    The comparison only means something when the number comes from the same
    surface fit as whatever it's shown beside — an IV+pp from a separate fetch
    is measured against a differently-fitted surface. None when the contract
    isn't in the chain (expired, or a strike the provider no longer lists) or
    has no fitted value.
    """
    try:
        side = "call" if pos.get("option_type") == "C" else "put"
        if df is None or "iv_excess" not in getattr(df, "columns", []):
            return None
        m = df[(df["type"] == side)
               & (df["expiration"] == str(pos.get("expiration")))
               & ((df["strike"] - float(pos.get("strike"))).abs() < 1e-6)]
        if m.empty:
            return None
        v = float(m["iv_excess"].iloc[0]) * 100.0
        return v if v == v else None       # NaN → unknown
    except (KeyError, TypeError, ValueError):
        return None


def leg_iv_pp(pos: dict, ticker: str, provider: str,
              scfg: dict) -> float | None:
    """IV+pp for one held leg, fetching and fitting a chain to get it.

    Every other figure on a leg snapshot rides along on the re-quote; this one
    doesn't exist in a quote, so it costs a chain read (~2s measured — the fit
    itself is ~4ms) behind `fetch_and_enrich`'s 5-minute cache. Only the first
    render of a given ticker pays; reruns from editing a limit or a size are
    free, as is switching between Close, Roll and Unwind on the same position.

    The fetch window runs from 0 DTE to past the held leg, because the surface
    is fitted across expirations — pulling only the leg's own expiration would
    leave nothing to fit against. Narrowing it saves nothing anyway: the cost is
    the round-trip, not the row count.

    Never raises. IV+pp is context, not a precondition for placing an order, so
    a throttled or empty chain drops the row rather than blocking the panel.
    """
    from options_scanner.fetch import fetch_and_enrich
    try:
        held_dte = (datetime.strptime(str(pos.get("expiration", "")),
                                      "%Y-%m-%d").date() - date.today()).days
    except (TypeError, ValueError):
        return None
    if held_dte < 0:
        return None
    opt_key = "calls" if pos.get("option_type") == "C" else "puts"
    try:
        with st.spinner(f"Fitting {ticker}'s IV surface…"):
            df, _earn, err = fetch_and_enrich(
                ticker, opt_key, 0, max(400, held_dte + 1), provider=provider,
                schwab_config=scfg)
        if err or df is None or df.empty:
            return None
        return held_iv_pp(df, pos)
    except Exception:  # noqa: BLE001 — provider/SDK errors are all non-fatal
        return None


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
                      "unchanged at your broker (see the **Positions** tab).")
    trades_store.update(t["id"], status="open", close_order_id=None,
                        close_limit_px=None, close_qty=None)
    return True, (f"Closing order {_cstat} without filling — the position is "
                  "open again; place a new closing order when ready.")


def _reconcile_closing_orders(scfg: dict) -> bool:
    """Poll every tracked working closing order and settle the ones that already
    resolved at the broker. Returns True when anything changed.

    Run on load by BOTH the Trades and Positions tabs. Without it a closing order
    that expired overnight stayed "closing" in the store until the user happened
    to expand that specific row on the Trades tab (the per-trade Schwab reads are
    deferred behind row expansion) — which left the Positions tab refusing to place a
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
                            "paper=false in config.toml (applies on your next "
                            "click, no restart).")}
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


# ── Live Schwab option legs — the table behind the "Positions" tab ───────────

# Cached (60s) read-only reader for every live option leg — it lives in
# positions_cache so the roll builder and the ⚙️ Settings dialog share one cache and
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
    Shared with the roll builder — see ui_theme."""
    scroll_into_view()


def _render_option_close(pos: dict, scfg: dict, market_open,
                         config_paper: bool, spot: float | None = None,
                         provider: str = "schwab") -> None:
    """Close builder for one selected Schwab option leg: leg snapshot, editable
    limit + contracts (partial close), LIVE + market-hours gate, 2-step confirm.
    A tracked leg routes through _submit_close (so it shows 'closing' up top); an
    untracked leg uses _submit_position_close.

    `spot` is the underlying's live price, passed down from the caller's already-
    fetched spot map (None when unavailable) so the snapshot can lead with it
    without a second fetch. `provider` is only used to fetch the chain behind
    IV+pp — this panel renders on row selection, not on tab load, so it can
    afford the one scan a fitted surface needs."""
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
                   "`paper = false` in config.toml — it takes effect on your "
                   "next click here, no restart needed. In paper mode this "
                   "section is view-only.")
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
        # The same leg snapshot the roll and unwind builders show, from the same
        # `leg_rows` — one contract described one way wherever you act on it.
        # Everything here but IV+pp rides along on the re-quote above; IV+pp
        # needs a chain fetch and surface fit (~2s, cached 5 min), which this
        # panel pays because it only renders once a row is selected.
        _iv_pp = leg_iv_pp(pos, tkr, provider, scfg)
        st.markdown(kv_table_html(leg_rows(
            q.get("bid"), q.get("ask"), q.get("mid"), q.get("last"),
            q.get("open_interest"), q.get("volume"), q.get("last_trade_ms"),
            iv_pp=_iv_pp, iv=q.get("iv"), delta=q.get("delta"),
            spot=spot, fmt_last_et=trade_actions.fmt_last_trade_et),
            pairs=2), unsafe_allow_html=True)
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
        # Buying back a short is a debit — what it costs, against what the
        # account has to pay it with, right where the limit was just set.
        if not is_long:
            st.caption(f"Cost to close at this limit: "
                       f"**{money_md(limit * 100 * close_n)}**")
    else:
        st.error(_input_err)
    render_buying_power_caption(scfg, "Account", f"close_{posid}")
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


def _split_by_right(positions: list, frame, is_call: bool):
    """``(positions, rows)`` for one side of the Positions tab's split tables.

    The two must stay index-aligned. The table hands back a row *index*, and
    that index picks the position to close — if the row frame and the position
    list were filtered separately and drifted, selecting a row would close a
    different leg than the one on screen. Both come out of a single mask here so
    they can't.
    """
    import pandas as pd
    mask = [(str(p.get("option_type", "P")).upper() == "C") == bool(is_call)
            for p in positions]
    subset = [p for p, keep in zip(positions, mask) if keep]
    rows = frame[pd.Series(mask, index=frame.index)].reset_index(drop=True)
    return subset, rows


def _buying_power_line(cap: dict | None) -> str | None:
    """"Cash $X · With margin $Y" for the Positions tab, or None when unavailable.

    Closing a short is a debit, so the question this answers is "can I afford to
    buy this back?". Two figures, both straight from Schwab and named for what
    they are:

    * **Cash** — settled cash you could spend without borrowing:
      ``cashAvailableForTrading`` on a cash account, else ``cashBalance``.
    * **With margin** — ``buyingPower`` (Schwab falls back to
      ``optionBuyingPower``), i.e. cash plus what the account can borrow.

    Only the cash figure shows on an account with no margin line, rather than
    printing the same number twice under two labels.
    """
    if not cap:
        return None
    bal = cap.get("balances") or {}
    cash = cap.get("cash")
    if cash is None:
        cash = bal.get("cashBalance")
    margin = cap.get("bp")
    if cash is None and margin is None:
        return None
    # money_md escapes each "$": two unescaped ones in a single markdown string
    # are LaTeX delimiters, and Streamlit would eat both dollar signs and set
    # everything between them in math type — which is what made these read as
    # unformatted numbers rather than currency.
    bits = []
    if cash is not None:
        bits.append(f"Cash **{money_md(cash)}**")
    if margin is not None and (cash is None
                               or abs(float(margin) - float(cash)) >= 1):
        bits.append(f"with margin **{money_md(margin)}**")
    return " · ".join(bits)


# The caveat that matters before leaning on the margin figure, kept in one
# place: every screen that shows these two numbers explains them the same way.
_BUYING_POWER_TIP = (
    "Cash is settled cash you can spend without borrowing (Schwab "
    "cashAvailableForTrading, else cashBalance). With margin is Schwab "
    "buyingPower — cash plus what the account can borrow. Long options are not "
    "marginable, so your broker may allow less than the margin figure for an "
    "option purchase.")

# Both labels survive masking: which figures exist is not the private part, and
# a bare "•••••" wouldn't say what had been hidden.
_MASKED_BUYING_POWER = "Cash **\\$•••••** · with margin **\\$•••••**"


def render_buying_power_caption(scfg: dict, lead: str, key: str = "") -> None:
    """"💵 <lead>: Cash $X · with margin $Y" — or nothing when unavailable.

    Reads the shared 60s account cache, so the Positions table and the roll
    dialog cost one Schwab round-trip between them (the Sell dialog's sizing
    read is the same one again). The whole line is the hover target — no marker,
    just a help cursor carrying `_BUYING_POWER_TIP`.

    Honors the ⚙️ mask-balances preference, with the 👁 reveal beside the line;
    `key` keeps that button unique across the screens that render this (it's on
    four). Masking is display-only — the figures themselves are still fetched,
    because the caption is the only thing hiding them.
    """
    line = _buying_power_line(positions_cache.account_capacity(
        scfg.get("app_key", ""), scfg.get("app_secret", ""),
        scfg.get("callback_url", ""), scfg.get("token_file", "")))
    if not line:
        return
    if settings_ui.balances_masked():
        line = _MASKED_BUYING_POWER
    _tc, _bc = st.columns([9, 1], vertical_alignment="center")
    with _tc:
        st.caption(f"<span style='cursor:help;' title='{_BUYING_POWER_TIP}'>"
                   f"💵 {lead}: {line}</span>", unsafe_allow_html=True)
    with _bc:
        settings_ui.render_reveal_toggle(key or lead)


# The stored lifecycle word for a live position is "open" — but on screen that
# reads as an *open order*, i.e. one still working, which is the opposite of
# what it means here. "held" can't be misread as an order state, and it works
# for a paper position too (where "filled" would be a lie: nothing was sent to
# a broker). Display only — `status` in the trade log is untouched, and nothing
# branches on this string.
_STATUS_WORDS = {"open": "held"}


def _display_status(store_status, broker_status, filled_known: bool) -> str:
    """The one status word for a trade — used by the collapsed header and the
    expanded view alike, so they can't disagree.

    Two different facts are in play: the store tracks the *record's* lifecycle
    (open → closing → closed), the broker tracks the *opening order* (WORKING →
    FILLED). The header used to substitute the broker's word for the store's
    whenever a broker read happened to be available — and since a collapsed row
    deliberately fetches nothing, the same trade read "open" collapsed and
    "working" expanded.

    The rule:

    * a known fill means the position exists → **"held"** (a fill is recorded
      once, as ``filled_at``, so this stays true without re-reading Schwab);
    * an opening order that hasn't filled says what the broker says →
      **"working"**, "rejected", "expired", …;
    * with nothing known, the store's own word stands.

    So the two states a reader has to tell apart — "my order is still out
    there" and "I own this" — never share a word.
    """
    store_status = str(store_status or "open")
    if store_status != "open" or filled_known or not broker_status:
        return _STATUS_WORDS.get(store_status, store_status)
    return str(broker_status).lower()


def _intrinsic_value(spot, strike, option_type: str) -> float | None:
    """Intrinsic value per share — what the contract is worth on exercise alone
    (call: spot − strike, put: strike − spot, each floored at 0). None when spot
    is unavailable."""
    try:
        spot, strike = float(spot), float(strike)
    except (TypeError, ValueError):
        return None
    if spot != spot or spot <= 0:                   # NaN / no quote
        return None
    return (max(0.0, spot - strike) if str(option_type).upper() == "C"
            else max(0.0, strike - spot))


def yield_base(spot, strike, option_type: str, mark=None,
               covered: bool = False) -> float | None:
    """Capital a **held** leg actually ties up, per share — what an annualized
    yield on it should be measured against. None when it can't be determined.

    * **Covered call** → ``spot − mark``, the position's net liquidation value.
      That's what unwinding frees, and it's the capital genuinely committed:
      the upside is capped at the strike, so measuring against spot overstates
      the base — on a deep-ITM call by about 2× (a $185 call with spot $380 ties
      up ~$183/share, not $380).
    * **Short put** → ``strike``, the cash securing it. Already a
      capital-committed base, so unchanged.
    * **Anything else** (long legs, naked short calls) → spot for calls, strike
      for puts. A long's committed capital is the premium paid and a naked
      call's is broker margin; neither is the same question, so the old rule
      stands rather than inventing an answer.

    Candidate options in a chain scan deliberately do NOT use this — nothing is
    held yet, so the base there is the collateral you *would* commit
    (`chain_common.build_row`).
    """
    try:
        spot = float(spot) if spot is not None else None
        strike = float(strike)
    except (TypeError, ValueError):
        return None
    is_call = str(option_type).upper() == "C"
    if covered and is_call:
        try:
            net_liq = spot - float(mark)
        except (TypeError, ValueError):
            net_liq = None
        # A mark at or above spot would mean the call is worth more than the
        # stock backing it — impossible for a standard option, so treat it as
        # bad data and fall through rather than divide by ~0.
        if net_liq is not None and net_liq > 0:
            return net_liq
    base = spot if is_call else strike
    return base if base and base > 0 else None


def _time_value(mark, spot, strike, option_type: str) -> float | None:
    """Extrinsic value per share: ``mark − intrinsic``, floored at 0.

    Whatever the market pays above intrinsic is time value. Floored because a
    mark below intrinsic is a quote artifact — a wide spread or a stale print —
    not negative time value.

    None when the mark or spot is unavailable. Feeds both the Intrinsic | Time
    column and the Ann% it's annualized into, so the two can't disagree.
    """
    intrinsic = _intrinsic_value(spot, strike, option_type)
    if intrinsic is None:
        return None
    try:
        mark = float(mark)
    except (TypeError, ValueError):
        return None
    if mark != mark:
        return None
    return max(0.0, mark - intrinsic)


def _position_pl(avg_price, mark, quantity, direction) -> float | None:
    """Unrealized P/L on a whole option leg, in dollars.

    Short: premium collected − cost to buy back → ``(avg − mark) × 100 × qty``.
    Long:  value now − what you paid            → ``(mark − avg) × 100 × qty``.

    Positive is in your favor either way, which is what lets one green/red rule
    cover both directions. Replaces the raw Avg column: the broker's open price
    only mattered as an input to this.
    """
    try:
        avg, mk, qty = float(avg_price), float(mark), int(quantity)
    except (TypeError, ValueError):
        return None
    if avg != avg or mk != mk or not qty:           # NaN / nothing held
        return None
    per_share = (avg - mk if str(direction).strip().lower() == "short"
                 else mk - avg)
    return per_share * 100 * qty


# ── Moneyness: the ITM% column, the row shade, and the row order ────────────
# Lives here rather than in rolls.py because tabs/rolls already imports from
# this module (the reverse would be a cycle).
#
# Bands are (exclusive upper bound, tint, legend label), ordered deep-OTM →
# deep-ITM; one table drives both the shade and the legend so they can't drift.
# Translucent tints so they overlay the cell's theme background in light AND
# dark mode, in a cool → hot ramp: slate (far out, nothing to do) → sky →
# yellow at the money → orange → red. Deliberately no green — these tables
# color P/L green, and a green row would read as "profitable" rather than "far
# from the strike".
MONEYNESS_BANDS = [
    (-15.0, "rgba(148,163,184,0.16)", "&gt;15% OTM"),
    (-5.0,  "rgba(56,189,248,0.16)",  "5–15% OTM"),
    (5.0,   "rgba(234,179,8,0.18)",   "within 5% (ATM)"),
    (15.0,  "rgba(249,115,22,0.18)",  "5–15% ITM"),
    (None,  "rgba(239,68,68,0.18)",   "&gt;15% ITM"),
]

# ── Covered-call coverage: the stocks table's row shade and legend ──────────
# Same rules as the moneyness bands above: translucent tints that overlay the
# cell background in light AND dark mode, and no green — these tables color P/L
# green, so a green row would read as "profitable" rather than "written".
#
# The ramp is by how much is left to do, quiet → loud: fully covered is slate
# (nothing to act on, same reading slate has in MONEYNESS_BANDS), partial is
# yellow, an uncovered position you could actually write is sky (the one row
# that invites a click), and over-written is red because it's the only state
# here that carries risk rather than opportunity.
COVERAGE_BANDS = {
    "covered":     ("rgba(148,163,184,0.16)", "Covered",
                    "Written up to the last whole lot — nothing left to sell."),
    "partial":     ("rgba(234,179,8,0.18)",   "Partly covered",
                    "Some calls written, and another 100-share lot is free."),
    "uncovered":   ("rgba(56,189,248,0.16)",  "Uncovered",
                    "No calls written against these shares."),
    "over_written": ("rgba(239,68,68,0.18)",  "Over-written",
                     "More calls than shares to back them — part of the "
                     "position is naked."),
}

# An uncovered odd lot (under 100 shares) gets no shade at all: it is genuinely
# uncovered, but there is nothing you could write against it, so coloring it
# like an actionable row would be noise. The label still reads "Uncovered".
COVERAGE_NO_SHADE = ""


def coverage_color(state: str, coverable: int = 0) -> str:
    """Row tint for a coverage state; "" when there's nothing worth shading."""
    if state == "uncovered" and not coverable:
        return COVERAGE_NO_SHADE
    band = COVERAGE_BANDS.get(str(state))
    return band[0] if band else ""


def coverage_bg(state: str, coverable: int = 0) -> str:
    """`coverage_color` as a pandas Styler declaration ("" for no shade)."""
    color = coverage_color(state, coverable)
    return f"background-color: {color}" if color else ""


def coverage_label(state: str) -> str:
    band = COVERAGE_BANDS.get(str(state))
    return band[1] if band else str(state)


MONEYNESS_HELP = (
    "How far the strike is through the money, as a % of spot: positive = in "
    "the money, negative = out of the money. Same figure the row shade uses. "
    "Rows start furthest out of the money; click to re-sort. Blank when spot "
    "is unavailable.")


def signed_moneyness(spot, strike, option_type) -> float | None:
    """Moneyness as a signed % of spot: + = in the money, − = out of it.

    A property of the contract, not of your side of it — the sign flips between
    calls and puts, not between long and short. None when spot is unknown, which
    the callers keep distinct from 0.0 (exactly at the money).
    """
    try:
        sp, k = float(spot), float(strike)
    except (TypeError, ValueError):
        return None
    if sp != sp or sp <= 0 or k != k:   # NaN / no usable spot
        return None
    diff = (sp - k) if str(option_type).upper() == "C" else (k - sp)
    return diff / sp * 100.0


def moneyness_color(itm) -> str:
    """Tint for a signed moneyness (% of spot, + = ITM); "" if unknown.

    An exact edge shades as the OTM-ward band (5.0 is ATM, −5.0 is 5–15% OTM),
    matching how these bands read before the five-bucket split.
    """
    try:
        f = float(itm)
    except (TypeError, ValueError):
        return ""
    if f != f:  # NaN → no shade
        return ""
    for upper, color, _label in MONEYNESS_BANDS:
        if upper is None or f <= upper:
            return color
    return ""


def moneyness_bg(itm) -> str:
    """`moneyness_color` as a pandas Styler declaration ("" for no shade)."""
    color = moneyness_color(itm)
    return f"background-color: {color}" if color else ""


def sort_by_moneyness(positions: list, table):
    """(positions, table) reordered furthest-OTM first, kept in lockstep.

    Moneyness is only known once the table has fetched spot, so the built frame
    is what gets sorted — and `positions` is permuted the same way, because row
    selection maps the clicked row index back into that list. If the two ever
    drift, clicking a row acts on a DIFFERENT leg than the one displayed: they
    are reordered together here, and nowhere else.

    A stable sort leaves the caller's ticker/strike ordering as the tie-break.
    Legs with no spot (blank ITM%) sort last rather than reading as 0%, which
    would float them into the middle as if they were at the money.
    """
    import pandas as pd   # local, like every other pandas use in this module
    rank = pd.to_numeric(table["ITM%"], errors="coerce")
    order = list(rank.sort_values(ascending=True, na_position="last",
                                  kind="stable").index)
    return ([positions[i] for i in order],
            table.loc[order].reset_index(drop=True))


def moneyness_legend() -> None:
    """The row-shade color key, rendered off the same bands that shade rows."""
    swatches = "".join(
        "<span style='display:inline-flex;align-items:center;margin-right:18px;'>"
        "<span style='display:inline-block;width:15px;height:15px;"
        "border-radius:3px;border:1px solid rgba(128,128,128,0.5);"
        f"background:{_c};margin-right:6px;'></span>{_lbl}</span>"
        for _upper, _c, _lbl in MONEYNESS_BANDS)
    st.markdown(
        "<div style='font-size:0.8rem;color:var(--osc-ink-3);margin:2px 0 8px 0;'>"
        "<span style='font-weight:600;color:var(--osc-ink-2);margin-right:12px;'>"
        f"Row shade — moneyness:</span>{swatches}</div>",
        unsafe_allow_html=True,
    )


def _signed_value(market_value, quantity: int, direction) -> float | None:
    """The whole leg's market value, signed like a brokerage statement: an
    option you sold is negative (a liability — what it costs to buy back), one
    you bought is positive. None when nothing is held or the value is unusable.

    The sign comes from `direction`, NOT from the broker's own sign, so it can't
    flip on us if Schwab ever reports a short's marketValue unsigned — only the
    magnitude is taken from the broker. An unknown direction reads as long: an
    asset can't understate what you owe.
    """
    try:
        mv, qty = float(market_value), int(quantity)
    except (TypeError, ValueError):
        return None
    if mv != mv or not qty:          # NaN / nothing held
        return None
    magnitude = abs(mv)
    return (-magnitude if str(direction).strip().lower() == "short"
            else magnitude)


def _pos_pkey(p: dict) -> tuple:
    """Stable identity for an option leg — keys the prefetched-quote map."""
    return (p.get("underlying", ""), p.get("option_type", "P"),
            float(p.get("strike", 0)), p.get("expiration", ""))


def _prefetch_position_quotes(positions: list, scfg: dict) -> dict:
    """Parallel per-leg re-quotes → {pkey: quote|None}, so the positions table
    shows each leg's live delta without N sequential round-trips. Reuses the
    cached `_close_quote`; a slow/hung leg leaves that entry None (delta blank)
    rather than blocking the tab. Mirrors the roll builder's quote prefetch — can't
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
def _render_option_positions(scfg: dict, provider: str, market_open,
                             render_detail=None) -> None:
    """The Positions tab's table: every live option leg (with live delta,
    remaining-time-value Ann%, moneyness, and the underlying's spot / day
    change), and a detail panel for the selected row.

    `render_detail(pos, scfg, provider, market_open, config_paper, spot)` renders
    that panel; it defaults to the close builder, and the Positions tab passes
    one that offers Close *or* Roll. The argument exists because the roll flow
    lives in `tabs/rolls`, which imports this module — the callback keeps the
    dependency pointing one way."""
    import pandas as pd
    from options_scanner.display.spot_meta import fetch_spot_meta

    _hdr, _rf = st.columns([8, 1], vertical_alignment="bottom")
    with _hdr:
        section_header(title="Your Option Positions (Schwab)")
    with _rf:
        if st.button("🔄", key="opt_pos_refresh",
                     help="Re-fetch positions, quotes, order status, and spot."):
            _option_positions.clear()
            _close_quote.clear()
            _order_status.clear()
            fetch_spot_meta.clear()
            # Full rerun, not the implicit fragment one — the top-bar Schwab
            # token countdown and run_app's token-mtime cache invalidation are
            # script-level, so 🔄 after a re-auth must re-run the whole app or
            # the toggle keeps reading "Schwab (expired)".
            st.rerun()

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
    positions, _hidden = settings_ui.filter_hidden(positions, scope="positions")
    if not positions:
        st.caption(f"All {_held_n} of your option positions are hidden by your "
                   f"⚙️ Settings — nothing to show.")
        # Still render the notice: it carries the toggle that reveals them.
        settings_ui.render_hidden_notice(_hidden, scope="positions")
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
        # Intrinsic + time value per share, and the time value annualized over
        # the capital the position ties up (see yield_base — net liquidation for
        # a covered call, the cash collateral for a put). Time value is computed
        # independently of DTE so a position expiring today still shows what's
        # left; only Ann% needs days to annualize over.
        _intr = _intrinsic_value(_spot, _strike, _opt)
        _tv = _time_value(_mark, _spot, _strike, _opt)
        _ann = None
        if _tv is not None and _dte_v and _dte_v > 0:
            _base = yield_base(_spot, _strike, _opt, _mark,
                               covered=trade_actions.is_unwindable(p))
            if _base:
                _ann = _tv / _base * (365.0 / _dte_v) * 100.0
        _delta = (quotes.get(_pos_pkey(p)) or {}).get("delta")
        # One trade-log lookup, two uses: the "tracked" tag, and how long the
        # leg has been open (the broker reports neither). A position opened
        # outside the scanner has no record and simply shows its DTE alone.
        _trec = _tracked_open_leg(_tkr, _opt, _strike, p.get("expiration", ""))
        _tracked = _trec is not None
        _days_open = days_since(_trec.get("opened_at")) if _trec else None
        # Open cell: what the UNDERLYING cost when the position was opened, then
        # what the option itself opened at. The stock figure is only ever
        # recorded by this app (fill_spot at the fill), so "—" holds its slot for
        # a leg opened elsewhere and the option's price stays on the right.
        _open_cell = open_prices_cell((_trec or {}).get("fill_spot"),
                                      p.get("avg_price"))
        _tags = ([("covered" if p.get("covered") else "naked")]
                 if _opt == "C" and _dir == "short" else [])
        if _tracked:
            _tags.append("tracked")
        _spotday, _pct = _spot_day(_tkr)
        rows.append({
            "Ticker": _tkr,
            # Second column: what the row sorts by and what the shade says, so
            # it reads with the ticker. Raw float — the column sorts by depth.
            "ITM%": signed_moneyness(_spot, _strike, _opt),
            "Spot/Day%": _spotday,
            "_pct": _pct,
            "_opt": _opt,   # hidden — splits the rows into the two tables
            # Just the direction: puts and calls have their own tables, so the
            # right is in the table heading rather than repeated on every row.
            "Type": "Short" if _dir == "short" else "Long",
            "Strike": _strike,
            "Exp": p.get("expiration", ""),
            "DTE": dte_cell(_dte_v, _days_open),
            "Qty": _qty,
            "Delta": (round(_delta, 2) if _delta is not None else None),
            "Ann%": (round(_ann, 1) if _ann is not None else None),
            # What the leg's value is made of, split whole-leg the same way Mkt
            # Val beside it is: exercise value vs what's left to decay. Text,
            # since it carries two figures.
            "Intrinsic | Time": (
                f"${_intr * 100 * _qty:,.0f} | ${_tv * 100 * _qty:,.0f}"
                if (_intr is not None and _tv is not None) else "—"),
            # Left of Mkt Val so the row reads open → worth now → P/L.
            "Open$": _open_cell,
            "Mkt Val": _signed_value(_mv, _qty, _dir),
            "P/L": _position_pl(p.get("avg_price"), _mark, _qty, _dir),
            "Note": " · ".join(_tags),
        })
    disp = pd.DataFrame(rows)
    # What you have to close with. Buying back a short is a debit, so this is
    # the number that decides whether a close is affordable — shared 60s cache
    # with the Sell dialog, so it costs no extra round-trip.
    render_buying_power_caption(scfg, "Available to close", "table")
    st.caption("🔍 **Select a position** to close it (all or part) or roll it.")
    st.caption("view-only in paper mode." if config_paper
               else "**🔴 LIVE** (buy-to-close for short legs, sell-to-close "
                    "for long).")
    _col_cfg = {
            # Uncolored on purpose: the row shade already carries how far
            # through the money the leg is, and green/red text would fight it —
            # deeper ITM is bad for a short and good for a long, so no single
            # color rule fits a table holding both.
            "ITM%": st.column_config.NumberColumn(
                "ITM%", format="%+.1f%%", width=85, help=MONEYNESS_HELP),
            "Spot/Day%": st.column_config.TextColumn(
                "Spot/Day%", width=130,
                help="Underlying spot and today's change (green up / red down)."),
            "_pct": None,
            "_opt": None,
            "Type": st.column_config.TextColumn(
                "Direction", width=85,
                help="Short = you sold it (closing buys it back). Long = you "
                     "bought it (closing sells it)."),
            "Strike": st.column_config.NumberColumn("Strike", format="$%.2f"),
            "Delta": st.column_config.NumberColumn("Delta", format="%.2f"),
            "Ann%": st.column_config.NumberColumn(
                "Ann%", format="%.1f%%",
                help="Annualized return on the option's remaining time value "
                     "(extrinsic), over the capital the position ties up: a "
                     "covered call against its net liquidation value (spot "
                     "minus the call — what unwinding would free), a put "
                     "against the cash securing it (strike). Low = little "
                     "premium left to decay, a cue to close or unwind."),
            "Intrinsic | Time": st.column_config.TextColumn(
                "Intrinsic | Time", width=140,
                help="What the leg's value is made of, whole-leg (× 100 × "
                     "contracts): intrinsic = worth on exercise alone; time = "
                     "the extrinsic left. A deep ITM leg is nearly all "
                     "intrinsic — nothing left to decay, so holding it only "
                     "carries assignment risk."),
            # Label spells out both slots — two dollar figures in one cell are
            # otherwise ambiguous. Key stays "Open$" to match the frame column.
            "Open$": st.column_config.TextColumn(
                "Open (stock · option)", width=160,
                help="What the underlying was trading at when the position was "
                     "opened, then the option's own opening price per share. "
                     "The stock figure is recorded when a trade is placed here, "
                     "so it reads '—' for legs opened outside the scanner; the "
                     "option figure is the broker's average price."),
            "Mkt Val": st.column_config.NumberColumn(
                "Mkt Val", format="$%.0f",
                help="Current market value of the whole leg, as the broker "
                     "carries it: negative for a short (a liability — what it "
                     "costs to buy back), positive for a long."),
            "P/L": st.column_config.NumberColumn(
                "P/L", format="$%+,.0f",
                help="Unrealized P/L on the leg, against the broker's average "
                     "open price. Short: premium collected − cost to buy back. "
                     "Long: value now − what you paid. Positive is in your "
                     "favor either way."),
            # Text, not a number: it carries the days-open parenthetical. The
            # label spells that out so the second figure doesn't read as a
            # second DTE.
            "DTE": st.column_config.TextColumn(
                "DTE (days open)", width=140,
                help="Days to expiration, and — in parentheses — how many days "
                     "ago the position was opened. The open date comes from the "
                     "app's trade log, so the parens appear only for legs "
                     "opened through the scanner; anything opened elsewhere "
                     "shows the DTE alone."),
    }

    # Puts and calls in their own tables, puts first — they're different trades
    # (a short put is cash-secured, a short call is covered by shares), and
    # splitting them keeps the sort within each kind. Each table maps its own
    # row index back to its own slice of `positions`, so a selection can't point
    # at the wrong leg.
    # Selections are collected here and rendered *after* the color key below,
    # so opening a detail panel doesn't shove the key down under it — the key
    # explains the shading on the tables, so it belongs with them.
    _pending: list[tuple] = []
    for _label, _is_call, _key in (("Puts", False, "opt_pos_put"),
                                   ("Calls", True, "opt_pos_call")):
        _subset, _sub = _split_by_right(positions, disp, _is_call)
        if not _subset:
            continue
        st.markdown(f"**{_label}** ({len(_subset)})")
        # Furthest out of the money first, rows shaded by band — same reading as
        # the roll builder expects. Both halves of the split are reordered together
        # so a click still selects the leg on that row.
        _subset, _sub = sort_by_moneyness(_subset, _sub)
        _styled = (_sub.style
                   .apply(lambda r: [moneyness_bg(_sub.loc[r.name, "ITM%"])]
                          * len(r), axis=1)
                   .apply(lambda s: [_sign_color(_sub.loc[i, "_pct"])
                                     for i in s.index], subset=["Spot/Day%"])
                   .map(_sign_color, subset=["P/L"]))
        event = st.dataframe(
            _styled, hide_index=True, width="stretch", on_select="rerun",
            selection_mode="single-row", key=_key,
            height=df_height(_styled), column_config=_col_cfg)
        sel = event.selection.rows if hasattr(event, "selection") else []
        _scroll_guard = f"_opt_pos_scroll_{_key}"
        if not sel:
            st.session_state.pop(_scroll_guard, None)
            continue
        _fresh = st.session_state.get(_scroll_guard) != sel[0]
        if _fresh:
            st.session_state[_scroll_guard] = sel[0]
        # Reuse the spot already fetched for the table above (same cached
        # source), so the close panel's quote line can't disagree with the row.
        _sel_pos = _subset[sel[0]]
        _sel_spot = (meta.get(str(_sel_pos.get("underlying", ""))) or {}).get(
            "spot")
        _pending.append((_sel_pos, _sel_spot, _fresh))

    # Color key for the moneyness row shading — under both tables and ABOVE any
    # detail panel, so selecting a row can't push the key out of sight.
    moneyness_legend()

    for _sel_pos, _sel_spot, _fresh in _pending:
        # Scroll fires here rather than back at the table: the component scrolls
        # its OWN iframe into view, so it has to sit at the top of the thing
        # it's bringing on screen. Left at the detection site it would aim at a
        # point above the key instead of at the panel.
        if _fresh:
            _scroll_into_view()
        st.markdown("---")
        if render_detail is None:
            _render_option_close(_sel_pos, scfg, market_open, config_paper,
                                 _sel_spot, provider)
        else:
            render_detail(_sel_pos, scfg, provider, market_open, config_paper,
                          _sel_spot)
        st.markdown("---")

    # Hidden-position note last, so it never pushes the table down.
    settings_ui.render_hidden_notice(_hidden, scope="positions")


def _trades_context() -> tuple:
    """Per-render context shared by the Trades and Positions tabs:
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
    # The "Trades" tab: the scanner-trades list only. Live Schwab option legs
    # live on the "Positions" tab (tabs/rolls.tab_positions, which renders
    # _render_option_positions below). _scanner_trades is a fragment, so a
    # trade-row toggle or the 🔄 reruns just this list, not the app.
    provider, scfg, market_open = _trades_context()
    _scanner_trades(provider, scfg, market_open)


@st.fragment
def _scanner_trades(provider: str, scfg: dict, market_open) -> None:
    # A fragment so trade-row toggles rerun just this list. The toggle
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

    # Is the broker actually reachable this render? Everything below that reads
    # or writes at Schwab is gated on this, and an expanded row says so rather
    # than silently showing dashes.
    _schwab_live = provider == "schwab" and bool(scfg.get("app_key"))

    # Settle any working roll before the list is read. Imported late: rolls.py
    # imports from this module, so a top-level import would be circular.
    if _schwab_live:
        from options_scanner.tabs.rolls import reconcile_rolls
        for _note in reconcile_rolls(scfg):
            st.info(_note)

    # In-flight rolls (status "rolling") are listed here too, so a placed roll
    # can be watched through to its fill in the same place as everything else —
    # they used to be filtered out and visible only on the old Roll tab.
    trades = trades_store.load()
    if not trades:
        section_header(title=f"Trades made from Scanner{_mode_badge}")
        st.info(
            "No trades yet. Put-sells you place from the **Watchlist** "
            "leaderboard's *Sell Put* dialog appear here with live P/L and a "
            "cost-to-close estimate, and can be closed from here."
        )
        return

    # Title (with a small, muted trade count) + a compact 🔄 refresh on one
    # row. Refresh clears the cached status/quote/spot fetches so the rerun
    # re-pulls them — e.g. to catch a fill — instead of taking its own row.
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
            # Full rerun — see the Positions 🔄 above: the top-bar token
            # countdown only recomputes at script level.
            st.rerun()

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
    roll_close_quote_by_id: dict = {}   # in-flight roll: the leg being closed
    roll_status_by_id: dict = {}        # in-flight roll: its net order status
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

        def _roll_close_quote_job(tr):
            # The leg a roll is buying back — the record itself describes the
            # NEW leg, so this one comes out of roll_from.
            rf = tr.get("roll_from") or {}
            return _close_quote(_ak, _as, _cb, _tf, tr.get("ticker"),
                                rf.get("expiration", ""),
                                float(rf.get("strike", 0) or 0),
                                rf.get("option_type", tr.get("option_type", "P")))

        def _roll_status_job(tr):
            return _order_status(_ak, _as, _cb, _tf, tr.get("roll_order_id"),
                                 (tr.get("account") or "")[-4:])

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
                    and tr.get("status") in ("open", "closing", "rolling")):
                _spot_seen.add(_stk)
                jobs.append(("spot", _stk, tr))
            _expanded = st.session_state.get(f"trade_open_{tr.get('id')}", False)
            # Opening-order status. Polled even while COLLAPSED when the order is
            # unresolved — otherwise the header calls a still-working order
            # "open", which is what it says once a fill is confirmed. A fill is
            # recorded on the trade (filled_at, below) the first time we see it,
            # so a filled position never needs this read again: only genuinely
            # in-flight orders keep costing a round-trip. Expanded rows still
            # read it either way, since the broker line there shows fill details.
            if (not tr.get("paper") and tr.get("order_id")
                    and tr.get("status") == "open"
                    and (not tr.get("filled_at") or _expanded)):
                jobs.append(("status", tr.get("id"), tr))
            # Deferred load: a collapsed trade fetches nothing else. Its
            # remaining Schwab reads run only once the user opens its row (the
            # toggle header below sets this flag, then reruns so this loop picks
            # the trade up).
            if not _expanded:
                continue
            # A working closing order is polled via close_order_id instead.
            if (not tr.get("paper") and tr.get("close_order_id")
                    and tr.get("status") == "closing"):
                jobs.append(("close_status", tr.get("id"), tr))
            # An in-flight roll needs BOTH legs quoted (the net it would fill at
            # is the difference) plus its own order status.
            if tr.get("status") == "rolling":
                jobs.append(("roll_close_quote", tr.get("id"), tr))
                jobs.append(("roll_status", tr.get("id"), tr))
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
                        "spot": _spot_job,
                        "roll_close_quote": _roll_close_quote_job,
                        "roll_status": _roll_status_job}
            _job_maps = {"status": status_by_id,
                         "close_status": close_status_by_id,
                         "quote": quote_by_id,
                         "chart": chart_by_ticker,
                         "spot": spot_by_ticker,
                         "roll_close_quote": roll_close_quote_by_id,
                         "roll_status": roll_status_by_id}
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
        # Record the fill the first time we see it. That makes "did this order
        # fill?" a durable fact on the trade instead of an answer that needs a
        # Schwab read every time the tab loads — which is what lets the prefetch
        # above stop polling resolved orders, and what lets a collapsed row say
        # "open" and mean it.
        if filled and not t.get("filled_at"):
            _fat = bs.get("filled_at")
            _fat_iso = (_fat.isoformat(timespec="seconds")
                        if hasattr(_fat, "isoformat")
                        else (str(_fat) if _fat else
                              datetime.now().isoformat(timespec="seconds")))
            trades_store.update(t["id"], filled_at=_fat_iso)
            t["filled_at"] = _fat_iso
        _disp_status = _display_status(
            _store_status, bs.get("status") if bs else None,
            filled or bool(t.get("filled_at")))
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
        if _store_status == "rolling":
            # A roll is two legs, so the header names both — "$150 → $160" reads
            # as the move, where a single strike would look like a plain
            # position and hide what's actually in flight.
            _rf = t.get("roll_from") or {}
            label = (f"{t.get('ticker', '?')} {_otw} "
                     f"\\${_rf.get('strike', '?')} → \\${t.get('strike', '?')} "
                     f"— {exp_disp} · {qty}x · rolling"
                     + (f" · {_spot_seg}" if _spot_seg else "")
                     + ("  ·  📝 PAPER" if t.get("paper") else "  ·  🔴 LIVE"))
        else:
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
        # An in-flight roll is its own thing: two legs moving as one net order,
        # with no position to show P/L on until it fills. Render the shared
        # monitor (both legs' quotes, net now vs your limit, broker status) and
        # skip the single-leg position body entirely.
        if _store_status == "rolling":
            if is_open:
                from options_scanner.tabs.rolls import (render_roll_monitor,
                                                        _cancel_roll)
                render_roll_monitor(t, roll_close_quote_by_id.get(t.get("id")),
                                    quote_by_id.get(t.get("id")),
                                    roll_status_by_id.get(t.get("id")))
                _rbs = roll_status_by_id.get(t.get("id"))
                _rk = f"roll_cancel_result_{t['id']}"
                _rc1, _rc2, _ = st.columns([2, 2, 3])
                with _rc1:
                    _cancelable = bool(_rbs and _rbs.get("cancelable"))
                    if st.button(
                            "Cancel roll", key=f"trades_cancel_roll_{t['id']}",
                            disabled=not _cancelable, width="stretch",
                            help=("Cancels the unfilled net order; your current "
                                  "position stays as it is." if _cancelable else
                                  "Only a working order can be canceled — hit "
                                  "🔄 to re-check.")):
                        st.session_state[_rk] = _cancel_roll(scfg, t)
                        _order_status.clear()
                        st.rerun()
                with _rc2:
                    _rmbox = st.container(key=f"rm_box_{t['id']}")
                    _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                                  on_click=trades_store.remove,
                                  args=(t["id"],), width="stretch")
                _rres = st.session_state.get(_rk)
                if _rres:
                    (st.success if _rres["ok"] else st.error)(
                        _rres["msg"].replace("$", "\\$"))
            continue

        if is_open:
            # Not on Schwab → say so up front. Everything broker-side is
            # unavailable (re-quote, P/L, order status, closing), so without
            # this the row is a wall of dashes with no stated reason — and the
            # fallback caption below would blame an unreadable order status for
            # what is really just a disconnected broker.
            if not _schwab_live:
                if st.session_state.get("_schwab_configured"):
                    st.info(
                        "📊 Reading from **"
                        + ("Yahoo Finance" if provider == "yahoo" else "Moomoo")
                        + "**. Live cost-to-close, P/L, order status and "
                        "closing are Schwab-only — switch the data source to "
                        "**Schwab** in the top bar to manage this trade at "
                        "your broker.")
                else:
                    st.info(
                        "📊 No broker connected. This record is tracked "
                        "locally; live cost-to-close, order status and closing "
                        "need Schwab — add your credentials to config.toml and "
                        "run `schwab_auth.py`.")
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
                # Annualized yield over the capital the position ties up: a
                # covered call against its net liquidation value (spot − mark,
                # what unwinding would free), a cash-secured put against its
                # strike. Every trade placed from the scanner's call dialog is
                # share-covered by construction, so `covered` follows the right.
                _ann_base = yield_base(_spot, _strike,
                                       "C" if _is_call else "P",
                                       q.get("mid"), covered=_is_call)
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
                        "to manage it — applies on your next click here, no "
                        "restart needed.")
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
                        st.markdown(kv_table_html(_terms),
                                    unsafe_allow_html=True)
                    with _s2:
                        st.markdown(kv_table_html(_prices),
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
                # Positions tab. A note means it ended without fully filling.
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
                    st.caption(f"Cost to close at this limit: "
                               f"**{money_md(close_limit * 100 * close_n)}**")
                else:
                    st.error(_input_err)
                # Only for a close that actually spends money. A paper close
                # books a simulated result and sends nothing, so the account's
                # balances have no bearing on it.
                if close_live:
                    render_buying_power_caption(
                        scfg, "Account", f"trade_{t['id']}")

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
            elif t.get("status") == "open" and _schwab_live:
                # Live order, but its broker status couldn't be read — can't
                # tell working vs filled, so offer neither action automatically.
                # Only when Schwab IS connected: off-broker, the notice at the
                # top of the row already explains the blanks, and blaming the
                # order status would point at the wrong thing.
                st.caption("Order status unavailable — verify at your broker; "
                           "use **Remove from tracker** if it didn't fill.")

            # Else it already sits next to Cancel / Place Closing Trade above.
            if not _cancel_branch and not _close_branch:
                _rmbox = st.container(key=f"rm_box_{t['id']}")
                _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                              on_click=trades_store.remove, args=(t["id"],))

    st.caption("Estimates use a live Schwab mid; verify at your broker.")
