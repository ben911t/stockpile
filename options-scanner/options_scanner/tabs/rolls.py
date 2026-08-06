"""Positions tab — your live option legs, each closable or rollable.

Two things live here:

* ``tab_positions`` — the tab itself. The *table* is `tabs/trades`'s (every
  live Schwab option leg); this module adds the detail panel that offers
  **Close** or **Roll** on the selected row. It was two tabs until they proved
  to be one list read twice, which forced you to pick the verb before you could
  look at the position.
* The **roll builder** — assisted rolling of covered calls & cash-secured puts:
  choose a new leg from a filtered IV-surface scan and place the roll as ONE
  atomic net-price order (buy-to-close the held leg + sell-to-open the new one,
  both fills together). This is the *only* place in the app that executes a
  roll — the Portfolio/Single roll views are analysis-only and point here.

Schwab only (positions come from the live account). Because every rollable
position is a REAL position, execution follows the Trades-tab rule: a real
position can't be rolled while the app is in paper mode (that would desync the
tracker from the open broker position), so placement needs ``paper = false`` and
an open market. A working net order shows as **rolling** and is cancelable on
the Trades tab; once it fills the new leg becomes a normal open position there.
See ``expressive-rolling-lampson`` plan.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

from options_scanner import (
    confirm_gate, positions_cache, settings_ui, trade_actions, trades_store,
)
from options_scanner.fetch import fetch_and_enrich
# Leg snapshots (`leg_rows` + `kv_table_html`) live in `format` so the close
# builder in `tabs.trades` can render the identical table without importing this
# module — the dependency runs trades → rolls only lazily, and only for the roll
# monitor. Same rows, same order, same colors on all three screens.
from options_scanner.format import (
    fmt_strike, iv_pp_color, kv_table_html, leg_rows, money_html,
)
from options_scanner.ui_theme import df_height, scroll_into_view, section_header
# Reuse the Trades tab's position table, close builder, and cached read-only
# Schwab helpers rather than re-implementing them: the Positions tab is that
# table plus this module's roll builder.
from options_scanner.tabs.trades import (
    COVERAGE_BANDS, _close_quote, _day_head_md, _equity_quote, _order_status,
    _render_option_close, _render_option_positions, _sign_color, _time_value,
    _trades_context, coverage_bg, coverage_label, held_iv_pp, leg_iv_pp,
    render_buying_power_caption, yield_base,
)


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
    it's seen to fill; `roll_from` carries the old leg's identity so it can be
    closed then."""
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
                    "It shows as rolling on the Trades tab until it fills.\n"
                    "Once filled, the new leg is a normal open position.\n"
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


def reconcile_rolls(scfg: dict) -> list[str]:
    """Settle every working roll: finalize on FILL, drop it on a terminal status
    that isn't a fill. Returns human-readable notes for whatever it settled.

    Rendering-free and called by **both** the Positions tab and the Trades tab
    on load. It used to be inline in the roll tab's in-flight section, which meant
    a roll only ever settled while that tab happened to be rendering — sit on
    the Trades tab and a filled roll stayed "rolling" indefinitely.
    """
    notes: list[str] = []
    pending = [t for t in trades_store.load() if t.get("status") == "rolling"]
    if not pending:
        return notes
    _ak, _as = scfg.get("app_key", ""), scfg.get("app_secret", "")
    _cb, _tf = scfg.get("callback_url", ""), scfg.get("token_file", "")
    for t in pending:
        if not t.get("roll_order_id"):
            continue
        bs = _order_status(_ak, _as, _cb, _tf, t.get("roll_order_id"),
                           (t.get("account") or "")[-4:])
        if not bs:
            continue
        if bs.get("status") == "FILLED":
            _finalize_filled_roll(t, bs.get("filled_at"))
            notes.append(
                f"✅ Roll filled — {t.get('ticker', '?')} is now "
                f"${t.get('strike', '?')} {t.get('expiration', '')}.")
        elif not bs.get("cancelable"):
            # Terminal but unfilled → the roll won't happen; drop the record and
            # leave the original position exactly as it was.
            trades_store.remove(t["id"])
            notes.append(
                f"Roll order {bs.get('status')} without filling — your "
                f"{t.get('ticker', '?')} position is unchanged. Place a new "
                "roll when ready.")
    if notes:
        _order_status.clear()
    return notes


def roll_legs(t: dict) -> tuple[dict, dict]:
    """``(close_leg, open_leg)`` for a rolling trade — the leg being bought back
    and the one being sold. The record IS the new leg; ``roll_from`` carries the
    old one."""
    rf = t.get("roll_from") or {}
    close_leg = {"ticker": t.get("ticker"),
                 "option_type": rf.get("option_type", t.get("option_type")),
                 "strike": rf.get("strike"),
                 "expiration": rf.get("expiration")}
    open_leg = {"ticker": t.get("ticker"),
                "option_type": t.get("option_type"),
                "strike": t.get("strike"),
                "expiration": t.get("expiration")}
    return close_leg, open_leg


def render_roll_monitor(t: dict, close_q: dict | None, open_q: dict | None,
                        bs: dict | None) -> None:
    """Live view of a working roll: both legs' quotes, the net the market is
    paying now vs the limit on the order, and the broker's status.

    Shared by the roll flow and the Trades tab row, so the
    two can't describe the same order differently.
    """
    _close, _open = roll_legs(t)
    _net_limit = t.get("roll_net_px")
    _now = trade_actions.roll_net_quote(close_q, open_q)

    def _leg_line(label, leg, q):
        _r = "CALL" if str(leg.get("option_type")) == "C" else "PUT"
        _hdr = (f"**{label}** — \\${leg.get('strike', '?')} {_r} "
                f"{leg.get('expiration', '')}")
        if not q:
            return _hdr + "  \n  quote unavailable"
        return (_hdr + f"  \n  bid \\${float(q.get('bid') or 0):.2f} · "
                f"ask \\${float(q.get('ask') or 0):.2f} · "
                f"mid \\${float(q.get('mid') or 0):.2f}")

    st.markdown(_leg_line("Buy to close", _close, close_q))
    st.markdown(_leg_line("Sell to open", _open, open_q))

    def _net(v):
        return f"{'+' if v >= 0 else '−'}\\${abs(v):.2f}"

    if _net_limit is not None:
        st.markdown(f"**Your net limit** {_net(float(_net_limit))} per share")
    if _now:
        # The mid is the realistic fill estimate; the range says how much of it
        # depends on the spreads coming to you.
        _reach = ""
        if _net_limit is not None:
            _reach = ("  ·  ✅ the mid is at or through your limit"
                      if _now["mid"] >= float(_net_limit) else
                      "  ·  ⏳ the mid is short of your limit — it fills if the "
                      "market comes to you")
        st.markdown(
            f"**Net now** {_net(_now['mid'])} at the mids "
            f"({_net(_now['worst'])} crossing both spreads … "
            f"{_net(_now['best'])} if both come to you){_reach}")
    else:
        st.caption("Net unavailable — one of the legs has no two-sided quote.")
    if bs and bs.get("status"):
        st.caption(f"Broker status **{bs['status']}** — both legs fill "
                   "together.")
    else:
        st.caption("Broker status unavailable — hit 🔄 to re-check.")


# Closing-leg IV+pp bands, colored by ADVANTAGE rather than by richness. This is
def _scan_state_key(posid: str) -> str:
    """Session key holding a position's last target scan (targets + the held
    leg's IV+pp). Shared by the builder and the confirm panel."""
    return f"roll_scan_result_{posid}"



@st.fragment
def _render_roll_detail(pos: dict, scfg: dict, market_open, config_paper: bool,
                        provider: str) -> None:
    """The roll builder for one selected position: close-leg snapshot, a
    filtered target scan (NetCr column), then per-leg snapshots + a net-price
    confirm step.

    Its own fragment (nested inside the Positions table's fragment) so editing
    a filter reruns only this section — the position table above keeps its live
    quotes, spot reads and scroll position instead of re-rendering on every
    keystroke commit. Explicit place/cancel actions still call a full
    ``st.rerun()``."""
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
    _scan_key = _scan_state_key(posid)
    _bcol, _tcol = st.columns([2, 5], vertical_alignment="center")
    with _bcol:
        _scan_clicked = st.button(
            "🔍 Scan targets", key=f"roll_scan_btn_{posid}", type="primary",
            width="stretch",
            help="Run the target scan with the filters above. Adjusting a "
                 "filter won't rescan until you click this.")
    if _scan_clicked:
        # Widen the FETCH (not the filters) to cover the held leg's expiration,
        # so its IV+pp comes from the same surface fit as the targets. Rolling
        # out normally sets Min DTE past the held leg, which would otherwise
        # drop it from the chain and leave nothing to compare against. The
        # user's DTE window still filters the target list below.
        _held_dte = _dte(pos["expiration"])
        _fetch_min = (min(int(min_dte), max(0, _held_dte))
                      if _held_dte is not None else int(min_dte))
        _fetch_max = (max(int(max_dte), _held_dte)
                      if (max_dte is not None and _held_dte is not None)
                      else max_dte)
        with st.spinner(f"Scanning {ticker} targets…"):
            df, _earn, err = fetch_and_enrich(
                ticker, opt_key, _fetch_min, _fetch_max, provider=provider,
                schwab_config=scfg)
        if err:
            _res = {"error": f"Couldn't scan {ticker}: {err}"}
        elif df is None or df.empty:
            _res = {"empty": True}
        else:
            _sub = df[(df["type"] == side)
                      & (df["dte"] >= int(min_dte))
                      & (df["open_interest"] >= min_oi)
                      & (df["volume"] >= min_vol)
                      & (df["delta"].abs().between(d_lo, d_hi))].copy()
            if max_dte is not None:
                _sub = _sub[_sub["dte"] <= int(max_dte)]
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
            # Held leg's IV+pp from this same frame — shown beside the select
            # prompt and on the close-leg snapshot in the confirm panel.
            _res = {"sub": _sub, "held_pp": held_iv_pp(df, pos)}
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
    # IV+pp of the leg being closed — stated beside the select prompt below so
    # the target IV+pp values have something to be read against. Computed at
    # scan time from the same surface fit as the targets.
    _held_pp = _scan_res.get("held_pp")
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
        "IV+pp": st.column_config.NumberColumn(
            "IV+pp", format="%+.1f pp", width=80,
            help="How far the TARGET's IV sits above (+) or below (−) the "
                 "fitted surface."),
        "NetCr": st.column_config.NumberColumn(
            "Net Credit", format="$%+.2f", width=90,
            help="New-leg mid − cost to close the held leg (per share). "
                 "Positive = a net-credit roll."),
        "Delta": st.column_config.NumberColumn("Delta", format="%.2f", width=60),
        "Ann%": st.column_config.NumberColumn("Ann%", format="%.1f%%", width=65),
        "OI": st.column_config.NumberColumn("OI", format="%d", width=65),
        "Vol": st.column_config.NumberColumn("Vol", format="%d", width=65),
    }
    # The closing leg's IV+pp rides on the select prompt — one figure, right
    # where the target IV+pp column needs a reference, shaded by how far above
    # or below the surface it sits. Dropped entirely when the held leg wasn't in
    # the scanned chain. Rendered as HTML rather than st.caption because
    # Streamlit's :color[…] directives have no yellow.
    _held_txt = ""
    if _held_pp is not None:
        _held_txt = (
            f"&nbsp;&nbsp;<span style='color:{iv_pp_color(_held_pp)};"
            f"font-weight:700;' title='IV of the leg you are buying back vs "
            f"the fitted surface. Below it (green) = a cheap buyback; above it "
            f"(red) = paying up to close.'>"
            f"Closing Leg IV+pp: {_held_pp:+.1f} pp</span>")
    st.markdown(
        "<div style='font-size:0.875rem;color:var(--osc-ink-3);'>"
        "🔍 <b>Select a target row</b> to build the roll."
        f"{_held_txt}</div>", unsafe_allow_html=True)
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
        # Rebuild the target table on the next run (the dismissal above causes
        # one) so it returns with nothing selected and the same target can be
        # re-picked. The scan generation only feeds that table's widget key —
        # bumping it re-renders the stored results, it does not rescan.
        st.session_state[f"roll_scangen_{posid}"] = (
            st.session_state.get(f"roll_scangen_{posid}", 0) + 1)


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

    # on_dismiss="rerun": Streamlit's default runs nothing when a dialog is
    # closed with ✕ / Esc / a click outside, so the target table behind it never
    # re-rendered and its row stayed selected — with the open-guard then seeing
    # no new selection, re-picking the same target needed a deselect/reselect.
    @st.dialog(_title, width="large", on_dismiss="rerun")
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

    # Two per-leg snapshots side by side, even though they fill as a unit.
    # `leg_rows` is shared with the close builder and the unwind. IV+pp goes on
    # BOTH legs here, so the confirm step shows what the roll does to your IV
    # position — not just what it pays.
    #
    # Held leg's IV+pp comes from the scan that produced these targets (same
    # surface fit); the target's rides on its own scan row.
    _held_pp = (st.session_state.get(_scan_state_key(posid)) or {}).get("held_pp")
    try:
        _tgt_pp = float(tgt["iv_excess"]) * 100.0
        _tgt_pp = _tgt_pp if _tgt_pp == _tgt_pp else None
    except (KeyError, TypeError, ValueError):
        _tgt_pp = None

    cl_col, op_col = st.columns(2)
    with cl_col:
        st.markdown(f"**Close (buy to close)** — ${pos['strike']:g} "
                    f"{datetime.strptime(pos['expiration'], '%Y-%m-%d').strftime('%b %d')}")
        if close_q:
            st.markdown(kv_table_html(leg_rows(
                close_q.get("bid"), close_q.get("ask"), close_q.get("mid"),
                close_q.get("last"), close_q.get("open_interest"),
                close_q.get("volume"), close_q.get("last_trade_ms"),
                iv_pp=_held_pp), pairs=2),
                unsafe_allow_html=True)
        else:
            st.caption("Close-leg re-quote unavailable.")
    with op_col:
        st.markdown(f"**Open (sell to open)** — ${tgt_strike:g} "
                    f"{datetime.strptime(tgt_exp, '%Y-%m-%d').strftime('%b %d')}")
        st.markdown(kv_table_html(leg_rows(
            tgt.get("bid"), tgt.get("ask"), tgt.get("mid"), tgt.get("last"),
            tgt.get("open_interest"), tgt.get("volume"),
            tgt.get("last_trade_ms"), iv_pp=_tgt_pp), pairs=2),
            unsafe_allow_html=True)

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

    # What the account has, directly under the net figure it qualifies. A
    # net-debit roll has to be paid for out of these; a net-credit one adds to
    # them. Shown either way — "can I afford this?" is the question the numbers
    # on this dialog raise, and the answer shouldn't be a tab away.
    render_buying_power_caption(scfg, "Account", f"roll_{posid}")

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
                   "`paper = false` in config.toml to place rolls — it takes "
                   "effect on your next click here, no restart needed.")
        st.button("Confirm Roll", disabled=True,
                  key=f"roll_btn_{posid}", type="primary",
                  help="Disabled in paper mode — set paper=false in config.toml "
                       "(applies on your next click, no restart).")
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
                # Close the dialog and stay on this tab. The banner says where
                # the order now lives — this tab places rolls, the Trades tab
                # is where you watch one fill or cancel it. We deliberately
                # don't switch tabs for you: rendering Trades cold re-fetches
                # live data for every tracked trade, which makes placement feel
                # like it hung (same reason the Sell dialog stays put).
                st.session_state["_osc_toast"] = (
                    _result["msg"]
                    + "\nWatch it fill — or cancel it — on the Trades tab.")
                st.session_state["_osc_goto_tab"] = "Positions"
                st.rerun()

    # Failures stay in the dialog (success closes it via the rerun above).
    if _result and not _result.get("ok"):
        st.error(_result["msg"].replace("$", "\\$"))


def _time_value_line(opt_mid, spot, strike: float, expiration: str,
                     contracts: int) -> str | None:
    """The unwind panel's time-value line, or None:

    ``⏳ Time value $1.52/share · $152 on 1 contract · 2.2%/yr yield on net
    liquidation ($183/share)``

    The number that decides whether unwinding early costs you anything: buying
    the call back hands its remaining extrinsic to whoever sells it to you, so
    a fat time value is a reason to wait and a thin one is a reason not to.

    The yield annualizes that extrinsic over the capital the position actually
    ties up — its net liquidation value (`yield_base`), which is exactly what
    unwinding frees — so it answers "what is waiting paying me on the money
    that's committed?". Matches the Positions table's Ann% column for the same
    leg. Returns None when spot or the option mid is missing (nothing honest to
    say), and drops the yield alone when the leg expires today (no days to
    annualize over) rather than dropping the whole line.
    """
    tv = _time_value(opt_mid, spot, strike, "C")
    if tv is None:
        return None
    n = max(int(contracts or 0), 1)
    bits = [f"**\\${tv:,.2f}**/share",
            f"**\\${tv * 100 * n:,.0f}** on {n} contract" + ("s" if n > 1 else "")]
    dte = _dte(expiration)
    base = yield_base(spot, strike, "C", opt_mid, covered=True)
    if dte and dte > 0 and base:
        # Name the base inline. Which capital a yield is measured against is the
        # whole question here, and a number that only makes sense after hovering
        # is a number that gets misread.
        bits.append(f"**{tv / base * (365.0 / dte) * 100:.1f}%/yr** yield on "
                    f"net liquidation (\\${base:,.0f}/share)")
    return ("<span style='cursor:help;' title='Extrinsic value left in the "
            "call — what you give back by buying it in before expiration. "
            "Annualized over the capital the position ties up (spot minus the "
            "call, i.e. what unwinding frees), the same base as the Ann% "
            "column. Near zero means there is nothing left to wait for.'>"
            "⏳ Time value " + " · ".join(bits) + "</span>")


def _submit_unwind(scfg: dict, unwind: "trade_actions.UnwindOrder",
                   pos: dict) -> dict:
    """Place a LIVE unwind (close the call + sell the shares, one net order).

    Records the OPTION leg as a "closing" trade so the Trades tab polls it and
    books realized premium P/L on fill — the same record the plain close writes.
    The share sale rides on ``unwind_shares``: the trade log models premium
    received, so it can describe the stock leg but must not pretend to compute
    its gain, which depends on a cost basis this app doesn't hold. Returns
    {ok, msg}."""
    from stocks_shared.schwab_live import get_client
    try:
        client = get_client(scfg.get("app_key", ""), scfg.get("app_secret", ""),
                            scfg.get("callback_url", ""),
                            scfg.get("token_file", ""))
    except Exception as exc:
        return {"ok": False, "msg": f"Schwab unreachable: {exc}"}
    resolved = trade_actions.resolve_account_hash(
        client, (pos.get("account_mask") or "")[-4:] or None)
    if not resolved:
        return {"ok": False,
                "msg": "Couldn't resolve the account — unwind NOT sent."}
    account_hash, mask = resolved
    res = trade_actions.place_unwind_order(client, unwind, account_hash)
    if not res["ok"]:
        return {"ok": False, "msg": f"Unwind rejected: {res['error']}"}
    trades_store.add({
        "ticker": unwind.ticker, "strike": unwind.strike,
        "expiration": unwind.expiration, "option_type": "C",
        "quantity": unwind.quantity,
        "credit": round(float(pos.get("avg_price", 0) or 0), 2),
        "status": "closing",
        "close_order_id": res["order_id"],
        "close_limit_px": round(float(unwind.net_limit), 2),
        "close_qty": unwind.quantity,
        "unwind_shares": unwind.shares,
        "account": mask, "paper": False,
        "opened_from": "schwab_position",
    })
    _oid = f" (id {res['order_id']})" if res["order_id"] else ""
    # Toast format: headline first, then ONE sentence per line.
    return {"ok": True,
            "msg": (f"✅ LIVE unwind sent to {mask}{_oid}\n"
                    f"{unwind.describe()}.\n"
                    "Both legs fill together as one net order — the calls "
                    "can't close without the shares selling.\n"
                    "It shows on the Trades tab and finalizes once filled.\n"
                    "The share sale's gain/loss isn't tracked here — check "
                    "your broker for cost basis.\n"
                    "Verify at your broker.")}


@st.fragment
def _render_unwind_detail(pos: dict, scfg: dict, market_open,
                          config_paper: bool, spot: float | None,
                          provider: str = "schwab") -> None:
    """The unwind builder: buy back the covered call AND sell the shares behind
    it, as one net-credit order.

    Its own fragment so editing the size or the limit reruns only this panel.
    Structure mirrors the roll builder deliberately — same two-leg framing, same
    per-leg snapshots (`format.leg_rows`), same net-price confirm — it's the
    same kind of order. The option leg carries what the roll's close leg does
    (Bid/Ask/Mid/Last/OI/Vol/IV+pp, plus the IV% and delta that arrive on the
    same re-quote); the share leg carries the equity equivalents, since OI, IV
    and expiry mean nothing for stock.
    """
    ticker = pos["underlying"]
    held_qty = int(pos.get("quantity", 1) or 1)
    shares_held = float(pos.get("shares_held", 0) or 0)
    posid = f"{ticker}_C_{float(pos['strike']):g}_{pos['expiration']}"
    exp_disp = datetime.strptime(pos["expiration"],
                                 "%Y-%m-%d").strftime("%b %d '%y")
    st.markdown(f"### Unwind {ticker} ${pos['strike']:g} Call — {exp_disp}")
    st.caption("Buys the call back and sells the shares as ONE net-credit "
               "order: both legs fill together, so the call can't close while "
               "you still hold the stock (or the reverse — shares gone, call "
               "left naked).")

    # Both legs' live quotes — the same cached reads the rest of the tab uses.
    # `spot` from the table isn't enough on its own: it has no bid/ask, and the
    # net credit is a difference between two two-sided markets.
    _keys = (scfg.get("app_key", ""), scfg.get("app_secret", ""),
             scfg.get("callback_url", ""), scfg.get("token_file", ""))
    opt_q = _close_quote(*_keys, ticker, str(pos["expiration"]),
                         float(pos["strike"]), pos.get("option_type", "C"))
    stock_q = _equity_quote(*_keys, ticker)
    net = trade_actions.unwind_net_quote(stock_q, opt_q)

    # IV+pp for the call. Unlike bid/ask/OI/Vol/IV/delta — which ride along on
    # the re-quote above — this one needs a chain fetch and a surface fit, since
    # it's the leg's IV measured against the fitted surface. Measured cost is
    # ~2s, and `fetch_and_enrich` caches for 5 minutes, so editing the size or
    # the limit re-renders free. The panel's own job (the net credit) never
    # depends on it: a failed or empty scan just leaves the row off.
    _held_pp = leg_iv_pp(pos, ticker, provider, scfg)

    _lc, _rc = st.columns(2)
    with _lc:
        st.markdown(f"**Buy to close** — {held_qty} × ${pos['strike']:g} call")
        st.markdown(kv_table_html(leg_rows(
            (opt_q or {}).get("bid"), (opt_q or {}).get("ask"),
            (opt_q or {}).get("mid"), (opt_q or {}).get("last"),
            (opt_q or {}).get("open_interest"), (opt_q or {}).get("volume"),
            (opt_q or {}).get("last_trade_ms"), iv_pp=_held_pp,
            iv=(opt_q or {}).get("iv"), delta=(opt_q or {}).get("delta")),
            pairs=2), unsafe_allow_html=True)
    with _rc:
        st.markdown(f"**Sell** — {int(shares_held):,} shares held")
        # Shares have no OI, IV or expiry, so this leg gets the equity
        # equivalents: the mid the net is actually priced off, plus what the
        # stock has done today — selling into a 4% rally is a different trade
        # from selling into a 4% slide, and that context is on the same quote.
        _srows = [("Bid", money_html((stock_q or {}).get("bid"))),
                  ("Ask", money_html((stock_q or {}).get("ask"))),
                  ("Mid", money_html((stock_q or {}).get("mid")))]
        # Schwab's mark alongside the midpoint, not instead of it. On a liquid
        # name the mark tracks the last print and sits cents off the middle of
        # the spread; the net below is priced off the MID, so showing only the
        # mark (as this leg used to) made the net look wrong against its own
        # inputs. Dropped when it would just repeat the midpoint.
        _mark = (stock_q or {}).get("mark")
        _mid = (stock_q or {}).get("mid")
        if _mark is not None and (_mid is None or abs(_mark - _mid) >= 0.005):
            _srows.append(("Mark", money_html(_mark)))
        _srows.append(("Last", money_html((stock_q or {}).get("last"))))
        _pct = (stock_q or {}).get("pct_change")
        if _pct is not None and pd.notna(_pct):
            # Same colors, same one-decimal precision, and the same flat-is-up
            # rule as the Spot/Day% column and the Trades row headers — one
            # stock's day shouldn't read two ways in one app.
            _col = "#16a34a" if _pct >= 0 else "#dc2626"
            _srows.append(("Day", f"<span style='color:{_col};"
                                  f"font-weight:600'>{_pct:+.1f}%</span>"))
        _svol = (stock_q or {}).get("volume")
        if _svol is not None:
            _srows.append(("Vol", f"{int(_svol):,}"))
        st.markdown(kv_table_html(_srows, pairs=2), unsafe_allow_html=True)

    if not net:
        st.info("Need a two-sided quote on both the call and the stock to "
                "price an unwind — hit 🔄 and try again.")
        return
    st.markdown(
        f"**Net now** \\${net['mid']:,.2f} per share at the mids "
        f"(\\${net['worst']:,.2f} crossing both spreads … "
        f"\\${net['best']:,.2f} if both come to you)")

    _confirm_key = f"unwind_confirm_{posid}"
    _result_key = f"unwind_result_{posid}"
    _net_wid = f"unwind_net_{posid}"
    _qty_wid = f"unwind_qty_{posid}"
    _val_keys = (_net_wid, _qty_wid)

    _c1, _c2, _ = st.columns([1.2, 1, 2], vertical_alignment="bottom")
    with _c1:
        net_limit = st.number_input(
            "Net limit ($/share credit)",
            value=float(trade_actions.round_to_tick(net["mid"])),
            step=0.01, format="%.2f", key=_net_wid)
    with _c2:
        # No min/max: Streamlit refuses to commit an out-of-range entry and
        # keeps the last valid one, which would arm Place for a size nobody
        # typed. Validated in _unwind_error instead.
        qty = st.number_input(f"Contracts  \n(Max {held_qty})", value=held_qty,
                              step=1, disabled=(held_qty == 1), key=_qty_wid)

    def _build(net_v, qty_v):
        return trade_actions.build_unwind_order(
            ticker=ticker, strike=float(pos["strike"]),
            expiration=str(pos["expiration"]), quantity=int(qty_v),
            net_limit=float(net_v), shares_held=shares_held)

    def _unwind_error(net_v, qty_v) -> str | None:
        """User-facing reason this unwind can't be placed, or None."""
        if net_v is None or qty_v is None:
            return "Enter a net limit and a contract count."
        if int(qty_v) > held_qty:
            return f"You hold {held_qty} contract(s) — can't unwind {int(qty_v)}."
        try:
            _build(net_v, qty_v)
        except (ValueError, TypeError) as exc:
            return f"Can't build this unwind: {exc}"
        return None

    unwind = None
    _err = _unwind_error(net_limit, qty)
    if _err:
        st.error(_err)
    else:
        unwind = _build(net_limit, qty)
        st.success((f"{unwind.describe()} — {unwind.shares} shares sold, "
                    f"net credit ${unwind.net_amount:,.0f} total."
                    ).replace("$", "\\$"))
        if unwind.shares < shares_held:
            st.caption(f"Leaves {int(shares_held) - unwind.shares:,} shares "
                       f"of {ticker} untouched.")
    render_buying_power_caption(scfg, "Account", f"unwind_{posid}")

    # What unwinding costs you in optionality: the remaining extrinsic on the
    # call is premium you'd hand back by buying it in early, and the yield says
    # what that is worth per year. A near-worthless time value is the cue that
    # there's nothing left to wait for — the same reading as the Ann% column,
    # and computed the same way (calls annualize against spot) so the two can't
    # disagree.
    _tv_line = _time_value_line(
        (opt_q or {}).get("mid"), (stock_q or {}).get("mid"),
        float(pos["strike"]), str(pos["expiration"]), int(qty or held_qty))
    if _tv_line:
        st.caption(_tv_line, unsafe_allow_html=True)

    _result = st.session_state.get(_result_key)
    _armed = confirm_gate.armed(_confirm_key, _val_keys, valid=_err is None)

    if config_paper:
        st.warning("⚠️ Unwinding sends a **real** order (the position and the "
                   "shares are live), but the app is in **paper mode** "
                   "(`paper = true`). Set `paper = false` in config.toml to "
                   "place one.")
        st.button("Confirm Unwind", disabled=True, key=f"unwind_btn_{posid}",
                  type="primary",
                  help="Disabled in paper mode — set paper=false in config.toml.")
        return

    st.caption("🔴 LIVE — sends a real net-credit order (option + shares "
               "together).")
    _blocked = (None if market_open is True else
                ("Equity options trade 9:30–16:00 ET, Mon–Fri."
                 if market_open is False else
                 "Can't confirm market hours (Schwab unreachable)."))
    if market_open is False:
        st.caption("⏸ Market closed")
    _bc, _ = st.columns([2, 3])
    with _bc:
        if _blocked:
            st.button("Confirm Unwind · 🔴 LIVE", disabled=True,
                      key=f"unwind_btn_{posid}", type="primary", help=_blocked,
                      width="stretch")
        elif _armed:
            st.button("Confirm Unwind · 🔴 LIVE", disabled=True,
                      key=f"unwind_btn_{posid}", type="primary",
                      help=confirm_gate.ARMED_HELP, width="stretch")
        else:
            st.button("Confirm Unwind · 🔴 LIVE", key=f"unwind_btn_{posid}",
                      type="primary", width="stretch",
                      on_click=confirm_gate.arm(_confirm_key, _val_keys,
                                                clear_keys=(_result_key,),
                                                validate=_unwind_error))

    if _armed and unwind is not None:
        st.warning((f"**Confirm unwind** — {unwind.describe()}\n\n"
                    f"This closes the option AND sells {unwind.shares} shares. "
                    f"Both legs fill together or not at all."
                    ).replace("$", "\\$"))
        _pc, _cc, _ = st.columns([2, 2, 3])
        with _pc:
            if st.button("Place Unwind · 🔴 LIVE", key=f"unwind_place_{posid}",
                         type="primary", width="stretch"):
                _res = _submit_unwind(scfg, unwind, pos)
                st.session_state[_confirm_key] = False
                if _res.get("ok"):
                    st.session_state["_osc_toast"] = _res["msg"]
                    st.session_state.pop(_result_key, None)
                    # The leg (and its shares) are on their way out — drop the
                    # cached position read so the table doesn't keep offering
                    # an unwind for something already being unwound.
                    positions_cache.option_positions.clear()
                    st.rerun()
                st.session_state[_result_key] = _res
        with _cc:
            st.button("Cancel", key=f"unwind_cancel_{posid}", width="stretch",
                      on_click=confirm_gate.disarm(_confirm_key))

    if _result and not _result.get("ok"):
        st.error(_result["msg"].replace("$", "\\$"))


def _position_action_panel(pos: dict, scfg: dict, provider: str, market_open,
                           config_paper: bool, spot: float | None) -> None:
    """The Positions tab's detail panel: Close, Roll or Unwind the selected leg.

    The action is chosen **after** the row is selected, not before, because
    "close it, roll it, or get out entirely?" is one decision made while looking
    at one position. Close is the default: opening the roll builder runs a chain
    scan for the underlying, and no row click should cost that unless it was
    asked for.

    Which actions are offered depends on what the leg is:

    * **Close** — always. It's the one action every position supports.
    * **Roll** — legs `trade_actions.is_rollable` accepts (short puts,
      share-backed short calls): a roll replaces premium you sold.
    * **Unwind** — legs `trade_actions.is_unwindable` accepts (covered calls
      only): closes the call *and* sells the shares behind it, so it needs both
      halves to exist.

    A leg that supports nothing but Close gets the close builder and a line
    saying why the others don't apply.
    """
    posid = (f"{pos.get('underlying', '')}_{pos.get('option_type', 'P')}"
             f"_{float(pos.get('strike', 0)):g}_{pos.get('expiration', '')}")
    actions = ["Close"]
    if trade_actions.is_rollable(pos):
        actions.append("Roll")
    if trade_actions.is_unwindable(pos):
        actions.append("Unwind")

    if len(actions) == 1:
        _why = ("it's a long option — there's no premium to roll forward and no "
                "short position to unwind"
                if str(pos.get("direction", "")).lower() != "short"
                else "it's a naked short call — rolling or unwinding one needs "
                     "at least 100 shares of the underlying per contract")
        st.caption(f"Only closing is available for this leg: {_why}.")
        _render_option_close(pos, scfg, market_open, config_paper, spot,
                             provider)
        return

    # Switching action disarms the close gate. A confirm attests to specific
    # numbers on a specific order; leaving it armed while the user goes off to
    # price a roll would bring them back to a live Place button they armed in
    # another context. (The roll and unwind gates are keyed per target/size and
    # re-validate on render, so they can't survive a switch either.)
    # Seed the key rather than passing `default=`: run_app re-asserts every str
    # session value on each run to keep widgets alive across tab switches, and
    # Streamlit warns once per rerun when a widget has both a default and a
    # session-state value. Same pattern as the data-source toggle in run_app.
    _akey = f"_osc_pos_action_{posid}"
    st.session_state.setdefault(_akey, "Close")
    action = st.segmented_control(
        "Action", actions, key=_akey, label_visibility="collapsed",
        on_change=confirm_gate.disarm(f"opt_close_confirm_{posid}"))
    if action == "Roll":
        # Account resolved at placement time via resolve_account_hash.
        pos = dict(pos)
        pos["account_mask"] = None
        _render_roll_detail(pos, scfg, market_open, config_paper, provider)
        st.caption("Estimates use a live Schwab mid; a roll executes both legs "
                   "as one net-price order. Verify at your broker.")
    elif action == "Unwind":
        _render_unwind_detail(pos, scfg, market_open, config_paper, spot,
                              provider)
    else:
        # segmented_control returns None when the active chip is clicked again;
        # closing is the safe thing to fall back to (it can only reduce a
        # position, and it's what the panel opens on).
        _render_option_close(pos, scfg, market_open, config_paper, spot,
                             provider)


@st.fragment
def _render_stock_positions(scfg: dict, provider: str, market_open) -> None:
    """Every stock held at Schwab, shaded by how much of it is written against.

    The question this answers is "where could I still sell a call?" — so the
    row shade is by coverage state, the columns lead with shares/written/free,
    and picking a row with a free 100-lot opens the call builder below.

    A fragment so scanning a ticker for call candidates reruns only this
    section, leaving the option table above with its live quotes intact.
    """
    from options_scanner.display.spot_meta import fetch_spot_meta

    _hdr, _rf = st.columns([8, 1], vertical_alignment="bottom")
    with _hdr:
        section_header(title="Your Stock Positions (Schwab)")
    with _rf:
        if st.button("🔄", key="stk_pos_refresh",
                     help="Re-fetch stock positions and spot."):
            positions_cache.stock_positions.clear()
            fetch_spot_meta.clear()
            st.rerun()

    rows = positions_cache.stock_positions(
        scfg.get("app_key", ""), scfg.get("app_secret", ""),
        scfg.get("callback_url", ""), scfg.get("token_file", ""))
    if rows is None:
        st.warning("Couldn't reach Schwab — your token may have expired. "
                   "Re-run `schwab_auth.py`, then hit 🔄.")
        return
    if not rows:
        st.caption("No stock positions in your Schwab account.")
        return

    # Same display-only blacklist the option table uses, applied here rather
    # than in the cached reader so a settings change lands on the next rerun.
    # `filter_hidden` matches on `underlying`, which a stock row doesn't have —
    # so it's supplied here. A ticker-wide rule ("hide AAPL") therefore hides
    # the shares as well as the legs, which is what ticking a whole underlying
    # means on a tab that shows both; a narrower rule (one strike, or all puts)
    # can't match a stock row and correctly leaves it alone.
    #
    # The *notice* is deliberately not rendered here: `render_hidden_notice`
    # keys its "show these anyway" checkbox on the scope alone, so calling it
    # from both tables on this tab is a duplicate-key crash. The option table
    # above already renders it, and the toggle is session-wide — it governs
    # this table too.
    _held_n = len(rows)
    rows, _hidden = settings_ui.filter_hidden(
        [{**r, "underlying": r["ticker"]} for r in rows], scope="positions")
    if not rows:
        st.caption(f"All {_held_n} of your stock positions are hidden by your "
                   "⚙️ Settings — nothing to show.")
        return

    meta = {r["ticker"]: (fetch_spot_meta(r["ticker"], provider) or {})
            for r in rows}

    def _spot(t):
        v = (meta.get(t) or {}).get("spot")
        try:
            return float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    def _frame(subset):
        return pd.DataFrame({
            "Ticker": [r["ticker"] for r in subset],
            "Coverage": [coverage_label(r["state"]) for r in subset],
            "Shares": [int(r["shares"]) for r in subset],
            "Written": [r["written"] for r in subset],
            "Uncovered": [r["uncovered_shares"] for r in subset],
            "Can write": [r["coverable"] for r in subset],
            "Spot": [_spot(r["ticker"]) for r in subset],
            "Avg cost": [r["avg_price"] for r in subset],
            "Mkt Val": [r["market_value"] for r in subset],
            "P/L": [r["pl"] for r in subset],
        })

    _col_cfg = {
        "Ticker": st.column_config.TextColumn("Ticker", width=80),
        "Coverage": st.column_config.TextColumn(
            "Coverage", width=130,
            help="Whether calls are written against these shares. The row "
                 "shade says the same thing."),
        "Shares": st.column_config.NumberColumn("Shares", format="%d",
                                                width=80),
        "Written": st.column_config.NumberColumn(
            "Written", format="%d", width=80,
            help="Short call contracts open against this underlying."),
        "Uncovered": st.column_config.NumberColumn(
            "Uncovered", format="%d", width=95,
            help="Shares with no call written against them."),
        "Can write": st.column_config.NumberColumn(
            "Can write", format="%d", width=95,
            help="Further covered calls these shares support — uncovered "
                 "shares ÷ 100, rounded down. Select a row with 1 or more to "
                 "build the trade."),
        "Spot": st.column_config.NumberColumn("Spot", format="$%.2f", width=85),
        "Avg cost": st.column_config.NumberColumn("Avg cost", format="$%.2f",
                                                  width=90),
        "Mkt Val": st.column_config.NumberColumn("Mkt Val", format="$%.0f",
                                                 width=95),
        "P/L": st.column_config.NumberColumn("P/L", format="$%+.0f", width=90),
    }
    def _render(subset, *, selectable: bool):
        """One table. Styling and columns are identical either way — only the
        checkbox differs — so the split can't make the two halves look like
        different data."""
        frame = _frame(subset)
        styled = (frame.style
                  .apply(lambda r: [coverage_bg(subset[r.name]["state"],
                                                subset[r.name]["coverable"])]
                         * len(r), axis=1)
                  .map(_sign_color, subset=["P/L"]))
        return st.dataframe(
            styled, hide_index=True, width="stretch", column_config=_col_cfg,
            height=df_height(styled),
            key="stock_positions" if selectable else "stock_positions_locked",
            **({"on_select": "rerun", "selection_mode": "single-row"}
               if selectable else {}))

    # Only rows you could actually write against get a checkbox. Streamlit's
    # dataframe selection is all-or-nothing per table, so showing a checkbox on
    # some rows and not others means splitting them — the same thing the
    # watchlist Calls board does for the same reason
    # (`leaderboard._render_calls_by_coverage`).
    writable = [r for r in rows if r["coverable"] >= 1]
    locked = [r for r in rows if r["coverable"] < 1]

    event = None
    if writable:
        st.caption(f"**{len(writable)}** position(s) with 100+ uncovered "
                   "shares — select one to build a covered call.")
        event = _render(writable, selectable=True)
    else:
        st.info("None of your stock positions have 100+ uncovered shares, so "
                "there's no covered call to write right now.")

    if locked:
        st.markdown("**Nothing to write** — fully covered, under 100 uncovered "
                    "shares, or already over-written (no select checkbox).")
        _render(locked, selectable=False)

    # Legend — the shades are only readable if something says what they mean,
    # and st.dataframe has no per-row hover.
    st.caption(" · ".join(
        f"<span style='background:{c};padding:1px 7px;border-radius:4px'>"
        f"{label}</span>"
        for c, label, _h in COVERAGE_BANDS.values()), unsafe_allow_html=True)

    _over = [r for r in rows if r["state"] == "over_written"]
    if _over:
        st.warning(
            "⚠️ " + ", ".join(
                f"**{r['ticker']}** ({r['naked_calls']} call"
                f"{'s' if r['naked_calls'] != 1 else ''})" for r in _over)
            + " — more calls written than shares to back them. The excess is "
              "naked: assignment forces a buy at market.")
    if _hidden:
        # Names the symbols rather than pointing at the option table's "Hidden
        # positions" expander: hiding a symbol you hold no options on (the
        # reason stock hiding exists) means that expander isn't rendered at
        # all, so a pointer to it would lead nowhere. ⚙️ Settings always is.
        st.caption(
            "🙈 Hidden by your ⚙️ Settings: "
            + ", ".join(sorted(str(h.get("ticker") or h.get("underlying"))
                               for h in _hidden))
            + " — untick in ⚙️ Settings to bring them back.")

    sel = (event.selection.rows
           if event is not None and hasattr(event, "selection") else [])
    if not sel:
        st.session_state.pop("_stk_scroll", None)
        return
    if st.session_state.get("_stk_scroll") != sel[0]:
        st.session_state["_stk_scroll"] = sel[0]
        scroll_into_view()
    st.markdown("---")
    # Index into `writable`, not `rows` — the selectable table renders only
    # that subset, so a row index means nothing against the full list. Getting
    # this wrong builds a call for whichever position happens to sit at the
    # same index, which is why the two are never rendered from one frame.
    picked = writable[sel[0]]
    _render_sell_call_detail(picked, scfg, provider, market_open,
                             _spot(picked["ticker"]),
                             (meta.get(picked["ticker"]) or {}).get("pct_change"))


def _render_sell_call_detail(stk: dict, scfg: dict, provider: str,
                             market_open, spot: float | None,
                             pct: float | None = None) -> None:
    """Build a covered call against a selected stock position.

    Deliberately the roll builder's shape — filters, an explicit **Scan**, a
    ranked table, then a dialog on the picked row — because it's the same
    decision (which strike/expiration to write) reached from a different
    starting point. The Sell Call dialog itself is the watchlist leaderboard's,
    so a call sold from here goes through exactly the same order builder,
    confirm gate and trade log as one sold from a scan.
    """
    tkr = stk["ticker"]
    coverable = int(stk.get("coverable", 0) or 0)
    # Spot and today's move lead the panel: which strikes are worth scanning
    # depends on where the stock is and which way it's going, and both are
    # decided before touching the filters below. Above the coverable guard on
    # purpose — the quote is worth seeing even on a position you can't write.
    # `_day_head_md` is the Trades tab's own renderer, so this reads identically
    # to every other quote in the app: green up, red down, and plain when
    # there's no change to state (rather than defaulting to green).
    st.markdown(f"### Sell a covered call — {tkr}")
    _q = _day_head_md(spot, pct)
    if _q:
        # Its own line at body size rather than inside the h3, which would
        # blow the quote up to heading size and bury the ticker.
        st.markdown(_q)

    # Only writable rows get a checkbox, so this is a guard rather than the
    # path anyone takes. It stays because it's the invariant that matters: the
    # builder must never price a call these shares can't cover, whatever the
    # table above did.
    if coverable < 1:
        _held = int(stk.get("shares", 0))
        if stk["state"] == "over_written":
            st.warning(
                f"**{tkr}** already has {stk['written']} calls written against "
                f"{_held} shares — {stk['naked_calls']} of them naked. Close or "
                "roll a call on the table above before writing another.")
        elif stk["written"]:
            st.info(
                f"**{tkr}** is fully written: {stk['written']} call(s) against "
                f"{_held} shares, leaving {stk['uncovered_shares']} uncovered "
                "— under the 100 a contract needs.")
        else:
            st.info(
                f"**{tkr}** has {_held} shares. A covered call needs 100 per "
                f"contract, so there's nothing to write yet.")
        return

    st.caption(
        f"{int(stk['uncovered_shares']):,} uncovered shares → up to "
        f"**{coverable}** contract(s). Pick a strike and expiration below; the "
        "dialog caps the size at what these shares cover.")

    # Same two-row shape as the roll builder — same five knobs in the same
    # order, then the strike range — so the two builders on this tab read as
    # one control set rather than two dialects.
    f1, f2, f3, f4, f5 = st.columns(5)
    with f1:
        min_oi = int(st.number_input("Min OI", min_value=0, value=25, step=25,
                                     key=f"cc_minoi_{tkr}"))
    with f2:
        min_vol = int(st.number_input("Min Vol", min_value=0, value=0, step=10,
                                      key=f"cc_minvol_{tkr}",
                                      help="Contracts traded today. 0 keeps "
                                           "everything — volume is empty for "
                                           "the whole chain while the market "
                                           "is closed."))
    with f3:
        min_dte = int(st.number_input("Min DTE", min_value=0, value=21, step=7,
                                      key=f"cc_mindte_{tkr}"))
    with f4:
        # Nullable (value=None → clearable) seeded via session state, so it
        # shows a default ceiling that can still be cleared to mean "no max".
        _mk = f"cc_maxdte_{tkr}"
        if _mk not in st.session_state:
            st.session_state[_mk] = 90
        _max_raw = st.number_input("Max DTE", min_value=1, value=None, step=30,
                                   key=_mk,
                                   help="Clear the field for no upper limit.")
        max_dte = int(_max_raw) if _max_raw is not None else None
    with f5:
        d_lo, d_hi = st.slider("|Delta|", 0.0, 1.0, (0.15, 0.45), 0.05,
                               key=f"cc_delta_{tkr}",
                               help="0.20–0.40 is the usual covered-call "
                                    "band: meaningful premium without a "
                                    "coin-flip chance of assignment.")

    # Strike range. This is also how you keep assignment above your cost basis
    # — the reason there's no separate tick for it: one control, set to
    # whatever floor you actually want, rather than a checkbox that can only
    # mean exactly break-even.
    _avg = float(stk.get("avg_price", 0) or 0)
    _cost_hint = (f" Your cost basis is ${_avg:,.2f} — assignment below it "
                  "realizes a loss on the shares that the premium may not "
                  "cover." if _avg else "")
    g1, g2, _ = st.columns([1, 1, 3], vertical_alignment="bottom")
    with g1:
        _mins = st.number_input("Min Strike", min_value=0.0, value=None,
                                step=1.0, key=f"cc_minstrike_{tkr}",
                                help="Empty = no minimum." + _cost_hint)
    with g2:
        _maxs = st.number_input("Max Strike", min_value=0.0, value=None,
                                step=1.0, key=f"cc_maxstrike_{tkr}",
                                help="Empty = no maximum. Caps how far up you "
                                     "give away the shares' upside.")

    _skey = f"cc_scan_{tkr}"
    _bcol, _tcol = st.columns([2, 5], vertical_alignment="center")
    with _bcol:
        _scan = st.button("🔍 Scan calls", key=f"cc_scan_btn_{tkr}",
                          type="primary", width="stretch",
                          help="Adjusting a filter won't rescan until you "
                               "click this.")
    if _scan:
        with st.spinner(f"Scanning {tkr} calls…"):
            df, _earn, err = fetch_and_enrich(
                tkr, "calls", int(min_dte), max_dte, provider=provider,
                schwab_config=scfg)
        if err:
            _res = {"error": f"Couldn't scan {tkr}: {err}"}
        elif df is None or df.empty:
            _res = {"empty": True}
        else:
            _sub = df[(df["type"] == "call")
                      & (df["dte"] >= int(min_dte))
                      & (df["open_interest"] >= min_oi)
                      & (df["volume"] >= min_vol)
                      & (df["delta"].abs().between(d_lo, d_hi))].copy()
            if max_dte is not None:
                _sub = _sub[_sub["dte"] <= int(max_dte)]
            if _mins is not None:
                _sub = _sub[_sub["strike"] >= float(_mins)]
            if _maxs is not None:
                _sub = _sub[_sub["strike"] <= float(_maxs)]
            _sort = ("signal_score" if "signal_score" in _sub.columns
                     else "iv_excess")
            _sub = _sub.sort_values([_sort, "open_interest"],
                                    ascending=[False, False]
                                    ).reset_index(drop=True)
            # Keep the UNfiltered chain too: the dialog's IV chart plots the
            # whole surface to show how rich the picked strike is against it,
            # so handing it the filtered subset would flatten the comparison.
            _res = {"sub": _sub, "chain": df}
        st.session_state[_skey] = _res
        st.session_state[f"cc_gen_{tkr}"] = (
            st.session_state.get(f"cc_gen_{tkr}", 0) + 1)

    _res = st.session_state.get(_skey)
    if not _res:
        with _tcol:
            st.markdown("Set your filters, then click **🔍 Scan calls** to "
                        "rank candidates by IV+pp.")
        return
    if _res.get("error"):
        st.warning(_res["error"])
        return
    if _res.get("empty"):
        st.info(f"No chain returned for {tkr}.")
        return
    sub = _res["sub"]
    if sub.empty:
        st.info("No calls passed those filters — widen the DTE window or the "
                "delta band, lower Min OI / Min Vol, or clear the strike "
                "range. (Volume is 0 across the whole chain while the market "
                "is closed, so a Min Vol above 0 empties the table then.)")
        return

    _last = (sub["last"].where(sub["last"] > 0) if "last" in sub.columns
             else pd.Series([float("nan")] * len(sub), index=sub.index))
    disp = pd.DataFrame({
        "Strike": sub["strike"].apply(fmt_strike),
        "Expiration": [datetime.strptime(e, "%Y-%m-%d").strftime("%b %d '%y")
                       for e in sub["expiration"]],
        "DTE": sub["dte"].astype(int),
        "Bid": sub["bid"].round(2), "Ask": sub["ask"].round(2),
        "Mid": sub["mid"].round(2), "Last": _last.round(2),
        "IV+pp": (sub["iv_excess"] * 100).round(1),
        "Delta": sub["delta"].round(2),
        "Ann%": sub["ann_yield_pct"].round(1),
        "OI": sub["open_interest"], "Vol": sub["volume"],
        # What the whole trade pays if you write the full coverable size.
        "Credit": (sub["mid"] * 100 * coverable).round(0),
    })
    _cfg = {
        "Strike": st.column_config.TextColumn("Strike", width=75),
        "Expiration": st.column_config.TextColumn("Expiration", width=110),
        "DTE": st.column_config.NumberColumn("DTE", format="%d", width=55),
        "Bid": st.column_config.NumberColumn("Bid", format="$%.2f", width=70),
        "Ask": st.column_config.NumberColumn("Ask", format="$%.2f", width=70),
        "Mid": st.column_config.NumberColumn("Mid", format="$%.2f", width=70),
        "Last": st.column_config.NumberColumn("Last", format="$%.2f", width=70),
        "IV+pp": st.column_config.NumberColumn(
            "IV+pp", format="%+.1f pp", width=80,
            help="How far this call's IV sits above (+) or below (−) the "
                 "fitted surface. Higher = richer premium for the risk."),
        "Delta": st.column_config.NumberColumn(
            "Delta", format="%.2f", width=65,
            help="Rough chance of assignment at expiration."),
        "Ann%": st.column_config.NumberColumn("Ann%", format="%.1f%%", width=65),
        "OI": st.column_config.NumberColumn("OI", format="%d", width=65),
        "Vol": st.column_config.NumberColumn("Vol", format="%d", width=65),
        "Credit": st.column_config.NumberColumn(
            "Credit", format="$%.0f", width=90,
            help=f"Premium at the mid for all {coverable} contract(s)."),
    }
    st.markdown(
        "<div style='font-size:0.875rem;color:var(--osc-ink-3);'>"
        "🔍 <b>Select a call</b> to open the Sell Call dialog.</div>",
        unsafe_allow_html=True)
    _gen = st.session_state.get(f"cc_gen_{tkr}", 0)
    ev = st.dataframe(disp, column_config=_cfg, hide_index=True,
                      width="stretch", on_select="rerun",
                      selection_mode="single-row",
                      key=f"cc_targets_{tkr}_{_gen}", height=df_height(disp))
    picks = ev.selection.rows if hasattr(ev, "selection") else []
    if not picks:
        st.session_state[f"_cc_guard_{tkr}"] = None
        return

    # Build the contract and open the dialog through the leaderboard's own
    # helpers, not a hand-rolled dict: `contract_from_row` owns the shape the
    # dialog expects (including the NaN→None and int coercions its `:,d` /
    # `:.1f` formats depend on), and `open_investigate` owns the open-once
    # guard plus clearing that contract's stale confirm/result state. Sharing
    # them is what keeps a call sold from here identical to one sold from a
    # watchlist scan. Imported lazily so the Positions tab doesn't pull the
    # leaderboard's scan machinery on every render.
    from options_scanner.display.leaderboard import (
        contract_from_row, open_investigate)
    contract = contract_from_row(sub.iloc[picks[0]], "call", tkr,
                                 spot_fallback=spot)
    if open_investigate(contract, ticker_df=_res.get("chain"),
                        min_oi=int(min_oi),
                        top_n=5, min_vol=0, provider=provider,
                        guard_key=f"_cc_guard_{tkr}"):
        # Bump the table key so the row clears on the next full run — Streamlit
        # has no dialog-dismissed callback, so it's cleared at open time.
        st.session_state[f"cc_gen_{tkr}"] = _gen + 1


def tab_positions() -> None:
    """The Positions tab: every live option leg in the Schwab account, with
    Close *or* Roll on the selected row.

    Was two tabs. They listed the same account through two readers and rendered
    the same table, which forced you to pick the verb before you could look at
    the position. The table lives in `tabs/trades` (it's the richer of the two);
    this module supplies the action panel, because the roll builder is here.
    """
    provider, scfg, market_open = _trades_context()

    # Roll bookkeeping on load, before the table reads positions: settle any
    # roll that filled at the broker so the old leg drops out of the list
    # instead of lingering as a position you no longer hold. Rendering-free and
    # shared with the Trades tab.
    if provider == "schwab" and scfg.get("app_key"):
        for _note in reconcile_rolls(scfg):
            st.info(_note)
        _working = [t for t in trades_store.load()
                    if t.get("status") == "rolling"]
        if _working:
            st.info(f"⏳ {len(_working)} roll(s) working — watch the fill, or "
                    "cancel, on the **Trades** tab.")

    _render_option_positions(scfg, provider, market_open,
                             render_detail=_position_action_panel)
    # Stocks below options: the option legs are what this tab acts on, and the
    # stock table's job is to answer "what could I still write a call against?"
    # — a question you ask after reading what's already written.
    if provider == "schwab" and scfg.get("app_key"):
        st.markdown("---")
        _render_stock_positions(scfg, provider, market_open)
