"""Generate self-contained HTML reports (single ticker and portfolio)."""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from options_scanner.format import fmt_strike

log = logging.getLogger(__name__)

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #f0f3f7;
       color: #222; padding: 1.5em; }
h1 { font-size: 1.4em; font-weight: 700; }
h2 { font-size: 1.05em; font-weight: 600; color: #444;
     margin: 1.8em 0 0.5em; text-transform: uppercase;
     letter-spacing: .05em; }
h3 { font-size: 1em; font-weight: 600; color: #333; margin-bottom: .4em; }
.card { background: #fff; border-radius: 8px;
        box-shadow: 0 1px 4px rgba(0,0,0,.1); padding: 1.2em 1.5em;
        margin-bottom: 1.2em; }
.meta { display: flex; flex-wrap: wrap; gap: 1.2em;
        margin-top: .6em; font-size: .9em; color: #555; }
.meta strong { color: #222; }
.tag-earn { color: #b94; font-weight: 600; }
.tag-sell { color: #2980b9; font-weight: 600; }
.tag-buy  { color: #27ae60; font-weight: 600; }
.roll-note { font-size: .85em; color: #666; margin-bottom: .5em; }
table { width: 100%; border-collapse: collapse; font-size: .875em; }
thead th { background: #2c3e50; color: #fff; padding: .55em .9em;
           text-align: right; white-space: nowrap; cursor: pointer;
           user-select: none; position: sticky; top: 0; }
thead th:first-child { text-align: left; }
thead th.sort-asc::after  { content: " \25b2"; font-size: .7em; }
thead th.sort-desc::after { content: " \25bc"; font-size: .7em; }
tbody tr:nth-child(even) { background: #f7f9fb; }
tbody tr:hover { background: #edf3fa; }
td { padding: .45em .9em; text-align: right; border-bottom: 1px solid #eee; }
td:first-child { text-align: left; font-weight: 600; }
.row-count { font-size: .8em; color: #888; margin-top: .4em; }
/* IV+pp colours — sell mode */
.iv-s3 { color: #c0392b; font-weight: 700; }
.iv-s2 { color: #e67e22; font-weight: 600; }
.iv-s1 { color: #e8a020; }
.iv-s0 { color: #aaa; }
/* IV+pp colours — buy mode */
.iv-b3 { color: #27ae60; font-weight: 700; }
.iv-b2 { color: #2980b9; font-weight: 600; }
.iv-b1 { color: #5dade2; }
.iv-b0 { color: #aaa; }
.guide { font-size: .875em; }
.guide dt { font-weight: 600; margin-top: .8em; }
.guide dd { margin-left: 1.4em; color: #555; line-height: 1.5; }
code { font-family: ui-monospace, monospace; background: #e8ecf1;
       padding: .1em .35em; border-radius: 3px; font-size: .875em; }
.h1-meta { font-weight: 400; color: #777; margin-left: .6em; }
.settings-bar { padding: .55em 1.5em !important;
                margin-bottom: .6em !important; }
.refine-intro { font-size: .9em; color: #444; line-height: 1.6; }
.refine-list { font-size: .82em; color: #555; margin-top: .4em;
               padding-left: 1.4em; line-height: 1.8; }
details { margin-top: 1em; }
summary { cursor: pointer; font-size: .85em; font-weight: 600;
          color: #2980b9; user-select: none; }
summary:hover { color: #1a5276; }
.details-body { margin-top: .8em; padding-top: .8em;
                border-top: 1px solid #eee; }
/* Portfolio-specific */
.badge { display: inline-block; padding: .15em .55em; border-radius: 10px;
         font-size: .8em; font-weight: 600; margin-left: .5em;
         vertical-align: middle; }
.badge-covered   { background: #d5f5e3; color: #1e8449; }
.badge-uncovered { background: #ebf5fb; color: #1a5276; }
.open-call-note { background: #fef9e7; border-left: 3px solid #f39c12;
                  padding: .5em 1em; margin-bottom: .8em; font-size: .85em; }
.position-block { border-top: 2px solid #dde3ea; padding-top: .8em;
                  margin-top: 1.6em; }
.summary-table thead th { background: #4a4a4a; }
.summary-table tbody td { font-weight: normal; }
.nav-link { font-size: .8em; color: #888; text-decoration: none;
            float: right; margin-top: .2em; }
.nav-link:hover { color: #444; }
"""

_JS = """
function sortTable(id, col, numeric) {
  var tbl = document.getElementById(id);
  var asc = tbl.dataset.sortCol != col ||
            tbl.dataset.sortDir == 'desc';
  var rows = Array.from(tbl.querySelectorAll('tbody tr'));
  rows.sort(function(a, b) {
    var av = a.cells[col].dataset.val || a.cells[col].textContent.trim();
    var bv = b.cells[col].dataset.val || b.cells[col].textContent.trim();
    if (numeric) {
      av = parseFloat(av) || 0; bv = parseFloat(bv) || 0;
      return asc ? av - bv : bv - av;
    }
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  });
  var tb = tbl.querySelector('tbody');
  rows.forEach(function(r) { tb.appendChild(r); });
  tbl.dataset.sortCol = col;
  tbl.dataset.sortDir = asc ? 'asc' : 'desc';
  tbl.querySelectorAll('th').forEach(function(th) {
    th.classList.remove('sort-asc', 'sort-desc');
  });
  tbl.querySelectorAll('th')[col].classList.add(asc ? 'sort-asc' : 'sort-desc');
}
"""


def _iv_class(excess_pct: float, buy: bool) -> str:
    if buy:
        if excess_pct <= -5:  return "iv-b3"
        if excess_pct <= -3:  return "iv-b2"
        if excess_pct < 0:    return "iv-b1"
        return "iv-b0"
    else:
        if excess_pct >= 5:   return "iv-s3"
        if excess_pct >= 3:   return "iv-s2"
        if excess_pct >= 1:   return "iv-s1"
        return "iv-s0"


def _fmt_exp(exp_str: str) -> str:
    return datetime.strptime(exp_str, "%Y-%m-%d").strftime("%b %d '%y")


def _col_headers(has_net_credit: bool) -> list[tuple[str, bool]]:
    cols = [
        ("Strike", True), ("Expiration", False), ("DTE", True),
        ("Bid", True), ("Ask", True), ("Mid", True), ("Last", True),
        ("IV%", True), ("IV+pp", True), ("Delta", True),
        ("Ann%", True), ("OI", True), ("Vol", True),
    ]
    if has_net_credit:
        cols.append(("NetCr", True))
    return cols


def _build_table(sub, table_id: str, buy: bool,
                 roll_close_cost: float | None) -> str:
    has_net = roll_close_cost is not None
    cols = _col_headers(has_net)

    ths = "".join(
        f'<th onclick="sortTable(\'{table_id}\',{i},{"true" if num else "false"})">'
        f"{label}</th>"
        for i, (label, num) in enumerate(cols)
    )

    rows_html = []
    for _, r in sub.iterrows():
        iv_ex = r["iv_excess"] * 100
        net_cr = r["mid"] - roll_close_cost if has_net else None

        cells = [
            f'<td data-val="{r["strike"]:.2f}">{fmt_strike(r["strike"])}</td>',
            f'<td data-val="{r["expiration"]}">{_fmt_exp(r["expiration"])}</td>',
            f'<td data-val="{r["dte"]}">{int(r["dte"])}</td>',
            f'<td data-val="{r["bid"]:.4f}">${r["bid"]:.2f}</td>',
            f'<td data-val="{r["ask"]:.4f}">${r["ask"]:.2f}</td>',
            f'<td data-val="{r["mid"]:.4f}">${r["mid"]:.2f}</td>',
            (f'<td data-val="{r["last"]:.4f}">${r["last"]:.2f}</td>'
             if r.get("last", 0) > 0 else '<td data-val="0">&mdash;</td>'),
            f'<td data-val="{r["iv"]*100:.4f}">{r["iv"]*100:.1f}</td>',
            f'<td data-val="{iv_ex:.4f}" class="{_iv_class(iv_ex, buy)}">{iv_ex:+.1f}</td>',
            f'<td data-val="{r["delta"]:.4f}">{r["delta"]:.2f}</td>',
            f'<td data-val="{r["ann_yield_pct"]:.4f}">{r["ann_yield_pct"]:.1f}</td>',
            f'<td data-val="{r["open_interest"]}">{r["open_interest"]:,}</td>',
            f'<td data-val="{r["volume"]}">{int(r["volume"]):,}</td>',
        ]
        if has_net:
            cells.append(f'<td data-val="{net_cr:.4f}">${net_cr:+.2f}</td>')
        rows_html.append(f"<tr>{''.join(cells)}</tr>")

    return (
        f'<table id="{table_id}" data-sort-col="-1">'
        f"<thead><tr>{ths}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
        f'<p class="row-count">{len(sub)} options shown</p>'
    )


def _guide_html(buy: bool) -> str:
    if buy:
        return """
<dl class="guide">
  <dt>IV+pp</dt>
  <dd>Negative = option's IV sits below the fitted surface (IV-cheap
  relative to neighbors). Under &minus;3pp: meaningful ranking
  signal. Under &minus;5pp: strong.</dd>
  <dt>Delta</dt>
  <dd>Probability the option expires in the money (profitable).
  Higher delta = more likely to profit, but costs more.</dd>
  <dt>Ann%</dt>
  <dd>Annualized cost as % of underlying. Lower = cheaper.</dd>
</dl>"""
    else:
        return """
<dl class="guide">
  <dt>IV+pp</dt>
  <dd>How many percentage points above the fitted surface. Higher =
  IV-rich relative to neighbors. Under 3pp: chain's IV is roughly
  uniform, no strike stands out. Over 5pp: stronger signal.</dd>
  <dt>Delta</dt>
  <dd>~Probability of expiring ITM (assignment). Lower = safer,
  less premium.</dd>
  <dt>Ann%</dt>
  <dd>Annualized yield. Calls: vs. spot. Puts: vs. strike.</dd>
  <dt>NetCr (roll mode)</dt>
  <dd>New mid minus close cost. Positive = net credit roll.</dd>
  <dt>LT capital gains</dt>
  <dd>Close after the LT date shown to get long-term rates on premium.</dd>
</dl>"""


_ALGO_LABELS = {
    "global_poly": "Global polynomial",
    "per_expiration": "Per-expiration",
}
_SCORE_LABELS = {
    "raw_pp": "IV excess (pp above surface)",
    "zscore": "Z-score vs. surface neighbors",
    "relative": "Relative IV excess",
    "composite_exec": "Composite execution score",
    "vrp": "Volatility risk premium",
    "percentile": "Historical IV percentile",
}


def _scan_params_html(scan_params: dict, buy: bool, ticker: str) -> str:
    if not scan_params:
        return ""

    ds = scan_params.get("data_source", "yahoo").capitalize()
    preset = scan_params.get("preset", "current")
    algo = scan_params.get("algorithm", "global_poly")
    score = scan_params.get("score", "raw_pp")
    min_dte = scan_params.get("min_dte", 30)
    max_dte = scan_params.get("max_dte", 90)
    min_delta = scan_params.get("min_delta", 0.10)
    max_delta = scan_params.get("max_delta", 0.75)
    min_oi = scan_params.get("min_oi", 25)
    min_vol = scan_params.get("min_vol", 10)
    top = scan_params.get("top", 10)

    algo_label = _ALGO_LABELS.get(algo, algo)
    score_label = _SCORE_LABELS.get(score, score)
    side_flag = "--puts" if scan_params.get("mode") == "put" else "--calls"
    buy_flag = " --buy" if buy else ""
    base = (f"uv run options-scanner/run_scanner.py {ticker}"
            f" {side_flag}{buy_flag}")
    dte_flags = f"--min-dte {min_dte} --max-dte {max_dte}"
    delta_flags = f"--min-delta {min_delta:.2f} --max-delta {max_delta:.2f}"

    examples = [
        (f"{base} {dte_flags} --min-delta 0.25 --max-delta 0.35 --browser",
         "Tighter delta (0.25&ndash;0.35)"),
        (f"{base} --min-dte 40 --max-dte 50 {delta_flags} --browser",
         "Narrow DTE window (40&ndash;50)"),
        (f"{base} {dte_flags} {delta_flags} --min-oi 100 --browser",
         "Higher liquidity bar (OI &ge; 100)"),
        (f"{base} {dte_flags} {delta_flags} --min-vol 50 --browser",
         "Active strikes only (volume &ge; 50)"),
        (f"{base} {dte_flags} {delta_flags} --min-ivpp 2 --browser",
         "Only IV-rich outliers (&ge;2pp above surface)"),
        (f"{base} {dte_flags} {delta_flags} --top 20 --browser",
         "Show top 20 results"),
        (f"{base} {dte_flags} {delta_flags} --preset v2 --browser",
         "Try the v2 surface model (per-expiration, z-score)"),
        (f"{base} {dte_flags} {delta_flags} --data-source yahoo --browser",
         "Switch to Yahoo Finance data"),
    ]
    ex_html = "".join(
        f'<li><code>{cmd}</code> &mdash; {label}</li>'
        for cmd, label in examples
    )

    return f"""
<div class="card">
  <h2 style="margin-top:0">Ask Claude to set these ranges to your preference</h2>
  <p class="refine-intro">
    <strong>Delta range</strong> &middot;
    <strong>DTE range</strong> &middot;
    <strong>Min OI</strong> &middot;
    <strong>Min Volume</strong> &middot;
    <strong>Top N results</strong> &middot;
    <strong>Data Source</strong> (Yahoo Finance, Schwab, Moomoo)
  </p>
  <details>
    <summary>Technical details &amp; CLI commands</summary>
    <div class="details-body">
      <dl class="guide" style="margin-top:0">
        <dt>Delta range <span style="font-weight:400;color:#888">(current: {min_delta:.2f}&ndash;{max_delta:.2f})</span></dt>
        <dd>Controls which strikes appear. Lower delta = further OTM, less
        premium, lower assignment risk. Higher delta = closer to the money,
        more premium, higher risk of being called away. A 0.25&ndash;0.35
        window focuses tightly on the conventional covered-call zone.</dd>
        <dt>DTE range <span style="font-weight:400;color:#888">(current: {min_dte}&ndash;{max_dte})</span></dt>
        <dd>Controls which expirations are included. Shorter DTE (21&ndash;30)
        maximises theta decay rate but leaves less time to manage. The
        45&ndash;60 DTE zone is the most-cited sweet spot for covered calls.
        Narrowing the window (e.g. 40&ndash;50) targets a single cycle more
        precisely.</dd>
        <dt>Min open interest <span style="font-weight:400;color:#888">(current: {min_oi})</span></dt>
        <dd>Filters out illiquid strikes. Higher OI typically means tighter
        bid&ndash;ask spreads and easier fills. For high-volume tickers like
        NVDA the default of 25 is very permissive; asking for 100+ surfaces
        only the most-traded strikes.</dd>
        <dt>Min volume <span style="font-weight:400;color:#888">(current: {min_vol})</span></dt>
        <dd>Today&rsquo;s trading activity at each strike. Volume confirms
        current liquidity beyond the standing OI. Raising this (e.g. 50&ndash;100)
        keeps only strikes where real trading is happening today.</dd>
        <dt>Min IV premium (IV+pp)</dt>
        <dd>Only show options sitting at least N percentage points above the
        fitted surface. Useful when the chain is flat and you only want
        strikes with a meaningful IV edge. When IV+pp values are all near
        zero (as here), raising this threshold will return few or no results
        &mdash; that itself is informative: the chain is uniformly priced.</dd>
        <dt>Top N results <span style="font-weight:400;color:#888">(current: {top})</span></dt>
        <dd>Maximum rows shown per option type. Increase if you want to see
        further-OTM or lower-premium alternatives beyond the default 10.</dd>
        <dt>Data source <span style="font-weight:400;color:#888">(current: {ds})</span></dt>
        <dd>Schwab provides live quotes (requires active auth token); Yahoo
        Finance is free and slightly delayed but needs no credentials. If
        prices look stale, switching sources is the first thing to try.</dd>
        <dt>Surface model / preset</dt>
        <dd><strong>current</strong> &mdash; fits a single global polynomial
        across all expirations, ranks by raw IV excess (IV+pp). Simple and
        fast.<br>
        <strong>v2</strong> &mdash; fits each expiration independently using
        spread-weighted regression, ranks by z-score within each expiration.
        Better at isolating outliers when term structure is steep or earnings
        distort one expiration.</dd>
      </dl>
      <p style="margin-top:1.2em;font-size:.8em;color:#888">Example CLI commands (run from repo root):</p>
      <ul class="refine-list">{ex_html}</ul>
    </div>
  </details>
</div>"""


# ── Single-ticker report ─────────────────────────────────────────────────────

def render_html(
    df,
    ticker: str,
    spot: float,
    earnings_dates: list,
    mode: str,
    buy: bool,
    roll_close_cost: float | None,
    min_oi: int,
    min_vol: int = 0,
    scan_params: dict | None = None,
    top_n: int | None = None,
) -> str:
    """Return the single-ticker HTML report as a string."""
    today = date.today()
    now = datetime.now()
    lt_date = (today + timedelta(days=366)).strftime("%b %d '%y")
    is_roll = roll_close_cost is not None
    action_label = "Buy" if buy else ("Roll" if is_roll else "Sell")
    type_label = {"call": "Calls", "put": "Puts"}.get(mode, "Calls & Puts")
    scan_date = now.strftime("%B %d, %Y %H:%M")

    df = df[(df["open_interest"] >= min_oi)
            & (df["volume"] >= min_vol)].copy()

    earn_html = ""
    if earnings_dates:
        earn_html = (f'<span class="tag-earn">&#9656; Next earnings: '
                     f'{earnings_dates[0].strftime("%b %d")}</span>')

    action_cls = "tag-buy" if buy else "tag-sell"
    type_labels = {"call": "Calls", "put": "Puts"}
    to_show = [mode] if mode in type_labels else list(type_labels.keys())
    iv_asc = buy

    sections_html = ""
    for i, opt_type in enumerate(to_show):
        label = type_labels[opt_type]
        sub = (df[df["type"] == opt_type]
               .sort_values(["iv_excess", "open_interest"], ascending=[iv_asc, False]))
        if top_n:
            sub = sub.head(top_n)
        if sub.empty:
            continue
        table_id = f"tbl-{opt_type}-{i}"
        roll_note = ""
        if roll_close_cost is not None:
            roll_note = (f'<p class="roll-note">Buy-back cost: '
                         f'<strong>${roll_close_cost:.2f}</strong> mid &mdash; '
                         f'Net credit = new premium &minus; '
                         f'<strong>${roll_close_cost:.2f}</strong></p>')
        sections_html += (
            f"<h2>{label}</h2>{roll_note}"
            f'<div class="card" style="padding:0;overflow:auto">'
            f"{_build_table(sub, table_id, buy, roll_close_cost)}"
            f"</div>"
        )

    if scan_params is not None:
        scan_params = dict(scan_params, mode=mode)

    ds_label = (scan_params.get("data_source", "yahoo").capitalize()
                if scan_params else "")
    algo = scan_params.get("algorithm", "global_poly") if scan_params else ""
    score = scan_params.get("score", "raw_pp") if scan_params else ""
    algo_label = _ALGO_LABELS.get(algo, algo)
    score_label = _SCORE_LABELS.get(score, score)
    surface_html = (
        f'<span>Data: <strong>{ds_label}</strong></span>'
        f'<span>Surface: <strong>{algo_label} &middot; {score_label}</strong></span>'
        if scan_params else ""
    )

    roll_meta_html = ""
    if is_roll and scan_params and scan_params.get("roll_strike"):
        rtype = (scan_params.get("roll_type") or "").capitalize()
        rstrike = scan_params["roll_strike"]
        rexp = scan_params.get("roll_expiration") or ""
        rexp_fmt = _fmt_exp(rexp) if rexp else rexp
        close_str = f"${roll_close_cost:.2f}" if roll_close_cost is not None else "—"
        roll_meta_html = (
            f'<span>Rolling: <strong>{fmt_strike(rstrike)} {rexp_fmt} {rtype}</strong>'
            f' &mdash; Buy-back: <strong>{close_str}</strong></span>'
        )

    settings_bar_html = ""
    if scan_params:
        p = scan_params
        lo = p.get("min_strike")
        hi = p.get("max_strike")
        strike_span = (
            f'<span>Strike: <strong>'
            f'{"$"+str(int(lo)) if lo else ""}&ndash;{"$"+str(int(hi)) if hi else ""}'
            f'</strong></span>'
            if (lo or hi) else ""
        )
        settings_bar_html = (
            f'<div class="card settings-bar">'
            f'<div class="meta" style="margin-top:0;font-size:.82em">'
            f'<span>DTE: <strong>{p.get("min_dte",30)}&ndash;{p.get("max_dte",90)}</strong></span>'
            f'<span>Delta: <strong>{p.get("min_delta",.10):.2f}&ndash;{p.get("max_delta",.75):.2f}</strong></span>'
            f'{strike_span}'
            f'<span>Min OI: <strong>{p.get("min_oi",25)}</strong></span>'
            f'<span>Min Vol: <strong>{p.get("min_vol",10)}</strong></span>'
            f'<span>Top N: <strong>{p.get("top",10)}</strong></span>'
            f'</div></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{ticker} {action_label} {type_label} &mdash; {scan_date}</title>
  <style>{_CSS}</style>
</head>
<body>
<div class="card">
  <h1>{ticker} <span class="h1-meta">(${spot:.2f})</span> &mdash; <span class="{action_cls}">{action_label} {type_label}</span><span class="h1-meta"> &middot; {scan_date}</span></h1>
  <div class="meta">
    {surface_html}
    {roll_meta_html}
    <span>LT close if opened today: <strong>{lt_date}</strong></span>
    {earn_html}
  </div>
</div>
{settings_bar_html}{sections_html}
{_scan_params_html(scan_params, buy, ticker)}
<div class="card">
  <h2 style="margin-top:0">How to read this report</h2>
  {_guide_html(buy)}
</div>
<script>{_JS}</script>
</body>
</html>"""


def save_html(
    df,
    ticker: str,
    spot: float,
    earnings_dates: list,
    mode: str,
    buy: bool,
    roll_close_cost: float | None,
    min_oi: int,
    output_path: Path,
    min_vol: int = 0,
    scan_params: dict | None = None,
    top_n: int | None = None,
) -> None:
    html = render_html(df, ticker, spot, earnings_dates, mode, buy,
                       roll_close_cost, min_oi, min_vol, scan_params, top_n)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("HTML report saved: %s", output_path)


# ── Portfolio report ─────────────────────────────────────────────────────────

# Per-ticker leaderboard row count — keep in sync with
# display.leaderboard._ROWS_PER_TICKER (kept local so the CLI/HTML path doesn't
# import the streamlit module).
_ROWS_PER_TICKER = 3


def _leaderboard_html(results: list[dict], side: str, min_oi: int,
                      top_n: int, min_vol: int,
                      delta_range: tuple[float, float] | None = None,
                      buy: bool = False) -> str:
    """Cross-ticker leaderboard table for one side, mirroring the UI.

    Every ticker contributes its top 3 contracts by IV+pp, grouped by ticker
    so a calm name is represented as fully as an earnings-heavy one. The
    tickers are ordered by their single richest contract, and within a ticker
    the rows run best-first. Each ticker's #1 row is shaded. Returns "" when
    nothing qualifies.
    """
    import pandas as pd

    per_ticker = []
    for res in results:
        if res["error"] or res["df"] is None or res["df"].empty:
            continue
        df = res["df"]
        sub = df[(df["type"] == side)
                 & (df["open_interest"] >= min_oi)
                 & (df["volume"] >= min_vol)]
        if delta_range is not None:
            lo, hi = delta_range
            sub = sub[sub["delta"].abs().between(lo, hi)]
        if sub.empty:
            continue
        sub = (sub.sort_values(["iv_excess", "open_interest"],
                               ascending=[buy, False]).head(_ROWS_PER_TICKER).copy())
        sub["ticker"] = res["position"]["ticker"]
        sub["_is_ticker_top"] = [True] + [False] * (len(sub) - 1)
        per_ticker.append(sub.reset_index(drop=True))
    if not per_ticker:
        return ""

    board = pd.concat(per_ticker, ignore_index=True)
    # Order tickers by their single best contract's IV+pp, keep each ticker's
    # rows together (the `ticker` tiebreaker), and within a ticker keep
    # best-first.
    board["_rank"] = board.groupby("ticker")["iv_excess"].transform(
        "min" if buy else "max")
    board = (board.sort_values(["_rank", "ticker", "iv_excess", "open_interest"],
                               ascending=[buy, True, buy, False], kind="stable")
             .drop(columns="_rank"))

    rows = []
    for _, r in board.iterrows():
        iv_ex = r["iv_excess"] * 100
        tr_attr = ' style="background-color:rgba(53,194,193,0.16)"' \
            if r.get("_is_ticker_top") else ""
        rows.append(
            f'<tr{tr_attr}><td><a href="#{r["ticker"]}">{r["ticker"]}</a></td>'
            f'<td data-val="{r["strike"]:.2f}">{fmt_strike(r["strike"])}</td>'
            f'<td data-val="{r["expiration"]}">{_fmt_exp(r["expiration"])}</td>'
            f'<td data-val="{r["dte"]}">{int(r["dte"])}</td>'
            f'<td data-val="{r["bid"]:.4f}">${r["bid"]:.2f}</td>'
            f'<td data-val="{r["ask"]:.4f}">${r["ask"]:.2f}</td>'
            f'<td data-val="{r["mid"]:.4f}">${r["mid"]:.2f}</td>'
            + (f'<td data-val="{r["last"]:.4f}">${r["last"]:.2f}</td>'
               if r.get("last", 0) > 0 else '<td data-val="0">&mdash;</td>')
            + f'<td data-val="{iv_ex:.4f}" class="{_iv_class(iv_ex, buy)}">{iv_ex:+.1f}</td>'
            f'<td data-val="{r["delta"]:.4f}">{r["delta"]:.2f}</td>'
            f'<td data-val="{r["ann_yield_pct"]:.4f}">{r["ann_yield_pct"]:.1f}</td>'
            f'<td data-val="{r["open_interest"]}">{r["open_interest"]:,}</td>'
            f'<td data-val="{r["volume"]}">{int(r["volume"]):,}</td></tr>'
        )

    tid = f"tbl-lead-{side}"
    heads = [("Ticker", False), ("Strike", True), ("Expiration", False),
             ("DTE", True), ("Bid", True), ("Ask", True), ("Mid", True),
             ("Last", True), ("IV+pp", True), ("Delta", True),
             ("Ann%", True), ("OI", True), ("Vol", True)]
    ths = "".join(
        f'<th onclick="sortTable(\'{tid}\',{i},{"true" if num else "false"})">'
        f"{label}</th>"
        for i, (label, num) in enumerate(heads)
    )
    label = {"call": "Calls", "put": "Puts"}[side]
    return (
        f'<h3 style="margin:.6em 0 .3em">{label}</h3>'
        f'<div style="overflow:auto">'
        f'<table id="{tid}" data-sort-col="-1">'
        f"<thead><tr>{ths}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        f"</div>"
    )


def render_portfolio_html(
    results: list[dict],
    csv_name: str,
    min_oi: int,
    top_n: int,
    min_vol: int = 0,
    opt_type: str = "calls",
    delta_range: tuple[float, float] | None = None,
    buy: bool = False,
) -> str:
    """Return a combined portfolio HTML report as a string."""
    today = date.today()
    scan_date = today.strftime("%B %d, %Y")
    lt_date = (today + timedelta(days=366)).strftime("%b %d '%y")

    # Cross-ticker leaderboard section (only meaningful with >1 ticker).
    leaderboard_html = ""
    if len(results) > 1:
        sides = (["call", "put"] if opt_type == "both"
                 else ["call" if opt_type == "calls" else "put"])
        boards = "".join(
            _leaderboard_html(results, s, min_oi, top_n, min_vol, delta_range,
                              buy)
            for s in sides
        )
        if boards:
            leaderboard_html = (
                '<div class="card">'
                '<h2 style="margin-top:0">Leaderboard '
                '<span style="font-weight:400;font-size:.7em;color:#666">'
                '— richest IV+pp across all tickers</span></h2>'
                f"{boards}</div>"
            )

    # Summary table rows
    summary_rows = ""
    for res in results:
        pos = res["position"]
        ticker = pos["ticker"]
        spot = res["spot"]
        spot_str = f"${spot:.2f}" if spot else "—"
        if res["error"]:
            status_html = '<span style="color:#c0392b">Error</span>'
            calls_str = res["error"]
        elif pos["open_calls"]:
            status_html = '<span class="badge badge-covered">Covered</span>'
            calls_str = "; ".join(
                f'{o["strike"]} {o["expiration"]} ({o["contracts"]}x)'
                for o in pos["open_calls"]
            )
        else:
            status_html = '<span class="badge badge-uncovered">Uncovered</span>'
            calls_str = "—"
        summary_rows += (
            f'<tr><td><a href="#{ticker}">{ticker}</a></td>'
            f"<td>{pos['shares']:g}</td><td>{spot_str}</td>"
            f"<td>{status_html}</td><td>{calls_str}</td></tr>"
        )

    # Position sections
    sections_html = ""
    tbl_counter = 0
    for res in results:
        pos = res["position"]
        ticker = pos["ticker"]
        covered = bool(pos["open_calls"])
        badge = ('<span class="badge badge-covered">Covered</span>'
                 if covered else
                 '<span class="badge badge-uncovered">Uncovered</span>')

        open_calls_html = ""
        for opt in pos["open_calls"]:
            close = res["roll_close_costs"].get(opt["symbol"])
            close_str = f" &mdash; close mid: <strong>${close:.2f}</strong>" if close else ""
            open_calls_html += (
                f'<p class="open-call-note">Open call: <strong>{opt["symbol"]}</strong> '
                f'&mdash; {opt["contracts"]} contract(s){close_str}</p>'
            )

        if res["error"] or res["spot"] is None or res["df"].empty:
            msg = res["error"] or "No options data returned."
            body_html = f'<p style="color:#c0392b">{msg}</p>'
        else:
            spot = res["spot"]
            earnings_dates = res["earnings_dates"]
            df = res["df"][(res["df"]["open_interest"] >= min_oi)
                           & (res["df"]["volume"] >= min_vol)].copy()

            earn_str = ""
            if earnings_dates:
                earn_str = (f' &nbsp;|&nbsp; <span class="tag-earn">Next earnings: '
                            f'{earnings_dates[0].strftime("%b %d")}</span>')

            roll_close = None
            if pos["open_calls"]:
                first = pos["open_calls"][0]
                roll_close = res["roll_close_costs"].get(first["symbol"])
                if len(pos["open_calls"]) > 1:
                    open_calls_html += (
                        '<p class="roll-note">Multiple open calls — '
                        "showing roll vs. first position above.</p>"
                    )

            sub = (df[df["type"] == "call"]
                   .sort_values(["iv_excess", "open_interest"], ascending=[buy, False])
                   .head(top_n))

            roll_note = ""
            if roll_close is not None:
                roll_note = (f'<p class="roll-note">Close cost (mid): '
                             f'<strong>${roll_close:.2f}</strong> &mdash; '
                             f"NetCr = new mid &minus; this</p>")

            table_html = ""
            if not sub.empty:
                tbl_id = f"tbl-port-{tbl_counter}"
                tbl_counter += 1
                table_html = (
                    f"{roll_note}"
                    f'<div style="overflow:auto">'
                    f"{_build_table(sub, tbl_id, buy, roll_close)}"
                    f"</div>"
                )
            else:
                table_html = "<p>No options found matching filters.</p>"

            body_html = (
                f'<p class="meta">Spot: <strong>${spot:.2f}</strong>'
                f"{earn_str}</p>"
                f'<div style="margin:.6em 0">{open_calls_html}</div>'
                f"{table_html}"
            )

        sections_html += (
            f'<div class="position-block" id="{ticker}">'
            f'<a class="nav-link" href="#top">&#8593; top</a>'
            f"<h3>{ticker} &mdash; {pos['shares']:g} shares {badge}</h3>"
            f'<div class="card" style="padding:1em 1.5em">{body_html}</div>'
            f"</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Portfolio Scan &mdash; {scan_date}</title>
  <style>{_CSS}</style>
</head>
<body id="top">
<div class="card">
  <h1>Portfolio Scan</h1>
  <div class="meta">
    <span>Source: <strong>{csv_name}</strong></span>
    <span>Scanned: {scan_date}</span>
    <span>LT close if opened today: <strong>{lt_date}</strong></span>
  </div>
</div>
<div class="card" style="padding:0;overflow:auto">
  <table class="summary-table" id="tbl-summary" data-sort-col="-1">
    <thead><tr>
      <th onclick="sortTable('tbl-summary',0,false)">Ticker</th>
      <th onclick="sortTable('tbl-summary',1,true)">Shares</th>
      <th onclick="sortTable('tbl-summary',2,true)">Spot</th>
      <th>Status</th>
      <th>Open Calls</th>
    </tr></thead>
    <tbody>{summary_rows}</tbody>
  </table>
</div>
{leaderboard_html}
{sections_html}
<div class="card">
  <h2 style="margin-top:0">How to read this report</h2>
  {_guide_html(buy)}
</div>
<script>{_JS}</script>
</body>
</html>"""


def save_portfolio_html(
    results: list[dict],
    csv_name: str,
    output_path: Path,
    min_oi: int,
    top_n: int,
    min_vol: int = 0,
    opt_type: str = "calls",
    delta_range: tuple[float, float] | None = None,
    buy: bool = False,
) -> None:
    html = render_portfolio_html(results, csv_name, min_oi, top_n, min_vol,
                                 opt_type=opt_type, delta_range=delta_range,
                                 buy=buy)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("Portfolio HTML report saved: %s", output_path)
