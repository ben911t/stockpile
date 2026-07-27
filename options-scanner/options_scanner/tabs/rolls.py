"""Roll tab — assisted rolling of covered calls & cash-secured puts.

Sourced from LIVE Schwab positions: pick a short covered call or short put you
actually hold, choose a new leg from a filtered IV-surface scan, and place the
roll as ONE atomic net-price order (buy-to-close the held leg + sell-to-open the
new one, both fills together). This is the *only* place in the app that executes
a roll — the Portfolio/Single roll views are analysis-only and point here.

Schwab only (positions come from the live account). Because every rollable
position is a REAL position, execution follows the Trades-tab rule: a real
position can't be rolled while the app is in paper mode (that would desync the
tracker from the open broker position), so placement needs ``paper = false`` and
an open market. A working net order shows here as **rolling** and is cancelable;
once it fills the new leg becomes a normal open position on the Trades tab.
See ``expressive-rolling-lampson`` plan.
"""

from __future__ import annotations

import concurrent.futures
import functools
import time
from datetime import date, datetime

import pandas as pd
import streamlit as st

from options_scanner import (
    confirm_gate, positions_cache, settings_ui, trade_actions, trades_store,
)
from options_scanner.fetch import fetch_and_enrich
from options_scanner.format import fmt_strike
from options_scanner.ui_theme import (
    df_height, empty_state, metric_card, render_schwab_reauth_hint,
    section_header,
)
# Reuse the Trades tab's cached read-only Schwab helpers + threading context
# rather than re-implementing them (same pattern the leaderboard uses).
from options_scanner.tabs.trades import (
    _close_quote, _kv_table_html, _market_open, _order_status,
    add_script_run_ctx, get_script_run_ctx,
)

# Max wall-clock for the parallel per-position quote prefetch (delta/mark). A
# slow leg past this leaves that position's delta blank rather than hanging.
_QUOTE_PREFETCH_TIMEOUT_S = 8.0


def _pkey(p: dict) -> tuple:
    """Stable identity for a position leg — keys the prefetched-quote and
    days-opened maps."""
    return (p["underlying"], p["option_type"], float(p["strike"]),
            p["expiration"])


def _prefetch_quotes(positions: list, scfg: dict) -> dict:
    """Parallel per-position live quotes → {pkey: quote|None}, so the table can
    show each leg's current delta without N sequential round-trips. Reuses the
    cached ``_close_quote`` (30s per leg); a slow/hung call leaves that entry
    None (delta blank) rather than blocking the tab. Mirrors the Trades tab."""
    if not positions:
        return {}
    ak, sk = scfg.get("app_key", ""), scfg.get("app_secret", "")
    cb, tf = scfg.get("callback_url", ""), scfg.get("token_file", "")

    def _job(p):
        return _close_quote(ak, sk, cb, tf, p["underlying"], p["expiration"],
                            float(p["strike"]), p["option_type"])

    out: dict = {}
    ctx = get_script_run_ctx()
    init = (functools.partial(add_script_run_ctx, ctx=ctx)
            if add_script_run_ctx is not None else None)
    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(positions)), initializer=init)
    futs = {ex.submit(_job, p): _pkey(p) for p in positions}
    deadline = time.monotonic() + _QUOTE_PREFETCH_TIMEOUT_S
    for fut, k in futs.items():
        try:
            out[k] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            out[k] = None
    ex.shutdown(wait=False, cancel_futures=True)
    return out


def _opened_days_map(positions: list) -> dict:
    """{pkey: days-open} for positions the app has an open trade record for
    (matched by ticker/right/strike/expiration). Positions opened outside the
    app have no record, so they're simply absent — the table then omits the
    '(opened: N)' note for them."""
    out: dict = {}
    for p in positions:
        rec = _find_open_leg(p["underlying"], p["option_type"], p["strike"],
                             p["expiration"])
        opened = rec.get("opened_at") if rec else None
        if not opened:
            continue
        try:
            out[_pkey(p)] = (date.today()
                             - datetime.fromisoformat(opened).date()).days
        except Exception:
            continue
    return out


# Cached (60s) read-only rollable positions (short covered calls + short puts),
# shared with the Close tab and the ⚙️ Settings dialog via positions_cache.
# Aliased so `_rollable(...)` and `_rollable.clear()` read the same as before.
_rollable = positions_cache.rollable


def _dte(expiration: str) -> int | None:
    try:
        return (datetime.strptime(expiration, "%Y-%m-%d").date()
                - date.today()).days
    except Exception:
        return None


def _find_open_leg(ticker: str, option_type: str, strike: float,
                   expiration: str) -> dict | None:
    """The tracked, still-open trade record matching a held leg, or None.

    Used to flip the *old* leg to closed when a roll fills. A position read
    live from Schwab may have no store record (opened outside the app) — that's
    fine, there's simply nothing to close."""
    for t in trades_store.load():
        if (t.get("status") == "open"
                and str(t.get("ticker", "")).upper() == str(ticker).upper()
                and t.get("option_type", "P") == option_type
                and abs(float(t.get("strike", 0)) - float(strike)) < 1e-9
                and str(t.get("expiration", "")) == str(expiration)):
            return t
    return None


def _submit_roll(scfg: dict, roll: "trade_actions.RollOrder", close_mid: float,
                 open_mid: float, account_mask: str | None) -> dict:
    """Place a LIVE net-price roll and record the new leg as "rolling".

    Only reached past the market gate + a config with ``paper = false``. Returns
    {ok, msg}. The new leg is tracked as "rolling" (with the net order id) until
    the Roll tab sees it fill; `roll_from` carries the old leg's identity so it
    can be closed then."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    last4 = (account_mask or "")[-4:]
    resolved = trade_actions.resolve_account_hash(client, last4 or None)
    if not resolved:
        return {"ok": False,
                "msg": "Couldn't resolve the account — roll NOT sent."}
    account_hash, mask = resolved
    res = trade_actions.place_roll_order(client, roll, account_hash)
    if not res["ok"]:
        return {"ok": False, "msg": f"Roll rejected: {res['error']}"}
    trades_store.add({
        "ticker": roll.ticker, "strike": roll.open_strike,
        "expiration": roll.open_expiration, "quantity": roll.quantity,
        "credit": round(open_mid, 2), "option_type": roll.option_type,
        "status": "rolling", "paper": False,
        "roll_order_id": res["order_id"], "roll_net_px": roll.net_limit,
        "roll_from": {"strike": roll.close_strike,
                      "expiration": roll.close_expiration,
                      "option_type": roll.option_type},
        "close_cost_est": round(close_mid, 2), "account": mask,
    })
    _oid = f" (id {res['order_id']})" if res["order_id"] else ""
    # Toast format: headline first, then ONE sentence per line (run_app renders
    # each following line as its own bullet).
    return {"ok": True,
            "msg": (f"✅ LIVE roll sent to {mask}{_oid}\n"
                    f"{roll.describe()}.\n"
                    "Both legs fill together as one net-price order.\n"
                    "It shows as rolling on this tab until it fills.\n"
                    "Once filled, the new leg appears on the Trades tab.\n"
                    "Verify at your broker.")}


def _cancel_roll(scfg: dict, t: dict) -> dict:
    """Cancel a working net-price roll order; discard the "rolling" record.

    The roll never filled, so no position changed — the held leg is untouched.
    Returns {ok, msg}."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    last4 = (t.get("account") or "")[-4:]
    res = trade_actions.cancel_order(client, t.get("roll_order_id"),
                                     last4 or None)
    if not res["ok"]:
        return {"ok": False, "msg": f"Cancel failed: {res['error']}"}
    trades_store.remove(t["id"])
    return {"ok": True,
            "msg": "✅ Roll canceled — your original position is unchanged."}


def _finalize_filled_roll(t: dict, filled_at) -> None:
    """A working roll filled: flip the new leg to a normal open position and
    close the old tracked leg (if any). Mirrors the Trades tab's book-close on a
    filled closing order."""
    now = datetime.now().isoformat(timespec="seconds")
    when = filled_at.isoformat() if filled_at else now
    trades_store.update(t["id"], status="open", rolled_at=when,
                        opened_at=when, order_id=t.get("roll_order_id"))
    rf = t.get("roll_from") or {}
    old = _find_open_leg(t.get("ticker"), rf.get("option_type"),
                         rf.get("strike"), rf.get("expiration"))
    if old:
        trades_store.update(
            old["id"], status="closed",
            close_cost=t.get("close_cost_est"), closed_at=when,
            rolled_into={"strike": t.get("strike"),
                         "expiration": t.get("expiration")})


def _render_pending_rolls(trades: list, scfg: dict) -> None:
    """Working net-price rolls: poll each order, finalize on fill, offer
    Cancel. Rendered above the position list so an in-flight roll is obvious."""
    pending = [t for t in trades if t.get("status") == "rolling"]
    if not pending:
        return
    _ak, _as = scfg.get("app_key", ""), scfg.get("app_secret", "")
    _cb, _tf = scfg.get("callback_url", ""), scfg.get("token_file", "")
    st.markdown("**In-flight rolls**")
    for t in pending:
        _otw = "CALL" if t.get("option_type") == "C" else "PUT"
        rf = t.get("roll_from") or {}
        label = (f"{t.get('ticker', '?')} {_otw} "
                 f"${rf.get('strike', '?')} → ${t.get('strike', '?')} · "
                 "rolling")
        with st.container(border=True):
            st.markdown(f"⏳ **{label}**")
            bs = _order_status(_ak, _as, _cb, _tf, t.get("roll_order_id"),
                               (t.get("account") or "")[-4:])
            if bs and bs.get("status") == "FILLED":
                _finalize_filled_roll(t, bs.get("filled_at"))
                st.rerun()
            _stat = bs.get("status") if bs else None
            _working = bool(bs and bs.get("cancelable"))
            # Terminal but not filled → the roll won't happen; drop the record
            # and leave the original position as it was.
            if bs and not _working and _stat != "FILLED":
                trades_store.remove(t["id"])
                st.session_state[f"roll_note_{t['id']}"] = (
                    f"Roll order {_stat} without filling — your original "
                    "position is unchanged. Place a new roll when ready.")
                _order_status.clear()
                st.rerun()
            _net = t.get("roll_net_px")
            _net_txt = (f" · net {'+' if (_net or 0) >= 0 else '−'}"
                        f"${abs(_net):.2f}" if _net is not None else "")
            if _stat:
                st.caption(f"Broker status **{_stat}**{_net_txt} — both legs "
                           "fill together. Cancel to keep your current "
                           "position, or wait for a fill.")
            else:
                st.caption(f"Order placed{_net_txt}; broker status unavailable "
                           "— hit 🔄 to re-check, or Cancel below.")
            _rk = f"roll_cancel_result_{t['id']}"
            _c1, _c2, _ = st.columns([2, 2, 3])
            with _c1:
                if st.button("Cancel roll", key=f"cancel_roll_{t['id']}",
                             disabled=not _working, width="stretch",
                             help=("Cancels the unfilled net order; your "
                                   "position stays as-is." if _working else
                                   "Only a working order can be canceled — hit "
                                   "🔄 to re-check.")):
                    st.session_state[_rk] = _cancel_roll(scfg, t)
                    _order_status.clear()
                    st.rerun()
            with _c2:
                st.button("Remove from tracker", key=f"rm_roll_{t['id']}",
                          on_click=trades_store.remove, args=(t["id"],),
                          width="stretch")
            _res = st.session_state.get(_rk)
            if _res:
                (st.success if _res["ok"] else st.error)(
                    _res["msg"].replace("$", "\\$"))
    st.markdown("---")


def _positions_table(positions: list, provider: str, quotes: dict,
                     opened: dict) -> pd.DataFrame:
    """Selectable summary of rollable positions.

    The Ticker cell carries the underlying's live spot + day-change. Open$ /
    Mark / P-L come from the Schwab position itself (broker average price +
    market value), NOT the app's trade log — so positions opened outside the
    app read correctly. `quotes` ({pkey: quote}) adds each leg's live delta to
    the Mark cell; `opened` ({pkey: days}) adds days-since-open to the DTE cell
    (both omitted when unavailable)."""
    from options_scanner.display.spot_meta import fetch_spot_meta
    # One cached spot fetch per unique underlying → spot + day % for the label.
    meta: dict = {}
    for tk in {p["underlying"] for p in positions}:
        try:
            meta[tk] = fetch_spot_meta(str(tk), provider)
        except Exception:
            meta[tk] = {}

    def _spot_day(tk: str):
        # (display string, pct value): the Spot/Day% cell shows spot and today's
        # change together; the numeric pct (kept in a hidden column) drives the
        # cell's green/red color — a dataframe cell can't color only part of its
        # text, so spot rides the day-change color too.
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
        qty = int(p.get("quantity", 0) or 0)
        mv = float(p.get("market_value", 0) or 0)
        openp = round(float(p.get("avg_price", 0) or 0), 2)
        mark = abs(mv) / (100 * qty) if qty else None
        # Short premium seller: unrealized P/L = credit collected at open −
        # current cost to buy it back = (open − mark) × 100 × contracts.
        pnl = (openp - mark) * 100 * qty if mark is not None else None
        _spotday, _pct = _spot_day(p["underlying"])
        key = _pkey(p)
        # DTE cell: days-to-expiry, plus days-since-open when the app has a
        # record for this leg.
        _dte_v = _dte(p["expiration"])
        dte_cell = f"{_dte_v}" if _dte_v is not None else "—"
        _days = opened.get(key)
        if _days is not None:
            dte_cell += f" ({_days})"
        # Mark cell: current per-share value, plus the leg's live delta when a
        # quote came back.
        _q = quotes.get(key) or {}
        _delta = _q.get("delta")
        if mark is not None:
            mark_cell = f"${mark:,.2f}"
            if _delta is not None:
                mark_cell += f" (delta: {_delta:.2f})"
        else:
            mark_cell = "—"
        # Signed moneyness as a % of spot: + = ITM, − = OTM. Drives the row
        # shade (None when spot is unavailable → no shade).
        _spot_v = (meta.get(p["underlying"]) or {}).get("spot")
        _itm = None
        if _spot_v and _spot_v == _spot_v and float(_spot_v) > 0:  # not NaN
            _sp = float(_spot_v)
            _diff = ((_sp - float(p["strike"])) if p["option_type"] == "C"
                     else (float(p["strike"]) - _sp))
            _itm = _diff / _sp * 100.0
        rows.append({
            "Ticker": p["underlying"],
            "Spot/Day%": _spotday,
            "_pct": _pct,
            "_itm": _itm,
            "Type": "Call" if p["option_type"] == "C" else "Put",
            "Strike": fmt_strike(p["strike"]),
            "Expiration": datetime.strptime(
                p["expiration"], "%Y-%m-%d").strftime("%b %d '%y"),
            "DTE": dte_cell,
            # Covered calls: open contracts with total shares held in parens;
            # puts have no share coverage, so just the count.
            "Contracts": (
                f"{qty} (shares: {int(p.get('shares_held', 0) or 0):,})"
                if p["option_type"] == "C" else f"{qty}"),
            "Open$": openp,
            "Mark": mark_cell,
            "P/L": round(pnl) if pnl is not None else None,
        })
    return pd.DataFrame(rows)


def _scroll_into_view() -> None:
    """Scroll the roll detail into view after a row selection. Rendered at the
    TOP of the detail; its script scrolls its own iframe (window.frameElement)
    into view, so no injected anchor id is needed — Streamlit's markdown
    sanitizer strips element ids, which is why an anchor-div approach silently
    failed. scrollIntoView finds whatever container actually scrolls;
    scrollMarginTop offsets the fixed header + tab bar. The timeout lets the
    reran layout settle before measuring."""
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


@st.fragment
def _render_roll_detail(pos: dict, scfg: dict, market_open, config_paper: bool,
                        provider: str) -> None:
    """The roll builder for one selected position: close-leg snapshot, a
    filtered target scan (NetCr column), then per-leg snapshots + a net-price
    confirm step.

    Its own fragment (nested inside ``tab_rolls``) so editing a filter reruns
    only this section — the position tables above keep their live quotes, spot
    reads and scroll position instead of re-rendering on every keystroke commit.
    Explicit place/cancel actions still call a full ``st.rerun()``."""
    ticker = pos["underlying"]
    otype = pos["option_type"]        # "C" / "P"
    side = "call" if otype == "C" else "put"
    opt_key = "calls" if otype == "C" else "puts"
    posid = f"{ticker}_{otype}_{pos['strike']:g}_{pos['expiration']}"
    held_qty = int(pos.get("quantity", 1) or 1)

    exp_disp = datetime.strptime(pos["expiration"],
                                 "%Y-%m-%d").strftime("%b %d '%y")
    st.markdown(
        f"### Roll {ticker} ${pos['strike']:g} "
        f"{'Call' if otype == 'C' else 'Put'} — {exp_disp}")

    # Close-leg live snapshot (the held option we'll buy to close).
    q = _close_quote(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                     scfg.get("callback_url", ""), scfg.get("token_file", ""),
                     ticker, pos["expiration"], float(pos["strike"]), otype)
    close_mid = q.get("mid") if q else None

    # Target-scan filters — the same knobs used on the other tabs.
    st.caption("Pick the new leg from the ranked scan below, then confirm the "
               "net-price roll. Both legs execute as one order.")
    # Default Min DTE to the leg we're closing (rolls usually go further out).
    _pos_dte = _dte(pos["expiration"])
    _min_dte_default = max(0, _pos_dte) if _pos_dte is not None else 7
    f1, f2, f3, f4, f5 = st.columns(5)
    with f1:
        min_oi = int(st.number_input("Min OI", min_value=0, value=25, step=25,
                                     key=f"roll_minoi_{posid}"))
    with f2:
        min_vol = int(st.number_input("Min Vol", min_value=0, value=0, step=10,
                                      key=f"roll_minvol_{posid}"))
    with f3:
        min_dte = int(st.number_input("Min DTE", min_value=0,
                                      value=_min_dte_default, step=7,
                                      key=f"roll_mindte_{posid}"))
    with f4:
        # Nullable field (value=None → clearable) seeded to 400 via session
        # state, so it shows a default upper bound but can be cleared to mean
        # "no max". A plain value=400 would make the field non-clearable.
        _mk = f"roll_maxdte_{posid}"
        if _mk not in st.session_state:
            st.session_state[_mk] = 400
        _max_raw = st.number_input("Max DTE", min_value=1, value=None, step=30,
                                   key=_mk,
                                   help="Clear the field for no upper limit.")
        max_dte = int(_max_raw) if _max_raw is not None else None
    with f5:
        d_lo, d_hi = st.slider("|Delta|", 0.0, 1.0, (0.10, 0.90), 0.05,
                               key=f"roll_delta_{posid}")

    # Second filter row: strike range (each empty = unbounded) + net-credit-only.
    # Changing a filter no longer rescans on its own — the Scan button below does.
    g1, g2, g3, _ = st.columns([1, 1, 1.4, 1.6], vertical_alignment="bottom")
    with g1:
        _mins = st.number_input("Min Strike", min_value=0.0, value=None,
                                step=1.0, key=f"roll_minstrike_{posid}",
                                help="Empty = no minimum strike.")
    with g2:
        _maxs = st.number_input("Max Strike", min_value=0.0, value=None,
                                step=1.0, key=f"roll_maxstrike_{posid}",
                                help="Empty = no maximum strike.")
    with g3:
        net_credit_only = st.checkbox(
            "Net credit only", value=False, key=f"roll_netcredit_{posid}",
            help="Only targets whose new premium ≥ cost to close the current "
                 "leg (a net-credit roll).")
    # Gate the (network) target scan behind the Scan button (its own row below
    # the filters, left of the pre-scan hint): run it only when Scan is clicked,
    # then stash the filtered/sorted target set so plain reruns (a filter tweak,
    # a row selection) re-render it without re-scanning. Each scan bumps a
    # generation counter so the results table starts with no stale row selected
    # (the table's widget key includes it).
    _scan_key = f"roll_scan_result_{posid}"
    _bcol, _tcol = st.columns([2, 5], vertical_alignment="center")
    with _bcol:
        _scan_clicked = st.button(
            "🔍 Scan targets", key=f"roll_scan_btn_{posid}", type="primary",
            width="stretch",
            help="Run the target scan with the filters above. Adjusting a "
                 "filter won't rescan until you click this.")
    if _scan_clicked:
        with st.spinner(f"Scanning {ticker} targets…"):
            df, _earn, err = fetch_and_enrich(
                ticker, opt_key, int(min_dte), max_dte, provider=provider,
                schwab_config=scfg)
        if err:
            _res = {"error": f"Couldn't scan {ticker}: {err}"}
        elif df is None or df.empty:
            _res = {"empty": True}
        else:
            _sub = df[(df["type"] == side)
                      & (df["open_interest"] >= min_oi)
                      & (df["volume"] >= min_vol)
                      & (df["delta"].abs().between(d_lo, d_hi))].copy()
            if _mins is not None:
                _sub = _sub[_sub["strike"] >= float(_mins)]
            if _maxs is not None:
                _sub = _sub[_sub["strike"] <= float(_maxs)]
            if net_credit_only and close_mid is not None:
                # Net credit = new-leg mid − cost to close the current leg.
                _sub = _sub[_sub["mid"] >= close_mid]
            _sort = ("signal_score" if "signal_score" in _sub.columns
                     else "iv_excess")
            _sub = _sub.sort_values(
                [_sort, "open_interest"],
                ascending=[False, False]).reset_index(drop=True)
            _res = {"sub": _sub}
        st.session_state[_scan_key] = _res
        st.session_state[f"roll_scangen_{posid}"] = (
            st.session_state.get(f"roll_scangen_{posid}", 0) + 1)
        # Fresh scan → drop any prior confirm-open guard so a re-pick reopens it.
        st.session_state[f"_roll_confirm_guard_{posid}"] = None

    _scan_res = st.session_state.get(_scan_key)
    if not _scan_res:
        with _tcol:
            st.markdown("Set your target filters above, then click **🔍 Scan "
                        "targets** to rank roll candidates.")
        return
    if _scan_res.get("error"):
        st.warning(_scan_res["error"])
        return
    if _scan_res.get("empty"):
        st.info("No chain returned for the target scan.")
        return
    sub = _scan_res["sub"]
    if sub.empty:
        st.info("No target contracts passed those filters — loosen Min OI / Vol "
                "/ delta or the strike range, widen the DTE window, or uncheck "
                "Net credit only, then Scan again.")
        return

    # NetCr = new-leg mid − cost to close the held leg (per share). Shown even
    # when the close re-quote is missing (blank), so the table still ranks.
    net_cr = (sub["mid"] - close_mid) if close_mid is not None else pd.Series(
        [float("nan")] * len(sub), index=sub.index)

    # Last trade price — blank when 0/absent (no print), like the leaderboard.
    _last = (sub["last"].where(sub["last"] > 0) if "last" in sub.columns
             else pd.Series([float("nan")] * len(sub), index=sub.index))
    disp = pd.DataFrame({
        "Strike": sub["strike"].apply(fmt_strike),
        "Expiration": [datetime.strptime(e, "%Y-%m-%d").strftime("%b %d '%y")
                       for e in sub["expiration"]],
        "DTE": sub["dte"].astype(int),
        "Bid": sub["bid"].round(2), "Ask": sub["ask"].round(2),
        "Mid": sub["mid"].round(2),
        "Last": _last.round(2),
        "IV+pp": (sub["iv_excess"] * 100).round(1),
        "NetCr": net_cr.round(2),
        "Delta": sub["delta"].round(2),
        "Ann%": sub["ann_yield_pct"].round(1),
        "OI": sub["open_interest"], "Vol": sub["volume"],
    })
    col_cfg = {
        "Strike": st.column_config.TextColumn("Strike", width=75),
        "Expiration": st.column_config.TextColumn("Expiration", width=110),
        "DTE": st.column_config.NumberColumn("DTE", format="%d", width=55),
        "Bid": st.column_config.NumberColumn("Bid", format="$%.2f", width=70),
        "Ask": st.column_config.NumberColumn("Ask", format="$%.2f", width=70),
        "Mid": st.column_config.NumberColumn("Mid", format="$%.2f", width=70),
        "Last": st.column_config.NumberColumn("Last", format="$%.2f", width=70),
        "IV+pp": st.column_config.NumberColumn("IV+pp", format="%+.1f pp",
                                               width=80),
        "NetCr": st.column_config.NumberColumn(
            "Net Credit", format="$%+.2f", width=90,
            help="New-leg mid − cost to close the held leg (per share). "
                 "Positive = a net-credit roll."),
        "Delta": st.column_config.NumberColumn("Delta", format="%.2f", width=60),
        "Ann%": st.column_config.NumberColumn("Ann%", format="%.1f%%", width=65),
        "OI": st.column_config.NumberColumn("OI", format="%d", width=65),
        "Vol": st.column_config.NumberColumn("Vol", format="%d", width=65),
    }
    st.caption("🔍 **Select a target row** to build the roll.")
    # Key includes the scan generation so each new scan gives a fresh table with
    # nothing pre-selected (a stale selection would auto-open the confirm dialog).
    _scangen = st.session_state.get(f"roll_scangen_{posid}", 0)
    event = st.dataframe(disp, column_config=col_cfg, hide_index=True,
                         width="stretch", on_select="rerun",
                         selection_mode="single-row",
                         key=f"roll_target_{posid}_{_scangen}",
                         height=df_height(disp))
    sel = event.selection.rows if hasattr(event, "selection") else []
    if not sel:
        # Deselecting clears the open-guard so re-picking the SAME target row
        # reopens the dialog (mirrors the leaderboard's investigate guard).
        st.session_state[f"_roll_confirm_guard_{posid}"] = None
        return

    tgt = sub.iloc[sel[0]]
    open_mid = float(tgt["mid"]) if pd.notna(tgt["mid"]) else None
    open_roll_confirm(pos, tgt, q, close_mid, open_mid, posid, held_qty,
                      scfg, market_open, config_paper)


def open_roll_confirm(pos, tgt, close_q, close_mid, open_mid, posid, held_qty,
                      scfg, market_open, config_paper) -> None:
    """Open the roll-confirm dialog, but only on a NEW target selection.

    A per-position guard holds the last-opened target so dismissing the dialog
    doesn't immediately reopen it while the row stays selected, and a fresh open
    clears any stale confirm/result state from a prior target."""
    _sel_key = f"{float(tgt['strike']):g}|{tgt['expiration']}"
    _guard = f"_roll_confirm_guard_{posid}"
    if st.session_state.get(_guard) != _sel_key:
        st.session_state[_guard] = _sel_key
        st.session_state.pop(f"roll_result_{posid}", None)
        for _k in [k for k in list(st.session_state.keys())
                   if k.startswith(f"roll_confirm_{posid}_")]:
            st.session_state.pop(_k, None)
        _roll_confirm_dialog(pos, tgt, close_q, close_mid, open_mid, posid,
                             held_qty, scfg, market_open, config_paper)


def _roll_confirm_dialog(pos, tgt, close_q, close_mid, open_mid, posid,
                         held_qty, scfg, market_open, config_paper) -> None:
    """Dialog wrapper (dynamic title) around the roll confirm body — the roll
    equivalent of the Sell dialog. The decorator is applied at call time so the
    title can carry both legs."""
    _pexp = datetime.strptime(pos["expiration"], "%Y-%m-%d").strftime("%b %d '%y")
    _texp = datetime.strptime(str(tgt["expiration"]),
                              "%Y-%m-%d").strftime("%b %d '%y")
    _word = "Call" if pos["option_type"] == "C" else "Put"
    _title = (f"🔄 Roll {pos['underlying']} {_word}  ${pos['strike']:g} {_pexp}"
              f"  →  ${float(tgt['strike']):g} {_texp}")

    @st.dialog(_title, width="large")
    def _dlg() -> None:
        _render_confirm(pos, tgt, close_q, close_mid, open_mid, posid, held_qty,
                        scfg, market_open, config_paper)

    _dlg()


def _render_confirm(pos, tgt, close_q, close_mid, open_mid, posid, held_qty,
                    scfg, market_open, config_paper: bool) -> None:
    """Per-leg snapshots (close vs open), net credit/debit, and the two-step
    LIVE confirm. A real position can't be rolled in paper mode."""
    ticker = pos["underlying"]
    otype = pos["option_type"]
    tgt_strike = float(tgt["strike"])
    tgt_exp = str(tgt["expiration"])

    def _money(v):
        return f"${v:,.2f}" if v is not None and pd.notna(v) else "—"

    # Two per-leg snapshots side by side, even though they fill as a unit.
    def _leg_rows(bid, ask, mid, last, oi, vol, last_ms=None):
        # "Last" carries its print time (New York) on its own line beneath the
        # price when available, so a stale leg is obvious while pricing the net.
        _last = _money(last)
        _lt = trade_actions.fmt_last_trade_et(last_ms)
        if _lt:
            _last += f"<br><span style='color:#94a3b8'>{_lt}</span>"
        return [("Bid", _money(bid)), ("Ask", _money(ask)), ("Mid", _money(mid)),
                ("Last", _last), ("OI", f"{int(oi):,}" if oi is not None
                                  else "—"),
                ("Vol", f"{int(vol):,}" if vol is not None else "—")]

    cl_col, op_col = st.columns(2)
    with cl_col:
        st.markdown(f"**Close (buy to close)** — ${pos['strike']:g} "
                    f"{datetime.strptime(pos['expiration'], '%Y-%m-%d').strftime('%b %d')}")
        if close_q:
            st.markdown(_kv_table_html(_leg_rows(
                close_q.get("bid"), close_q.get("ask"), close_q.get("mid"),
                close_q.get("last"), close_q.get("open_interest"),
                close_q.get("volume"), close_q.get("last_trade_ms"))),
                unsafe_allow_html=True)
        else:
            st.caption("Close-leg re-quote unavailable.")
    with op_col:
        st.markdown(f"**Open (sell to open)** — ${tgt_strike:g} "
                    f"{datetime.strptime(tgt_exp, '%Y-%m-%d').strftime('%b %d')}")
        st.markdown(_kv_table_html(_leg_rows(
            tgt.get("bid"), tgt.get("ask"), tgt.get("mid"), tgt.get("last"),
            tgt.get("open_interest"), tgt.get("volume"),
            tgt.get("last_trade_ms"))), unsafe_allow_html=True)

    if open_mid is None or close_mid is None:
        st.info("Need a live mid on both legs to price the roll — hit 🔄 or try "
                "another target.")
        return

    net_default = trade_actions.round_to_tick(abs(open_mid - close_mid))
    net_default = net_default if (open_mid - close_mid) >= 0 else -net_default

    # Bottom-aligned so the two-line Contracts label keeps its input level with
    # the Net limit field.
    # Session keys for this target (inputs, confirm arm, last result). Defined
    # before the inputs because the confirm gate reads the inputs back by key —
    # editing either one disarms a pending Place Roll (see confirm_gate).
    _confirm_key = f"roll_confirm_{posid}_{tgt_strike:g}_{tgt_exp}"
    _result_key = f"roll_result_{posid}"
    _net_wid = f"roll_net_{posid}_{tgt_strike:g}_{tgt_exp}"
    _qty_wid = f"roll_qty_{posid}"
    _val_keys = (_net_wid, _qty_wid)

    _c1, _c2, _ = st.columns([1.2, 1, 2], vertical_alignment="bottom")
    with _c1:
        net_limit = st.number_input(
            "Net limit ($/sh · + credit / − debit)", value=float(net_default),
            step=0.01, format="%.2f", key=_net_wid)
    with _c2:
        # Max rides in the label ("Contracts \n(Max N)"), matching the Sell
        # dialog; a single-contract position has nothing to size, so the field
        # stays disabled at 1. "  \n" hard-breaks the max onto its own line.
        # No min_value/max_value: Streamlit refuses to commit an out-of-range
        # entry and keeps the last valid one, which would arm Place for a size
        # the user never typed. Validated below instead (see leaderboard).
        qty = st.number_input(f"Contracts  \n(Max {held_qty})",
                              value=held_qty, step=1,
                              disabled=(held_qty == 1), key=_qty_wid)

    # One builder, called two ways: here with the values on screen, and again in
    # the Confirm callback with the values as of the click — so Confirm can stay
    # clickable while an error shows and still refuse to arm a bad roll.
    def _build_roll(net_v, qty_v):
        return trade_actions.build_roll_order(
            ticker=ticker, option_type=otype, close_strike=float(pos["strike"]),
            close_expiration=str(pos["expiration"]), open_strike=tgt_strike,
            open_expiration=tgt_exp, quantity=int(qty_v),
            net_limit=float(net_v))

    def _roll_error(net_v, qty_v) -> str | None:
        """User-facing reason this roll can't be placed, or None."""
        if net_v is None or qty_v is None:
            # An emptied box returns None, which would reach the builder as
            # int(None) and raise TypeError instead of a message.
            return "Enter a net limit and a contract count."
        if int(qty_v) > held_qty:
            # build_roll_order validates quantity ≥ 1 but knows nothing about
            # the size actually held.
            return f"You hold {held_qty} contract(s) — can't roll {int(qty_v)}."
        try:
            _build_roll(net_v, qty_v)
        except (ValueError, TypeError) as exc:
            return f"Can't build this roll: {exc}"
        return None

    roll, _net_total, _kind = None, 0.0, "credit"
    _roll_err = _roll_error(net_limit, qty)
    if _roll_err:
        st.error(_roll_err)
    else:
        roll = _build_roll(net_limit, qty)
        _net_total = roll.net_amount
        _kind = "credit" if roll.is_credit else "debit"
        st.success((f"{roll.describe()} — net {_kind} "
                    f"${abs(_net_total):,.0f} total.").replace("$", "\\$"))

    # Real position → LIVE only. Paper mode blocks (can't simulate rolling a
    # real broker position without desyncing the tracker), mirroring Trades.
    _result = st.session_state.get(_result_key)
    account_mask = pos.get("account_mask")
    # Armed? Resolved before the Confirm button so Confirm draws disabled in the
    # same frame Place Roll appears — never both live. An input edited into an
    # invalid state since Confirm also disarms here.
    _armed = confirm_gate.armed(_confirm_key, _val_keys,
                                valid=_roll_err is None)

    if config_paper:
        st.warning("⚠️ Rolling sends a **real** order (your position is live), "
                   "but the app is in **paper mode** (`paper = true`). Set "
                   "`paper = false` in config.toml and restart to place rolls.")
        st.button("Confirm Roll", disabled=True,
                  key=f"roll_btn_{posid}", type="primary",
                  help="Disabled in paper mode — set paper=false in config.toml "
                       "and restart to place rolls.")
        return

    st.caption("🔴 LIVE — sends a real net-price roll (both legs together).")
    if market_open is False:
        st.caption("⏸ Market closed")
    _blocked = (None if market_open is True else
                ("Equity options trade 9:30–16:00 ET, Mon–Fri."
                 if market_open is False else
                 "Can't confirm market hours (Schwab unreachable)."))
    _bc, _ = st.columns([2, 3])
    with _bc:
        if _blocked:
            st.button("Confirm Roll · 🔴 LIVE", disabled=True,
                      key=f"roll_btn_{posid}", type="primary", help=_blocked,
                      width="stretch")
        elif _armed:
            # Armed → Place Roll is showing below; Cancel is the way back.
            st.button("Confirm Roll · 🔴 LIVE", disabled=True,
                      key=f"roll_btn_{posid}", type="primary",
                      help=confirm_gate.ARMED_HELP, width="stretch")
        else:
            # Stays clickable even with an invalid net limit / size: the click
            # commits the field and the callback re-validates, so a correction
            # takes one click instead of blur-then-click.
            st.button("Confirm Roll · 🔴 LIVE", key=f"roll_btn_{posid}",
                      type="primary", width="stretch",
                      on_click=confirm_gate.arm(_confirm_key, _val_keys,
                                                clear_keys=(_result_key,),
                                                validate=_roll_error))

    if _armed:
        st.warning((f"**Confirm roll** — {roll.describe()} · net {_kind} "
                    f"**${abs(_net_total):,.0f}** · 🔴 **LIVE**").replace(
                        "$", "\\$"))
        _cancel_box = f"roll_cancel_box_{posid}"
        st.markdown(
            ("<style>"
             "[class*='st-key-KEY'] button{background-color:#d9534f "
             "!important;border-color:#d9534f !important;color:#fff "
             "!important;}"
             "[class*='st-key-KEY'] button p{color:#fff !important;}"
             "</style>").replace("KEY", _cancel_box), unsafe_allow_html=True)

        b1, b2, _ = st.columns([1, 1, 3])
        with b1:
            _do = st.button("Place Roll · 🔴 LIVE", key=f"roll_do_{posid}",
                            type="primary", width="stretch")
        with b2:
            _cb = st.container(key=_cancel_box)
            _cb.button("Cancel", key=f"roll_cancel_{posid}", width="stretch",
                       on_click=confirm_gate.disarm(_confirm_key))
        if _do:
            _result = _submit_roll(scfg, roll, close_mid, open_mid, account_mask)
            st.session_state[_result_key] = _result
            st.session_state[_confirm_key] = False
            if _result.get("ok"):
                # Close the dialog and return to the Roll tab. A full rerun snaps
                # st.tabs back to the first tab, so ask run_app to switch back;
                # the message shows as a banner and the new roll appears under
                # "In-flight rolls".
                st.session_state["_osc_toast"] = _result["msg"]
                st.session_state["_osc_goto_tab"] = "Roll"
                st.rerun()

    # Failures stay in the dialog (success closes it via the rerun above).
    if _result and not _result.get("ok"):
        st.error(_result["msg"].replace("$", "\\$"))


@st.fragment
def tab_rolls() -> None:
    # A fragment so widget edits rerun just this tab. Explicit cancel/place/poll
    # actions call a full st.rerun() — never scope="fragment", which is illegal
    # during a full script run — and that's safe now because run_app's tab
    # selector preserves the active tab across a full rerun (no snap to tab 0).
    provider = st.session_state.get("data_source", "yahoo")
    scfg = st.session_state.get("schwab_config") or {}

    # Title + a compact 🔄 refresh on one row (saves a whole line). Refresh
    # clears the cached positions / status / quote / spot reads; placed above
    # the fetches so the clear takes effect on this same rerun.
    _th, _tr = st.columns([9, 1], vertical_alignment="bottom")
    with _th:
        section_header(title="🔍 Select Row to Roll a Position")
    with _tr:
        if st.button("🔄", key="roll_refresh",
                     help="Re-fetch positions, order status, quotes, and spot."):
            _rollable.clear()
            _order_status.clear()
            _close_quote.clear()
            from options_scanner.display.spot_meta import fetch_spot_meta
            fetch_spot_meta.clear()

    # ── Gating ladder (before any position read) ─────────────────────────────
    if not st.session_state.get("_schwab_configured"):
        empty_state(
            "Broker connection required",
            "Rolling places live orders, so it needs a connected broker. Only "
            "Schwab is supported today — add your Schwab credentials to "
            "config.toml and run schwab_auth.py, then pick Schwab in the top "
            "bar.")
        return
    if provider != "schwab":
        empty_state(
            "Switch to Schwab to roll",
            "Your rollable positions are read live from your Schwab account. "
            "Select **Schwab** as the data source in the top bar.")
        return

    positions = _rollable(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                          scfg.get("callback_url", ""),
                          scfg.get("token_file", ""))
    if positions is None:
        render_schwab_reauth_hint(
            "schwab", key="roll_reauth",
            token_file=st.session_state.get("_schwab_token_file"))
        empty_state("Couldn't reach Schwab",
                    "Positions are unavailable right now — re-authenticate "
                    "above or try again.")
        return

    trades = trades_store.load()
    _render_pending_rolls(trades, scfg)

    # One-time note if a roll just terminated unfilled (set in the pending loop).
    for _k in [k for k in list(st.session_state.keys())
               if k.startswith("roll_note_")]:
        st.info(st.session_state.pop(_k))

    # Market-hours gate for live placement (None = unknown → fail safe).
    market_open = _market_open(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                               scfg.get("callback_url", ""),
                               scfg.get("token_file", ""))
    config_paper = bool(scfg.get("paper", True))

    # Hidden-position blacklist (⚙️ Settings) — applied at render time, not in
    # the cached reader, so an edit lands on the next rerun (see trades.py).
    _held_n = len(positions)
    positions, _hidden = settings_ui.filter_hidden(positions, scope="roll")
    if not positions:
        if _held_n:
            # Hiding, not auth — the re-auth hint would be noise here.
            empty_state(
                "Every rollable position is hidden",
                f"All {_held_n} covered call(s) / short put(s) in your Schwab "
                f"account are hidden by your ⚙️ Settings. Unhide one there — or "
                f"tick “show these” below — to roll it.")
            # Still render the notice: it carries the reveal toggle.
            settings_ui.render_hidden_notice(_hidden, scope="roll")
        else:
            render_schwab_reauth_hint(
                "schwab", key="roll_reauth_empty",
                token_file=st.session_state.get("_schwab_token_file"))
            empty_state(
                "No rollable positions",
                "No covered calls or short puts found in your Schwab account. "
                "Sell a covered call or cash-secured put first, then roll it "
                "here.")
        return

    if config_paper:
        st.warning("📝 App is in **paper mode** — you can build and price rolls "
                   "below, but placing one needs `paper = false` (rolls act on "
                   "real positions). ")

    if _hidden:
        st.caption(f"{len(positions)} rollable position(s) in the table · "
                   f"{len(_hidden)} hidden by ⚙️ Settings.")
    else:
        st.caption(f"{len(positions)} rollable position(s) held at Schwab.")

    # Per-position live quotes (for delta) fetched once in parallel, and
    # days-since-open from the app trade log — both feed the table cells below.
    quotes = _prefetch_quotes(positions, scfg)
    opened = _opened_days_map(positions)

    # Shared column config for both tables. "Type" is dropped per table (each is
    # homogeneous, and the section title says which); "Shares" is dropped for
    # puts (share coverage is a covered-call concept only). DTE and Mark are
    # text (they carry a parenthetical: days-opened / live delta).
    col_cfg = {
        "Ticker": st.column_config.TextColumn("Ticker", width=75),
        "Spot/Day%": st.column_config.TextColumn(
            "Spot/Day%", width=135,
            help="Underlying spot and its change so far today (green up / red "
                 "down)."),
        "_pct": None,   # hidden — drives the Spot/Day% cell color only
        "_itm": None,   # hidden — drives the moneyness row shade
        "Strike": st.column_config.TextColumn("Strike", width=75),
        "Expiration": st.column_config.TextColumn("Expiration", width=110),
        # Label spells out the parenthetical so the second number reads as
        # days-open, not a second expiry figure. The dict key stays "DTE" — it
        # has to match the DataFrame column.
        "DTE": st.column_config.TextColumn(
            "DTE (days open)", width=140,
            help="Days to expiration, and — in parentheses — how many days ago "
                 "the position was opened, when the app has a record of it "
                 "(trades placed here). Blank parens never appear: positions "
                 "opened outside the scanner just show the DTE."),
        "Contracts": st.column_config.TextColumn(
            "Contracts", width=145,
            help="Open contracts; for covered calls, total shares held of the "
                 "underlying (covered-call coverage) in parens."),
        "Open$": st.column_config.NumberColumn(
            "Open", format="$%.2f", width=75,
            help="Broker average price per share when the position was opened "
                 "(from Schwab, not the app trade log)."),
        "Mark": st.column_config.TextColumn(
            "Mark", width=135,
            help="Current per-share value (from the position's market value), "
                 "and the option's live delta."),
        "P/L": st.column_config.NumberColumn(
            "Option P/L", format="$%+,.0f", width=95,
            help="Unrealized P/L on the short option: (open − mark) × 100 × "
                 "contracts. Positive = premium decayed in your favor."),
    }

    # Green/red text for signed columns (Day% and Option P/L). st.dataframe
    # honors a pandas Styler's `color`; concrete hex (CSS vars don't resolve in
    # the grid renderer).
    def _sign_color(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return ""
        if f > 0:
            return "color: #16a34a"   # green
        if f < 0:
            return "color: #dc2626"   # red
        return ""

    # Row shade by moneyness (signed % of spot: + = ITM, − = OTM). Translucent
    # tints so they overlay the cell's theme background in light AND dark mode.
    _M_GRAY = "rgba(148,163,184,0.16)"   # > 5% OTM
    _M_YELLOW = "rgba(234,179,8,0.18)"   # within 5% (ATM)
    _M_ORANGE = "rgba(249,115,22,0.18)"  # 5–20% ITM
    _M_RED = "rgba(239,68,68,0.18)"      # > 20% ITM

    def _moneyness_bg(itm):
        try:
            f = float(itm)
        except (TypeError, ValueError):
            return ""
        if f != f:  # NaN → no shade
            return ""
        if f < -5:
            _c = _M_GRAY
        elif f <= 5:
            _c = _M_YELLOW
        elif f <= 20:
            _c = _M_ORANGE
        else:
            _c = _M_RED
        return f"background-color: {_c}"

    groups = [
        ("Cash-secured puts",
         [p for p in positions if p["option_type"] == "P"],
         "roll_pos_put", ["Type"]),
        ("Covered calls",
         [p for p in positions if p["option_type"] == "C"],
         "roll_pos_call", ["Type"]),
    ]
    for label, subset, key, drop_cols in groups:
        if not subset:
            continue
        # Sort by ticker (then expiration/strike) so a ticker's legs sit
        # together. Sort the subset ITSELF — row selection maps sel[0] back into
        # `subset`, so the list order must match the rendered row order.
        subset = sorted(subset, key=lambda p: (p["underlying"], p["expiration"],
                                               float(p["strike"])))
        st.markdown(f"**{label}** ({len(subset)})")
        table = (_positions_table(subset, provider, quotes, opened)
                 .drop(columns=drop_cols))
        styled = (table.style
                  .apply(lambda r: [_moneyness_bg(table.loc[r.name, "_itm"])]
                         * len(r), axis=1)
                  .map(_sign_color, subset=["P/L"])
                  .apply(lambda s: [_sign_color(table.loc[i, "_pct"])
                                    for i in s.index], subset=["Spot/Day%"]))
        event = st.dataframe(styled, column_config=col_cfg, hide_index=True,
                             width="stretch", on_select="rerun",
                             selection_mode="single-row", key=key,
                             height=df_height(styled))
        sel = event.selection.rows if hasattr(event, "selection") else []
        if sel:
            pos = dict(subset[sel[0]])
            # Account resolved at placement time via resolve_account_hash.
            pos["account_mask"] = None
            # On a NEW selection, scroll the just-expanded detail into view — it
            # can open below the fold, so without this a click feels like nothing
            # happened. The scroll component renders at the TOP of the detail (so
            # it scrolls to the right place) and is guarded on the selected row
            # so it doesn't fight the user scrolling within the detail later.
            _guard = f"_roll_scroll_{key}"
            if st.session_state.get(_guard) != sel[0]:
                st.session_state[_guard] = sel[0]
                _scroll_into_view()
            st.markdown("---")
            _render_roll_detail(pos, scfg, market_open, config_paper, provider)
            st.markdown("---")
        else:
            st.session_state.pop(f"_roll_scroll_{key}", None)

    # Color key for the moneyness row shading (below both tables).
    _legend = [
        ("rgba(148,163,184,0.16)", "&gt;5% OTM"),
        ("rgba(234,179,8,0.18)", "within 5% (ATM)"),
        ("rgba(249,115,22,0.18)", "5–20% ITM"),
        ("rgba(239,68,68,0.18)", "&gt;20% ITM"),
    ]
    _swatches = "".join(
        "<span style='display:inline-flex;align-items:center;margin-right:18px;'>"
        "<span style='display:inline-block;width:15px;height:15px;"
        "border-radius:3px;border:1px solid rgba(128,128,128,0.5);"
        f"background:{_c};margin-right:6px;'></span>{_lbl}</span>"
        for _c, _lbl in _legend)
    st.markdown(
        "<div style='font-size:0.8rem;color:var(--osc-ink-3);margin:2px 0 8px 0;'>"
        "<span style='font-weight:600;color:var(--osc-ink-2);margin-right:12px;'>"
        f"Row shade — moneyness:</span>{_swatches}</div>",
        unsafe_allow_html=True,
    )

    st.caption("Estimates use a live Schwab mid; a roll executes both legs as "
               "one net-price order. Verify at your broker.")

    # Hidden-position note last, so it never pushes the tables down.
    settings_ui.render_hidden_notice(_hidden, scope="roll")
