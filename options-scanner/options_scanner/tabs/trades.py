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

from options_scanner import trade_actions, trades_store
from options_scanner.ui_theme import metric_card, section_header

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


@st.cache_data(ttl=30, show_spinner=False)
def _close_quote(app_key: str, app_secret: str, callback_url: str,
                 token_file: str, ticker: str, expiration: str,
                 strike: float) -> dict | None:
    """Cached (30s) read-only re-quote for one put. Returns dict or None."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(app_key, app_secret, callback_url, token_file)
    except Exception:
        return None
    return trade_actions.requote_put(client, ticker, expiration, strike)


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
        return {"ok": True,
                "msg": (f"Close recorded in the tracker ({close_qty}{_of} "
                        f"contract(s), debit ${debit:,.0f}). No live order "
                        "sent.")}
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
    return {"ok": True,
            "msg": (f"✅ LIVE closing order sent to {mask}{_oid} ({close_qty}"
                    f"{_of} contract(s)). It will show as **closing** here "
                    "until it fills — cancel it from this tab if needed. Verify "
                    "at your broker.")}


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


@st.fragment
def tab_trades() -> None:
    # A fragment so Cancel / Remove / close actions rerun only this tab, not
    # the whole app — otherwise the full rerun re-runs run_app's st.tabs and
    # snaps the view back to the first tab.
    provider = st.session_state.get("data_source", "yahoo")
    scfg = st.session_state.get("schwab_config") or {}
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
    # Market-hours gate for live closing (None = unknown → fail safe).
    market_open = (_market_open(scfg.get("app_key", ""),
                                scfg.get("app_secret", ""),
                                scfg.get("callback_url", ""),
                                scfg.get("token_file", ""))
                   if (provider == "schwab" and scfg.get("app_key")) else None)

    trades = trades_store.load()
    if not trades:
        section_header(title=f"Trades{_mode_badge}")
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
        section_header(title=f"Trades{_mode_badge}{_count}")
    with _tr:
        if st.button("🔄", key="trades_refresh",
                     help="Re-fetch order status, quotes, and spot."):
            _order_status.clear()
            _close_quote.clear()
            from options_scanner.display.spot_meta import fetch_spot_meta
            fetch_spot_meta.clear()

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
                                float(tr.get("strike", 0)))

        jobs = []  # (kind, trade_id, trade)
        for tr in trades:
            # Opening-order status only matters while the trade is still open;
            # a working closing order is polled via close_order_id instead.
            if (not tr.get("paper") and tr.get("order_id")
                    and tr.get("status") == "open"):
                jobs.append(("status", tr.get("id"), tr))
            if (not tr.get("paper") and tr.get("close_order_id")
                    and tr.get("status") == "closing"):
                jobs.append(("close_status", tr.get("id"), tr))
            jobs.append(("quote", tr.get("id"), tr))

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
                        "quote": _quote_job}
            _job_maps = {"status": status_by_id,
                         "close_status": close_status_by_id,
                         "quote": quote_by_id}
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
        label = (f"{t.get('ticker', '?')} ${t.get('strike', '?')} {_otw} — "
                 f"{exp_disp} · {qty}x · {_disp_status}"
                 + ("  ·  📝 PAPER" if t.get("paper") else "  ·  🔴 LIVE"))

        with st.expander(label, expanded=False):
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
                try:
                    _dte = (datetime.strptime(exp, "%Y-%m-%d").date()
                            - datetime.now().date()).days
                except Exception:
                    _dte = None
                _iv, _delta = q.get("iv"), q.get("delta")
                _ann = (q["mid"] / _strike * (365.0 / _dte) * 100.0
                        if (_dte and _dte > 0 and _strike and q.get("mid"))
                        else None)
                # Underlying spot + day-change % (one cached fetch_spot_meta
                # call) for the Spot row under Vol.
                from options_scanner.display.spot_meta import fetch_spot_meta
                try:
                    _meta = fetch_spot_meta(str(t.get("ticker", "")), provider)
                except Exception:
                    _meta = {}
                _spot, _spct = _meta.get("spot"), _meta.get("pct_change")

                # Delta cell: live value, plus the fill-time delta once captured.
                _delta_cell = f"{_delta:.2f}" if _delta is not None else "—"
                if t.get("fill_delta") is not None:
                    _delta_cell += ("<span style='color:#94a3b8'> · fill "
                                    f"{float(t['fill_delta']):.2f}</span>")
                # DTE cell: days remaining, plus how long the position has been
                # open once we know when it opened (mirrors the fill
                # annotations on Delta/Spot).
                _dte_cell = str(_dte) if _dte is not None else "—"
                _opened = t.get("opened_at")
                if _opened:
                    try:
                        _days_open = (datetime.now()
                                      - datetime.fromisoformat(_opened)).days
                        _dte_cell += ("<span style='color:#94a3b8'> · open "
                                      f"{_days_open}d</span>")
                    except Exception:
                        pass
                _terms = [
                    ("Type", "Put"), ("Strike", f"${_strike:g}"),
                    ("Expir", exp_disp),
                    ("DTE", _dte_cell),
                    ("IV", f"{_iv * 100:.1f}%" if _iv else "—"),
                    ("Delta", _delta_cell),
                    ("Ann%", f"{_ann:.1f}%" if _ann is not None else "—"),
                ]
                # Spot cell: live value, plus the fill-time spot once captured.
                if _spot is not None:
                    _spot_cell = f"${_spot:,.2f}"
                    if _spct is not None:
                        _spot_cell += f", {_spct:+.1f}%"
                else:
                    _spot_cell = "—"
                if t.get("fill_spot") is not None:
                    _spot_cell += ("<span style='color:#94a3b8'> · fill "
                                   f"${float(t['fill_spot']):,.2f}</span>")
                _prices = [
                    ("Bid", f"${float(q.get('bid', 0)):,.2f}"),
                    ("Ask", f"${float(q.get('ask', 0)):,.2f}"),
                    ("Mid", f"${float(q.get('mid', 0)):,.2f}"),
                    ("Last", f"${float(q.get('last', 0)):,.2f}"),
                    ("OI", f"{q.get('open_interest', 0):,}"),
                    ("Vol", f"{q.get('volume', 0):,}"),
                    ("Spot", _spot_cell),
                ]

            def _render_cards(cols):
                with cols[0]:
                    metric_card("CREDIT RECEIVED", f"${total_credit:,.0f}",
                                delta=f"${credit_ps:.2f}/sh", delta_sign="neutral")
                with cols[1]:
                    if close_mid is not None:
                        metric_card("COST TO CLOSE",
                                    f"${close_mid * 100 * qty:,.0f}",
                                    delta=f"${close_mid:.2f}/sh",
                                    delta_sign="neutral")
                    else:
                        metric_card("COST TO CLOSE", "—",
                                    delta="re-quote unavailable",
                                    delta_sign="neutral")
                with cols[2]:
                    if close_mid is not None:
                        _close_cost = close_mid * 100 * qty
                        pnl = total_credit - _close_cost
                        # Formula on a small line just above the value: credit
                        # received − cost to close (both at the mid).
                        _pl_label = (
                            "UNREALIZED P/L<br><span style='font-weight:400;"
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
                        metric_card("UNREALIZED P/L", "—")
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
                with cols[0]:
                    metric_card("CREDIT IF FILLED", f"${total_credit:,.0f}",
                                delta=f"${credit_ps:.2f}/sh", delta_sign="neutral")
                with cols[1]:
                    metric_card("COLLATERAL", f"${_strike_v * 100 * qty:,.0f}",
                                delta=f"${_strike_v:g} × 100 × {qty}",
                                delta_sign="neutral")
                with cols[3]:
                    metric_card("STATUS", _disp_status.upper())

            # Open position: details (two columns) left, cards as a 2x2 grid
            # right. No snapshot (closed/canceled): cards span full width. A
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
                with _cards_col:
                    _row1 = st.columns(2)
                    _row2 = st.columns(2)
                    _card_fn([_row1[0], _row1[1], _row2[0], _row2[1]])
            else:
                _card_fn(st.columns(4))

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
                if cbs and cbs.get("status") == "FILLED":
                    # Book the close — full → mark closed; partial → split off a
                    # closed record and keep the remainder open — at the true
                    # average execution price (falling back to the limit), then
                    # rerun to render the result cleanly.
                    _cat = cbs.get("filled_at")
                    _fill_px = cbs.get("fill_price")
                    _cost = round(_fill_px, 2) if _fill_px is not None else _lim
                    _book_close(t, int(cbs.get("filled") or _cqty), _cost,
                                (_cat.isoformat() if _cat
                                 else datetime.now().isoformat(
                                     timespec="seconds")))
                    st.rerun(scope="fragment")
                _cstat = cbs.get("status") if cbs else None
                _close_working = bool(cbs and cbs.get("cancelable"))
                # Terminal but not FILLED → the buy-to-close never (fully)
                # executed (a day order that EXPIRED at the close, or was
                # CANCELED/REJECTED at the broker). Don't leave the trade stuck
                # in "closing": book any filled contracts and return the rest to
                # a normal open position automatically, with a one-time note.
                if cbs and not _close_working and _cstat != "FILLED":
                    _filled_n = int(float(cbs.get("filled") or 0))
                    if _filled_n > 0:
                        # Partial fill then terminal: book the filled contracts
                        # and keep the remainder open (a split, like a partial
                        # close).
                        _fill_px = cbs.get("fill_price")
                        _cost = (round(_fill_px, 2) if _fill_px is not None
                                 else _lim)
                        _cat = cbs.get("filled_at")
                        _book_close(t, _filled_n, _cost,
                                    (_cat.isoformat() if _cat
                                     else datetime.now().isoformat(
                                         timespec="seconds")))
                        _note = (f"Closing order {_cstat} after filling "
                                 f"{_filled_n} of {qty} — the rest is open "
                                 "again.")
                    else:
                        # Nothing filled — revert the whole position to open.
                        trades_store.update(t["id"], status="open",
                                            close_order_id=None,
                                            close_limit_px=None, close_qty=None)
                        _note = (f"Closing order {_cstat} without filling — the "
                                 "position is open again; place a new closing "
                                 "order when ready.")
                    st.session_state[f"close_note_{t['id']}"] = _note
                    st.session_state.pop(f"close_result_{t['id']}", None)
                    _order_status.clear()
                    st.rerun(scope="fragment")

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
                        st.rerun(scope="fragment")
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

            # Broker order status (`bs`) was fetched above for the title;
            # working / filled / is_paper were derived from it near the top.
            if bs is not None:
                if filled:
                    # filledQuantity / quantity = contracts filled of ordered
                    # (Schwab returns them as floats).
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

            # While unfilled → Cancel (a live working order, or a paper sim that
            # never reaches a broker). Place Closing Trade appears only once the
            # order is confirmed FILLED.
            _cancel_branch = (t.get("status") == "open"
                              and (working or is_paper))
            _close_branch = (t.get("status") == "open" and filled)
            if _cancel_branch:
                _crk = f"cancel_result_{t['id']}"
                _clabel = ("Cancel working order" if working
                           else "Cancel (discard paper trade)")
                _chelp = ("Cancels the unfilled order at the broker — no "
                          "position changes." if working
                          else "Discards this simulated trade.")
                # Equal-width, adjacent on the left (spacer column on the right).
                _ac1, _ac2, _ = st.columns([2, 2, 3])
                with _ac1:
                    if st.button(_clabel, key=f"cancel_ord_{t['id']}",
                                 help=_chelp, width="stretch"):
                        if working:
                            st.session_state[_crk] = _cancel_order(scfg, t)
                            _order_status.clear()
                        else:  # paper — no broker order, discard the record
                            trades_store.update(
                                t["id"], status="canceled",
                                canceled_at=datetime.now().isoformat(
                                    timespec="seconds"))
                            st.session_state[_crk] = {
                                "ok": True, "msg": "Paper trade canceled."}
                        # Re-run the fragment so the status reflects it now.
                        st.rerun(scope="fragment")
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
                # A live position must be closed with a live order; a paper
                # trade is closed in the tracker only. Live closing obeys the
                # same config paper-arm switch + market-hours gate as opening.
                trade_live = not bool(t.get("paper"))
                config_paper = bool(scfg.get("paper", True))
                close_live = trade_live and not config_paper
                # A real (live) position viewed while the app is in paper mode:
                # it can't be paper-closed (that would mark a still-open broker
                # position "closed" in the tracker), so flag it and block below.
                _live_in_paper = trade_live and config_paper
                # Badge reflects what the close would do; a live position in
                # paper mode is LIVE (and blocked), never "paper".
                _close_badge = ("🔴 LIVE" if (close_live or _live_in_paper)
                                else "📝 PAPER")

                # Mode + market gate ABOVE the button so they clearly describe
                # the close (not the Remove button that follows below).
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

                # Inputs row: close limit + how many contracts to buy back —
                # always shown (locked at 1 for a single-lot) so the count is
                # never in doubt. Buttons sit on the row below.
                _il, _if, _ql, _qf, _ = st.columns(
                    [1, 1.2, 1.3, 0.9, 1.6], vertical_alignment="center")
                with _il:
                    st.markdown("Close limit")
                with _if:
                    _clbox = st.container(key=f"close_box_{t['id']}")
                    close_limit = _clbox.number_input(
                        "Close limit",
                        min_value=float(trade_actions.tick_for(default_close)),
                        step=float(trade_actions.tick_for(default_close)),
                        format="%.2f", key=_wid_key,
                        label_visibility="collapsed",
                    )
                # Seed via session_state (and re-clamp if a prior partial close
                # shrank the position below the remembered count) so the keyed
                # widget doesn't error on a stale over-max value.
                _qk = f"close_qty_{t['id']}"
                if st.session_state.get(_qk) is None or \
                        st.session_state[_qk] > qty:
                    st.session_state[_qk] = qty
                with _ql:
                    st.markdown(f"Contracts (of {qty})")
                with _qf:
                    close_n = int(st.number_input(
                        "Contracts", min_value=1, max_value=qty, step=1,
                        format="%d", key=_qk, label_visibility="collapsed",
                        disabled=(qty == 1)))

                _bc, _rc, _ = st.columns([2, 2, 1], vertical_alignment="center")
                with _bc:
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
                    elif st.button(f"Confirm Closing Trade · {_close_badge}",
                                   key=f"close_btn_{t['id']}", width="stretch",
                                   type="primary"):
                        st.session_state[_confirm_key] = True
                        st.session_state.pop(_result_key, None)
                with _rc:
                    _rmbox = st.container(key=f"rm_box_{t['id']}")
                    _rmbox.button("Remove from Tracker", key=f"rm_{t['id']}",
                                  on_click=trades_store.remove,
                                  args=(t["id"],), width="stretch")

                if st.session_state.get(_confirm_key):
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
                    def _cancel_close(_k=_confirm_key):
                        st.session_state[_k] = False

                    bc1, bc2, _ = st.columns([1, 1, 3])
                    with bc1:
                        _do = st.button(f"Place Closing Trade · {_close_badge}",
                                        key=f"close_do_{t['id']}",
                                        type="primary", width="stretch")
                    with bc2:
                        _cbox = st.container(key=_cancel_box_key)
                        _cbox.button("Cancel", key=f"close_cancel_{t['id']}",
                                     width="stretch", on_click=_cancel_close)
                    if _do:
                        _result = _submit_close(scfg, t, close_limit, close_live,
                                                close_n)
                        st.session_state[_result_key] = _result
                        st.session_state[_confirm_key] = False
                        # A live close moves the trade to "closing" — rerun so it
                        # re-renders with the working-status + Cancel UI instead
                        # of the (now stale) close panel.
                        if _result.get("ok") and close_live:
                            st.rerun(scope="fragment")

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
