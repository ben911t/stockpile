"""The spot + day-change readouts on the Trades tab.

Two renderers over one formatting rule: `_day_head_px` (HTML) captions the
intraday chart, `_day_head_md` (markdown) rides in the collapsed row header,
which is a ``st.button`` label and so can't take HTML. They must agree — the
chart line and its caption sit millimeters apart — and a missing quote has to
read as "no quote", not as a price.
"""

from options_scanner.tabs.trades import _day_head_md, _day_head_px

GREEN, RED = "#16a34a", "#dc2626"


def test_shows_spot_and_change():
    out = _day_head_px(34.125, 1.24)
    assert "$34.12" in out          # 2dp, matching the Spot cell
    assert "+1.2%" in out           # 1dp with an explicit sign
    assert GREEN in out


def test_a_down_day_is_red():
    out = _day_head_px(34.125, -0.62)
    assert "-0.6%" in out and RED in out


def test_flat_counts_as_up_like_the_chart_line():
    # _day_chart uses `last >= open` for green; the header must match, or a
    # flat session would show a red number under a green line.
    assert GREEN in _day_head_px(34.0, 0.0)


def test_the_quote_is_colored_with_the_change_not_separately():
    # Color on this readout means exactly one thing — the underlying's direction
    # today — so the price and the percentage carry it together.
    out = _day_head_px(34.125, -0.62)
    assert out.count(RED) == 1 and GREEN not in out
    assert out.index("$34.12") > out.index(RED)   # price inside the colored span


def test_spot_without_a_change_still_renders_but_uncolored():
    # No change → no direction to state, so the price must not default to green.
    out = _day_head_px(34.0, None)
    assert "$34.00" in out
    assert "%" not in out
    assert GREEN not in out and RED not in out


def test_missing_or_nonsense_spot_renders_nothing():
    # Empty string, so the header falls back to the bare "TODAY · TICKER"
    # eyebrow rather than showing a dash where a quote should be.
    for spot in (None, float("nan"), 0.0, -1.0, "", "abc"):
        assert _day_head_px(spot, 1.2) == ""


def test_nan_change_drops_only_the_change():
    out = _day_head_px(34.0, float("nan"))
    assert "$34.00" in out and "%" not in out


def test_thousands_are_separated():
    assert "$1,234.50" in _day_head_px(1234.5, 2.0)


def test_numeric_strings_are_accepted():
    # fetch_spot_meta values arrive from JSON and are occasionally strings.
    out = _day_head_px("34.125", "1.24")
    assert "$34.12" in out and "+1.2%" in out


# ── the markdown renderer (collapsed row header) ─────────────────────────────

def test_md_uses_streamlit_color_directives():
    # A button label renders markdown, not HTML — and the :green[…]/:red[…]
    # directives track the active theme instead of a hardcoded hex. Price and
    # percentage are colored as one unit; the "spot" label stays plain.
    assert _day_head_md(34.125, 1.24) == r"spot :green[\$34.12 +1.2%]"
    assert _day_head_md(34.125, -0.62) == r"spot :red[\$34.12 -0.6%]"


def test_md_escapes_every_dollar_sign():
    # THE regression: the header already contains the strike's "$", so a second
    # unescaped one turns everything between them into LaTeX math — Streamlit
    # eats the dollar signs and reflows the middle of the line.
    for spot, pct in ((34.125, 1.24), (34.0, None), (1234.5, -2.0)):
        out = _day_head_md(spot, pct)
        assert "$" in out
        assert out.count("$") == out.count(r"\$"), out


def test_md_carries_no_html():
    out = _day_head_md(34.125, 1.24)
    assert "<" not in out and ">" not in out


def test_md_labels_the_number_as_spot():
    # The header already shows a strike ("CPNG $30 CALL"), so an unlabeled
    # second dollar figure would be ambiguous.
    assert _day_head_md(34.0, 1.0).startswith("spot ")


def test_md_spot_without_a_change_is_uncolored():
    out = _day_head_md(34.0, None)
    assert out == r"spot \$34.00"
    assert ":green[" not in out and ":red[" not in out


def test_md_missing_spot_renders_nothing():
    # Empty string, so the caller drops the whole segment (and the mode badge
    # stays the last element on the line).
    for spot in (None, float("nan"), 0.0, "abc"):
        assert _day_head_md(spot, 1.2) == ""


def test_both_renderers_agree_on_the_numbers_and_direction():
    for spot, pct in ((34.125, 1.24), (34.125, -0.62), (1234.5, 0.0)):
        html, md = _day_head_px(spot, pct), _day_head_md(spot, pct)
        for token in (f"{float(spot):,.2f}", f"{float(pct):+.1f}%"):
            assert token in html and token in md
        up = pct >= 0
        assert (GREEN in html) is up and (":green[" in md) is up
        assert (RED in html) is (not up) and (":red[" in md) is (not up)
