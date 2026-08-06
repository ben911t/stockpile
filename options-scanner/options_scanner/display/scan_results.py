"""Ranked scan-results table renderer for the Single Ticker tab.

`show_scan_results` is the entry point — splits the input chain by
call/put, applies the OI/Vol filters, ranks each side, and delegates
the actual table render to `show_df`. `show_df` is also reused from
the Portfolio tab's per-position view.

The yellow row highlights and column tooltips come from
`display.chain_styling`; the source/timestamp caption below the
table comes from `display.scan_stamp.stamp_caption`.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from options_scanner import iv_scores
from options_scanner.format import EARNINGS_WARN_LEGEND, fmt_strike
from options_scanner.ui_theme import df_height, empty_state

from options_scanner.display.chain_styling import (
    SPREAD_HELP,
    LAST_HELP,
    CELL_WARN,
    OI_HELP,
    vol_help_for,
    ivpp_help_for,
    last_outside_mask,
    low_oi_mask,
    low_vol_mask,
    wide_spread_mask,
)
from options_scanner.display.scan_stamp import stamp_caption


def show_df(sub: pd.DataFrame, roll_close_cost: float | None = None,
            min_oi: int = 0, min_vol: int = 0,
            buy: bool = False, opt_type: str = "option",
            investigate: dict | None = None) -> None:
    """Render the styled table for one option-type subset (or one
    per-position view from the Portfolio tab).

    Empty input renders an `empty_state` callout so the user knows
    the table didn't fail to load — it just has nothing to show.
    When `roll_close_cost` is supplied (roll-an-existing-position
    flow), an extra Net Credit column is appended.

    When `investigate` is supplied (Schwab assisted-sell, sell mode), the table
    becomes single-row selectable and picking a row opens the Sell dialog. The
    dict carries {ticker, ticker_df (full chain), provider, next_earnings, spot,
    top_n, key_prefix}. None → the normal static table.
    """
    if sub.empty:
        empty_state(
            "No matches in this chain",
            "Try widening the delta band, lowering min OI/Volume, or "
            "extending the DTE range.",
        )
        return

    rank_col = {"Top": sub["_rank"]} if "_rank" in sub.columns else {}
    kind = iv_scores.active_kind(sub)

    # ⚠ in the Expiration cell = short-dated (≤60 DTE) and expiring after the
    # next earnings — its IV+pp carries event premium and it's the slice
    # excluded from the surface fit.
    _ec = (sub["earnings_count"].fillna(0) if "earnings_count" in sub.columns
           else pd.Series(0, index=sub.index))

    def _exp_cell(e, d, c):
        base = datetime.strptime(e, "%Y-%m-%d").strftime("%b %d '%y")
        return f"{base} ⚠" if (c >= 1 and d <= 60) else base

    _has_warn = any(c >= 1 and d <= 60 for c, d in zip(_ec, sub["dte"]))

    cols = {
        **rank_col,
        "Strike": sub["strike"].apply(fmt_strike),
        "Expiration": [_exp_cell(e, d, c) for e, d, c
                       in zip(sub["expiration"], sub["dte"], _ec)],
        "DTE":    sub["dte"].astype(int),
        "Bid":    sub["bid"].round(2),
        "Ask":    sub["ask"].round(2),
        "Mid":    sub["mid"].round(2),
        "Spread": (sub["ask"] - sub["bid"]).round(2),
        "Last":   sub["last"].where(sub["last"] > 0) if "last" in sub.columns else pd.Series([float("nan")] * len(sub), index=sub.index),
        "IV%":    (sub["iv"] * 100).round(1),
        "IV+pp":  (sub["iv_excess"] * 100).round(1),
    }
    # When a non-default score drives the ranking, show it alongside IV+pp.
    if kind != "IV+pp":
        mult, _ = iv_scores.display_for(kind)
        cols[kind] = (sub["signal_score"] * mult).round(2)
    cols.update({
        "Delta":  sub["delta"].round(2),
        "Ann%":   sub["ann_yield_pct"].round(1),
        "OI":     sub["open_interest"],
        "Vol":    sub["volume"],
    })
    disp = pd.DataFrame(cols)
    if roll_close_cost is not None:
        disp["NetCr"] = (sub["mid"] - roll_close_cost).round(2)

    wide = wide_spread_mask(sub["bid"], sub["ask"], sub["mid"])
    last_out = (last_outside_mask(sub["last"], sub["bid"], sub["ask"])
                if "last" in sub.columns else [False] * len(sub))
    lo = low_oi_mask(sub["open_interest"], min_oi)
    low_vol = low_vol_mask(sub["volume"], min_vol)

    styled = (
        disp.style
        .apply(lambda _: [CELL_WARN if w else "" for w in wide],
               subset=["Spread"])
        .apply(lambda _: [CELL_WARN if o else "" for o in last_out],
               subset=["Last"])
        .apply(lambda _: [CELL_WARN if l else "" for l in lo],
               subset=["OI"])
        .apply(lambda _: [CELL_WARN if v else "" for v in low_vol],
               subset=["Vol"])
    )

    col_cfg = {}
    if "_rank" in sub.columns:
        col_cfg["Top"] = st.column_config.NumberColumn("Top", format="%d",
                                                       width=45)
    col_cfg.update({
        "Strike":     st.column_config.TextColumn("Strike", width=75),
        "Expiration": st.column_config.TextColumn(
            "Expiration", width=115,
            help="⚠ = ≤60 DTE and expiring after the next earnings, so its "
                 "IV+pp includes event premium (and it's excluded from the "
                 "surface fit)."),
        "DTE":   st.column_config.NumberColumn("DTE", format="%d", width=55),
        "Bid":   st.column_config.NumberColumn("Bid", format="$%.2f",
                                               width=70),
        "Ask":   st.column_config.NumberColumn("Ask", format="$%.2f",
                                               width=70),
        "Mid":   st.column_config.NumberColumn("Mid", format="$%.2f",
                                               width=70),
        "Spread": st.column_config.NumberColumn("Spread", format="$%.2f",
                                                width=75, help=SPREAD_HELP),
        "Last":  st.column_config.NumberColumn("Last", format="$%.2f",
                                               width=70, help=LAST_HELP),
        "IV%":   st.column_config.NumberColumn("IV%", format="%.1f%%",
                                               width=70),
        "IV+pp": st.column_config.NumberColumn("IV+pp", format="%+.1f pp",
                                               width=75,
                                               help=ivpp_help_for(buy, opt_type)),
        "Delta": st.column_config.NumberColumn("Delta", format="%.2f",
                                               width=60),
        "Ann%":  st.column_config.NumberColumn("Ann%", format="%.1f%%",
                                               width=65),
        "OI":    st.column_config.NumberColumn("OI", format="%d",
                                               width=65, help=OI_HELP),
        "Vol":   st.column_config.NumberColumn("Vol", format="%d",
                                               width=65,
                                               help=vol_help_for(min_vol)),
    })
    if kind != "IV+pp":
        _, fmt = iv_scores.display_for(kind)
        col_cfg[kind] = st.column_config.NumberColumn(
            kind, format=fmt, width=85,
            help="Active ranking score — the chain is ranked by this "
                 "column. IV+pp shown alongside for context.")
    if roll_close_cost is not None:
        col_cfg["NetCr"] = st.column_config.NumberColumn("Net Credit",
                                                         format="$%+.2f",
                                                         width=85)

    if investigate is None:
        st.dataframe(styled, column_config=col_cfg, hide_index=True,
                     width="stretch", height=df_height(styled))
        if _has_warn:
            st.caption(EARNINGS_WARN_LEGEND)
        stamp_caption()
        return

    # Assisted sell: selectable rows → the Sell dialog (shared with the
    # leaderboard). One key + open-guard per table so multiple tables (sides /
    # per-ticker expanders) don't collide.
    from options_scanner.display.leaderboard import (contract_from_row,
                                                     open_investigate,
                                                     rows_fingerprint)
    _word = "covered call" if opt_type == "call" else "cash-secured put"
    st.caption(f"🔍 **Select a {opt_type} row** to investigate writing a "
               f"{_word} — Schwab assisted trade (preview).")
    # Row fingerprint in the key: a selection must not survive a filter change
    # by row index, or the dialog reopens on whatever contract now sits at that
    # index (see leaderboard.rows_fingerprint). The generation counter clears the
    # selection after a dialog opens, so a dismissed dialog leaves the table
    # ready to re-pick the same row. The guard below follows the key.
    _gen_key = f"_inv_sel_gen_{investigate['key_prefix']}_{opt_type}"
    _gen = int(st.session_state.get(_gen_key, 0))
    _key = (f"{investigate['key_prefix']}_{opt_type}_"
            f"{rows_fingerprint(sub)}_{_gen}")
    event = st.dataframe(styled, column_config=col_cfg, hide_index=True,
                         width="stretch", on_select="rerun",
                         selection_mode="single-row", key=_key,
                         height=df_height(styled))
    if _has_warn:
        st.caption(EARNINGS_WARN_LEGEND)
    stamp_caption()

    _guard = f"_inv_guard_{_key}"
    sel = event.selection.rows if hasattr(event, "selection") else []
    if not sel:
        st.session_state[_guard] = None
        return
    row = sub.iloc[sel[0]]
    contract = contract_from_row(
        row, opt_type, investigate["ticker"],
        next_earnings=investigate.get("next_earnings"),
        spot_fallback=investigate.get("spot"))
    if open_investigate(
            contract, ticker_df=investigate.get("ticker_df"),
            min_oi=min_oi, top_n=int(investigate.get("top_n", 5)),
            min_vol=min_vol, provider=investigate.get("provider", "schwab"),
            guard_key=_guard):
        # Rebuild on the next full run (dismissing the dialog causes one) so the
        # table returns with nothing selected.
        st.session_state[_gen_key] = _gen + 1


def show_scan_results(df: pd.DataFrame, mode: str, buy: bool,
                      roll_close_cost: float | None,
                      min_oi: int, top_n: int,
                      min_vol: int = 0,
                      investigate_ctx: dict | None = None) -> None:
    """Filter, rank, and render the top-N per option type.

    Splits the chain by `mode` ("call", "put", or "both"), sorts by
    signal_score (descending for sell mode, ascending for buy mode;
    defaults to iv_excess), applies the OI/Vol floors, takes the top
    N, and delegates to `show_df`. Adds a subheader when rendering
    both sides so the user knows which table is which.

    `investigate_ctx` (Schwab assisted-sell, set by the caller only in sell
    mode) turns the rows into selectable Sell controls. It carries the single
    ticker's {ticker, ticker_df, provider, next_earnings, spot, top_n,
    key_prefix, coverage}. Puts are always selectable; a call table is
    selectable only when the ticker has >=1 coverable covered call (coverage),
    otherwise it renders static with a "not coverable" note.
    """
    iv_asc = buy
    sort_col = "signal_score" if "signal_score" in df.columns else "iv_excess"
    type_labels = {"call": "Calls", "put": "Puts"}
    to_show = [mode] if mode in type_labels else list(type_labels.keys())
    # Assisted sell applies only in sell mode and outside the roll flow.
    inv_on = (investigate_ctx is not None and not buy
              and roll_close_cost is None)

    for opt_type in to_show:
        sub = (
            df[df["type"] == opt_type]
            .sort_values([sort_col, "open_interest"], ascending=[iv_asc, False])
        )
        sub = sub[(sub["open_interest"] >= min_oi)
                  & (sub["volume"] >= min_vol)].head(top_n)
        sub = sub.copy()
        sub["_rank"] = range(1, len(sub) + 1)
        if len(to_show) > 1:
            st.subheader(type_labels[opt_type])

        inv = None
        if inv_on:
            if opt_type == "put":
                inv = {**investigate_ctx, "top_n": top_n}
            else:  # call — selectable only if coverable, else static + note
                _cov = investigate_ctx.get("coverage")
                _coverable = (_cov or {}).get("coverable") or 0
                if _coverable >= 1:
                    inv = {**investigate_ctx, "top_n": top_n}
                else:
                    show_df(sub, roll_close_cost, min_oi, min_vol,
                            buy=buy, opt_type=opt_type)
                    if _cov is None:
                        st.caption("🔒 Covered-call selling needs your Schwab "
                                   "share coverage — unavailable.")
                    else:
                        st.caption(
                            f"🔒 Not coverable — you hold "
                            f"{_cov.get('shares', 0):,.0f} share(s) "
                            f"({_cov.get('short_calls', 0)} already in calls); "
                            "need 100+ uncovered to write a covered call.")
                    continue
        show_df(sub, roll_close_cost, min_oi, min_vol,
                buy=buy, opt_type=opt_type, investigate=inv)
