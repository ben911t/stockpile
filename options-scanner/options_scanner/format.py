"""Shared number-formatting helpers.

Leaf module — no internal imports, safe to import anywhere (display,
tabs, report, CLI). Centralizes strike formatting so 2.5-/0.25-wide
strikes (NVDA, TSLA, AAPL, SOFI, …) render their decimals everywhere
instead of being rounded to the nearest dollar.
"""

from __future__ import annotations

def md_escape(text) -> str:
    """Escape dollar signs so markdown renders text literally.

    **Two** unescaped ``$`` anywhere in one markdown string are read as LaTeX
    math delimiters: Streamlit swallows both and reflows everything between them
    into a serif math run. Any string carrying more than one money figure — a
    balance line, a list of legs, a header with a strike *and* a spot — has to
    come through here, and it has bitten three separate places already.

    Use it on the whole assembled string, or on each amount as it's formatted.
    """
    return str(text).replace("$", "\\$")


def money_md(value, decimals: int = 0) -> str:
    """A dollar amount ready to drop into markdown: grouped, and escaped so it
    can share a string with another amount. ``money_md(12500) → "\\$12,500"``."""
    try:
        return md_escape(f"${float(value):,.{int(decimals)}f}")
    except (TypeError, ValueError):
        return "—"


def money_html(v) -> str:
    """A dollar amount for a raw-HTML cell: ``$1,234.56``, or an em dash.

    The HTML twin of `money_md`. Inside `kv_table_html`'s cells the `$` is never
    parsed as a LaTeX delimiter, so this one must NOT escape — an escaped `\\$`
    would render the backslash literally.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"${f:,.2f}" if f == f else "—"      # NaN → em dash


def kv_table_html(rows: "list[tuple[str, str]]", pairs: int = 1) -> str:
    """Borderless key/value HTML table — the leg-snapshot layout, also used by
    the Sell Put snapshot. ($ is safe here: inside raw HTML it's not parsed as
    LaTeX.)

    `pairs` is how many label/value pairs sit side by side per line, so a long
    leg can trade width for height (a 10-row leg at `pairs=2` is 5 lines).
    Filling is **column-major**: the first half of `rows` runs down the left
    pair, the rest down the right. That keeps each column in the order the
    caller built it — on a full leg it lands the prices (Spot/Bid/Ask/Mid/Last)
    in one column and the analytics (IV%/Delta/OI/Vol/IV+pp) in the other, which
    row-major zigzagging would interleave. A short final column is padded with
    empty cells rather than reflowed.
    """
    pairs = max(1, int(pairs))
    per = -(-len(rows) // pairs)           # ceil: lines in the table
    cols = [rows[i * per:(i + 1) * per] for i in range(pairs)]
    lines = []
    for r in range(per):
        cells = []
        for c, col in enumerate(cols):
            if r >= len(col):
                cells.append("<td></td><td></td>")
                continue
            f, v = col[r]
            # Extra left padding opens a gutter before every pair but the
            # first, so four columns read as two groups instead of one jumble.
            lead = 28 if c else 14
            cells.append(
                f"<td style='padding:4px 14px 4px {lead}px;color:#808495'>{f}</td>"
                f"<td style='padding:4px 14px;font-variant-numeric:tabular-nums'>"
                f"{v}</td>")
        lines.append("<tr>" + "".join(cells) + "</tr>")
    return ("<table style='border-collapse:collapse;font-size:1rem'>"
            f"{''.join(lines)}</table>")


# Color bands for the IV+pp of a leg you're BUYING BACK: IV *below* the fitted
# surface means a cheap buyback (green), above it means you're paying up (red).
# That's the opposite of the usual "rich is green" reading, which applies when
# you're SELLING premium — the favorable direction depends on which side of the
# trade you're on. Every screen here (close, roll, unwind) is a buyback, so they
# all read the same way. Breaks at ±3 pp, the same noise floor the chain table
# uses (chain_table._NOISE), so a value that's really just noise can't render as
# a strong signal either way.
IV_PP_BANDS = ((-3.0, "#16a34a"), (0.0, "#ca8a04"), (3.0, "#ea580c"))
IV_PP_HIGH = "#dc2626"


def iv_pp_color(pp: float) -> str:
    """Hex color for a buyback leg's IV+pp — greener the cheaper the buyback,
    redder the further above the surface you're paying."""
    for ceiling, color in IV_PP_BANDS:
        if pp <= ceiling:
            return color
    return IV_PP_HIGH


def _known(v) -> bool:
    """True when a value is present and not NaN. pandas hands back NaN for a
    missing numeric far more often than None, and `None != None` is False, so
    both need checking before a value is formatted as though it were a reading."""
    return v is not None and v == v


def leg_rows(bid, ask, mid, last, oi, vol, last_ms=None, iv_pp=None,
             iv=None, delta=None, spot=None, fmt_last_et=None):
    """Key/value rows describing ONE option leg, for `kv_table_html`.

    Shared by every screen that shows a leg before you act on it — the Positions
    close builder, the roll's two-leg confirm, the unwind — so the same contract
    can't be described three different ways. Bid/Ask/Mid/Last/OI/Vol always
    render (em dash when absent); Spot, IV%, Delta and IV+pp appear only when
    supplied, because what's knowable differs by caller.

    "Last" carries its print time (New York) beneath the price when `fmt_last_et`
    is supplied and returns one, so a stale leg is obvious while you're pricing
    against it. It's injected rather than imported to keep this module a leaf.
    """
    rows = []
    if _known(spot):
        rows.append(("Spot", money_html(spot)))
    _last = money_html(last)
    _lt = fmt_last_et(last_ms) if (fmt_last_et and last_ms is not None) else ""
    if _lt:
        _last += f"<br><span style='color:#94a3b8'>{_lt}</span>"
    rows += [("Bid", money_html(bid)), ("Ask", money_html(ask)),
             ("Mid", money_html(mid)), ("Last", _last)]
    if _known(iv):
        rows.append(("IV%", f"{float(iv) * 100:.1f}%"))
    if _known(delta):
        rows.append(("Delta", f"{float(delta):.2f}"))
    rows += [("OI", f"{int(oi):,}" if _known(oi) else "—"),
             ("Vol", f"{int(vol):,}" if _known(vol) else "—")]
    # IV+pp last: it's the interpretive figure, and it reads as a verdict on the
    # raw numbers above it rather than another quote field.
    if _known(iv_pp):
        rows.append(("IV+pp", f"<span style='color:{iv_pp_color(iv_pp)};"
                              f"font-weight:600'>{iv_pp:+.1f} pp</span>"))
    return rows


# d3-format string for option strikes on Altair/Vega and Plotly
# charts. The `~` trims trailing zeros so whole strikes render as
# "$145" while fractional strikes keep their decimals ("$142.5",
# "$12.75"). Mirrors `fmt_strike` below for f-string call sites.
STRIKE_D3_FORMAT = "$,.2~f"

# Legend for the ⚠ marker appended to short-dated, post-earnings
# expiration cells in the ranked tables. Shown as an always-visible
# caption under the table (st.dataframe has no per-cell hover; only the
# column header carries a tooltip), so the meaning isn't hidden.
EARNINGS_WARN_LEGEND = (
    "⚠ next to a date = ≤60 DTE and expiring after the next earnings — its "
    "IV+pp includes event premium (and it's excluded from the surface fit)."
)


def fmt_strike(strike) -> str:
    """Format an option strike as a dollar string.

    Shows decimals only when the strike isn't a whole number:
    145 -> "$145", 142.5 -> "$142.5", 12.75 -> "$12.75". Keeps
    big-ticker integer strikes clean while preserving fractional
    strikes. Mirrors `STRIKE_D3_FORMAT` used on the charts.
    """
    x = float(strike)
    if x.is_integer():
        return f"${x:,.0f}"
    return f"${x:,.2f}".rstrip("0").rstrip(".")


def dte_cell(dte, days_open=None) -> str:
    """A live position's DTE cell: days to expiration, then days-since-open in
    parens. ``dte_cell(18, 44) → "18 (44)"``, ``dte_cell(18) → "18"``.

    The second figure comes from the app's trade log — the broker doesn't report
    when a leg was opened — so a position opened outside the scanner drops the
    parens entirely rather than showing them empty.
    """
    cell = f"{dte}" if dte is not None else "—"
    return f"{cell} ({days_open})" if days_open is not None else cell


def open_prices_cell(stock, option) -> str:
    """The Open cell: what the UNDERLYING cost when the position was opened,
    then what the option itself opened at. ``"$27.40 · $1.10"``.

    Together they say whether a covered call was written into strength or
    weakness. Only this app ever records the stock figure (``fill_spot``, at the
    fill), so a leg opened elsewhere gets "—" in that slot rather than dropping
    it — the option's price stays the number on the right either way. A zero is
    treated as missing: you don't open an option at $0.00.
    """
    def _money(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f"${f:,.2f}" if f and f == f else None

    left, right = _money(stock), _money(option)
    if not left and not right:
        return "—"
    return f"{left or '—'} · {right or '—'}"


def days_since(opened_at) -> int | None:
    """Whole days from an ISO-8601 timestamp to today; None if unparseable.

    Trade records carry `opened_at` as an ISO string, and every call site wants
    the same "how long has this been on?" integer.
    """
    from datetime import date, datetime
    try:
        return (date.today() - datetime.fromisoformat(str(opened_at)).date()).days
    except (TypeError, ValueError):
        return None


def strike_tick_values(strikes, lo=None, hi=None, max_ticks=16):
    """Axis tick positions aligned to the real option strikes.

    Vega-Lite's automatic ticks land on "nice" round steps (1, 2, 5, …), so
    $0.50-/$2.50-wide strikes (7.5, 152.5) never get a labeled tick. Passing
    the actual strikes as the axis `values` forces ticks onto them.

    Restricts to the [lo, hi] domain when given and thins uniformly to at
    most `max_ticks` so wide chains don't crowd the axis (the kept ticks are
    still real strikes). Returns an empty list when there are no strikes, so
    callers can fall back to Vega's default ticks.
    """
    vals = sorted({round(float(s), 4) for s in strikes if s is not None})
    if lo is not None:
        vals = [v for v in vals if v >= lo - 1e-9]
    if hi is not None:
        vals = [v for v in vals if v <= hi + 1e-9]
    if len(vals) > max_ticks:
        step = -(-len(vals) // max_ticks)   # ceil division
        vals = vals[::step]
    return vals
