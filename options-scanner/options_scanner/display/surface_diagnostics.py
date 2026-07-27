"""Surface-fit diagnostics — show what data actually fed the IV-surface fit.

Rendered in an expander beneath the IV chart, this answers three
questions the fit pipeline otherwise hides:

  - *What feeds the fit?* A filter funnel from the full fetched chain down
    to the rows that anchored the regression, so you can see which filter
    starved a thin fit.
  - *Where did each expiration's line come from?* A per-expiration table
    flagging slices fit by fallback (borrowed surface / slice mean) or not
    fit at all (`none` → the line traces the quotes).
  - *How solid is the fit?* A headline caption with coverage and residual σ.

It operates on the **full** fetched chain, not the delta-display subset
the table/chart show, because the surface is fit on the full chain (a
wider surface-fit delta range than the display slider). Reusing
`iv_filters.funnel` keeps the reported drop-offs in lockstep with the
real fit subset.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

from options_scanner import iv_algorithms, iv_filters
from options_scanner.display.chain_styling import CELL_WARN
from options_scanner.ui_theme import df_height

_FALLBACK_METHODS = {"fallback", "none"}
_METHOD_LABEL = {
    "global":     "global surface",
    "per_expiry": "per-expiry fit",
    "fallback":   "fallback (borrowed)",
    "none":       "none (traces quotes)",
}


def _residual_sigma_pp(frame: pd.DataFrame) -> float:
    """Std of in-fit residuals (iv_excess) in pp, or NaN if none qualify."""
    if not {"in_fit", "iv_excess"} <= set(frame.columns):
        return float("nan")
    resid = frame.loc[frame["in_fit"], "iv_excess"].to_numpy(dtype=float)
    resid = resid[np.isfinite(resid)]
    return float(np.std(resid) * 100) if resid.size else float("nan")


def _fmt_exp(exp: str) -> str:
    try:
        return datetime.strptime(exp, "%Y-%m-%d").strftime("%b %d '%y")
    except (ValueError, TypeError):
        return str(exp)


def show_surface_diagnostics(df_full: pd.DataFrame,
                             surface_filters: tuple | None,
                             algo_config: tuple | None) -> None:
    """Render the surface-fit diagnostics expander for the full chain."""
    if df_full is None or df_full.empty:
        return

    surface_filters = surface_filters or ()
    algo_name = algo_config[0] if algo_config else "global_poly"
    algo_label = iv_algorithms.REGISTRY.get(
        algo_name, {}).get("label", algo_name)

    total = len(df_full)
    in_fit = int(df_full["in_fit"].sum()) if "in_fit" in df_full.columns else 0
    sigma = _residual_sigma_pp(df_full)
    all_none = ("fit_method" in df_full.columns
                and (df_full["fit_method"] == "none").all())

    caption = f"**{algo_label}** · fit on {in_fit:,} / {total:,} contracts"
    if np.isfinite(sigma):
        caption += f" · residual σ {sigma:.1f} pp"

    with st.expander("🔬 Surface-fit diagnostics", expanded=False):
        st.caption(caption)
        st.markdown(
            "<div style='font-size:0.82rem;color:var(--osc-ink-3);"
            "line-height:1.6'>The surface is fit on the <b>full fetched "
            "chain</b> using the surface-fit filters below — a wider net than "
            "the delta slider that filters the <i>displayed</i> table and "
            "dots. Rows that pass every filter anchor the regression; the "
            "rest still get an <code>IV+pp</code> read from that fit.</div>",
            unsafe_allow_html=True,
        )
        if all_none:
            st.warning(
                "No expiration could be fit — the surface fell back to "
                "tracing the quotes (`IV+pp ≈ 0`). The funnel below shows "
                "where contracts dropped out; widen the DTE range or relax "
                "the surface-fit filters.",
                icon="⚠️",
            )

        # ── Filter funnel ─────────────────────────────────────────────────
        st.markdown("**Filter funnel** — contracts feeding the fit")
        funnel_rows: list[tuple[str, int, int]] = [
            ("Fetched (within DTE range)", total, 0),
        ]
        funnel_rows += iv_filters.funnel(
            df_full, iv_filters.with_sanity(surface_filters))
        funnel_df = pd.DataFrame(
            funnel_rows, columns=["Stage", "Remaining", "Dropped"])
        st.dataframe(
            funnel_df, hide_index=True, width="stretch",
            height=df_height(funnel_df),
            column_config={
                "Stage": st.column_config.TextColumn("Stage", width=260),
                "Remaining": st.column_config.NumberColumn(
                    "Remaining", format="%d", width=100,
                    help="Contracts still eligible for the fit after this "
                         "stage."),
                "Dropped": st.column_config.NumberColumn(
                    "Dropped", format="%d", width=90,
                    help="Contracts this stage removed."),
            },
        )

        # ── Per-expiration fit status ─────────────────────────────────────
        st.markdown("**Per-expiration fit status**")
        group_key = "expiration" if "expiration" in df_full.columns else "dte"
        recs = []
        for exp, g in df_full.groupby(group_key):
            method = (str(g["fit_method"].iloc[0])
                      if "fit_method" in g.columns else "")
            recs.append({
                "Expiration": _fmt_exp(exp) if group_key == "expiration"
                else str(exp),
                "DTE": int(g["dte"].iloc[0]) if "dte" in g.columns else 0,
                "Contracts": len(g),
                "In fit": int(g["in_fit"].sum())
                if "in_fit" in g.columns else 0,
                "Fit": _METHOD_LABEL.get(method, method),
                "_raw_method": method,
                "Resid σ (pp)": _residual_sigma_pp(g),
            })
        exp_df = pd.DataFrame(recs).sort_values("DTE").reset_index(drop=True)
        raw_methods = exp_df.pop("_raw_method")

        def _flag_fallback(_row: pd.Series) -> list[str]:
            warn = raw_methods.iloc[_row.name] in _FALLBACK_METHODS
            return [CELL_WARN if warn else ""] * len(_row)

        styled = exp_df.style.apply(_flag_fallback, axis=1)
        st.dataframe(
            styled, hide_index=True, width="stretch",
            height=df_height(styled),
            column_config={
                "Expiration": st.column_config.TextColumn(
                    "Expiration", width=105),
                "DTE": st.column_config.NumberColumn("DTE", format="%d",
                                                     width=55),
                "Contracts": st.column_config.NumberColumn(
                    "Contracts", format="%d", width=90),
                "In fit": st.column_config.NumberColumn(
                    "In fit", format="%d", width=70,
                    help="Contracts at this expiration that passed the "
                         "surface-fit filters and anchored the regression."),
                "Fit": st.column_config.TextColumn(
                    "Fit", width=170,
                    help="How this expiration's reference line was produced. "
                         "Highlighted rows borrowed a fit (fallback) or none "
                         "at all — the line may not reflect this expiry's "
                         "own smile."),
                "Resid σ (pp)": st.column_config.NumberColumn(
                    "Resid σ (pp)", format="%.1f", width=100,
                    help="Spread of in-fit IV+pp at this expiration. 0.0 "
                         "means the line passes through the points (too few "
                         "to regress)."),
            },
        )
