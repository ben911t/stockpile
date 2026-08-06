"""Tab-bar fill marking the tabs that need a live broker.

Trades and Positions read a Schwab account; every other tab works off a chain
fetch or an uploaded CSV. The tab bar is the only place that distinction is
visible before you click.

History worth keeping: the first attempt scoped its CSS to
`[data-testid="stSegmentedControl"]`, a test id Streamlit does not define, and
silently matched nothing. The real ids are `stButtonGroup` for the row and
`stBaseButton-segmented_control` / `…_controlActive` for the chips. Scoping now
runs through `st-key-osc_tabbar`, which is ours and can't be renamed by a
version bump.
"""

import re

import pytest

from options_scanner import ui_theme

TAB_NAMES = ["Single Ticker", "Watchlist", "Positions", "Trades",
             "Portfolio", "GEX", "Spreads", "Directional", "Neutral",
             "Live Charts"]
BROKER_TABS = {"Trades", "Positions"}


@pytest.fixture
def css(monkeypatch):
    """Capture what mark_broker_tabs would inject."""
    out = []

    class _St:
        @staticmethod
        def markdown(s, **k):
            out.append(s)

    monkeypatch.setattr(ui_theme, "st", _St)
    return lambda names=TAB_NAMES, broker=BROKER_TABS: (
        out.clear() or ui_theme.mark_broker_tabs(names, broker) or "".join(out))


def _indices(text):
    return sorted({int(m) for m in re.findall(r"nth-of-type\((\d+)\)", text)})


# ── which chips get filled ──────────────────────────────────────────────────

def _at(name):
    """1-based tab-bar position of a tab, derived not hard-coded — the pair has
    already been swapped once."""
    return TAB_NAMES.index(name) + 1


def test_it_fills_the_broker_tabs(css):
    assert _indices(css()) == sorted(_at(n) for n in BROKER_TABS)


def test_reordering_the_tabs_moves_the_fill(css):
    moved = ["Positions", "Single Ticker", "Trades", "GEX"]
    assert _indices(css(moved)) == [1, 3]


def test_inserting_a_tab_shifts_the_fill_with_it(css):
    inserted = ["Single Ticker", "NEW", "Watchlist", "Trades", "Positions"]
    assert _indices(css(inserted)) == [4, 5]


def test_live_charts_is_not_a_broker_tab(css):
    # Its panes take Yahoo and Hyperliquid too, so Schwab is one option rather
    # than a requirement — filling it would overstate the dependency.
    assert TAB_NAMES.index("Live Charts") + 1 not in _indices(css())


def test_a_name_that_is_not_a_tab_is_ignored(css):
    assert _indices(css(TAB_NAMES, {"Trades", "Nonexistent"})) == [_at("Trades")]


def test_no_broker_tabs_injects_nothing(css):
    assert css(TAB_NAMES, set()) == ""


# ── the color ───────────────────────────────────────────────────────────────

def test_the_fill_is_schwab_blue(css):
    # #00A0DF. Deliberately not --osc-primary: this marks a broker dependency,
    # not the app's own accent.
    assert f"rgba({ui_theme.SCHWAB_BLUE}" in css()
    assert ui_theme.SCHWAB_BLUE == "0, 160, 223"


def test_the_whole_chip_is_filled_not_just_the_label(css):
    # The previous version tinted only the text run via a Markdown directive,
    # which read as a pale box behind the words rather than a colored tab.
    out = css()
    assert "background-color" in out
    assert ":blue-background[" not in out


def test_the_selected_chip_is_distinct_from_the_others(css):
    # Streamlit marks the active option with its own background, which an
    # !important fill overrides — without a distinct active rule you can't tell
    # which broker tab is open.
    out = css()
    assert f"background-color: {ui_theme._BROKER_TAB_ACTIVE_BG}" in out
    assert f"rgba({ui_theme.SCHWAB_BLUE}, {ui_theme._BROKER_TAB_IDLE})" in out


def test_the_selected_fill_is_opaque(css):
    # The bug this replaced: a translucent active fill resolves against
    # whatever canvas is behind it, so it landed pale on the light theme
    # (white label at 2.0:1) and dark on the other. An opaque color renders
    # the same on both.
    assert re.match(r"^#[0-9A-Fa-f]{6}$", ui_theme._BROKER_TAB_ACTIVE_BG)
    # ...and the active rule must not fall back to an rgba of the base hue.
    active_block = css().split("!important;")[1]
    assert "rgba(" not in active_block


def test_the_selected_label_clears_wcag_aa(css):
    # 4.5:1 for normal text. The whole point of pinning the active pair.
    def _lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def _lum(hexstr):
        r, g, b = (int(hexstr[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    a, b = _lum(ui_theme._BROKER_TAB_ACTIVE_FG), _lum(ui_theme._BROKER_TAB_ACTIVE_BG)
    hi, lo = max(a, b), min(a, b)
    assert (hi + 0.05) / (lo + 0.05) >= 4.5


def test_the_label_color_reaches_the_text_node(css):
    # Streamlit wraps the label in a child element, so a color set on the
    # button alone never lands — the same trap the roll dialog's red Cancel
    # button hits, which is why it styles `button p` explicitly.
    out = css()
    assert " p," in out or " p " in out
    assert " span" in out


def test_the_idle_fill_stays_translucent(css):
    # Unselected chips keep the theme's own label color, which adapts with the
    # canvas — so their fill has to let the canvas through.
    assert 0 < ui_theme._BROKER_TAB_IDLE < 1
    assert f"rgba({ui_theme.SCHWAB_BLUE}, {ui_theme._BROKER_TAB_IDLE})" in css()


# ── selector robustness ─────────────────────────────────────────────────────

def test_the_dead_test_id_is_never_used_again(css):
    # The bug that made two rounds of this invisible.
    assert "stSegmentedControl" not in css()


def test_it_is_scoped_to_our_own_container_key(css):
    # st-key-osc_tabbar comes from st.container(key=...), so it survives a
    # Streamlit version bump in a way their internal test ids did not.
    out = css()
    assert "st-key-osc_tabbar" in out
    # Every selector is scoped — no bare `button:nth-of-type` that would also
    # hit the data-source toggle, which is another segmented_control.
    for sel in out.split("{")[0].split(","):
        if "nth-of-type" in sel:
            assert "st-key-osc_tabbar" in sel


def test_all_three_nesting_forms_are_covered(css):
    # The exact nesting isn't contractual, so address the chip three ways: a
    # direct button child, a button wrapped one level down, and one under the
    # stButtonGroup element. They all resolve to the same element.
    n = _at("Trades")
    out = css(TAB_NAMES, {"Trades"})
    assert f"button:nth-of-type({n})" in out
    assert f"*:nth-of-type({n}) > button" in out
    assert f'[data-testid="stButtonGroup"] > *:nth-of-type({n}) button' in out


def test_the_active_rule_matches_either_state_hook(css):
    # Streamlit exposes the selected chip both as a distinct kind and via
    # aria-checked; matching either means losing one doesn't lose the rule.
    out = css()
    assert '[data-testid$="Active"]' in out
    assert '[aria-checked="true"]' in out
