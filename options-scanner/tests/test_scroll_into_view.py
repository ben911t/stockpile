"""The scroll-the-detail-into-view component (Roll builder, Close builder).

Two browser-side bugs live behind this helper, and both come down to what gets
handed to ``st.iframe``:

1. selecting the LAST row scrolled nowhere — one delayed shot fired before the
   page had grown tall enough to have anywhere to scroll to; and
2. selecting a SECOND row scrolled nowhere — ``st.iframe`` takes no ``key``, so
   an identical payload at the same position is reconciled as unchanged and the
   iframe is never reloaded, meaning the script never runs again.

The rendered payload is what these assert: a retry loop for (1) and a per-call
nonce for (2).
"""

import pytest

from options_scanner import ui_theme


@pytest.fixture
def rendered(monkeypatch):
    """Capture the HTML handed to st.iframe instead of rendering it."""
    calls = []
    monkeypatch.setattr(ui_theme.st, "iframe",
                        lambda src, **kw: calls.append((src, kw)))
    return calls


def _nonce_free(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if "remount-nonce" not in l)


# ── remount on every call (the second-selection bug) ─────────────────────────

def test_each_call_renders_a_distinct_payload(rendered):
    for _ in range(3):
        ui_theme.scroll_into_view()
    payloads = [src for src, _ in rendered]
    assert len(set(payloads)) == 3, "identical payloads would not remount"


def test_only_the_nonce_differs_between_calls(rendered):
    ui_theme.scroll_into_view()
    ui_theme.scroll_into_view()
    a, b = (src for src, _ in rendered)
    assert a != b
    assert _nonce_free(a) == _nonce_free(b)


# ── retry loop (the last-row bug) ────────────────────────────────────────────

def test_payload_retries_rather_than_firing_once(rendered):
    ui_theme.scroll_into_view(tries=8, interval_ms=180)
    src = rendered[0][0]
    assert "n < 8" in src            # bounded retry, not a single shot
    assert "setTimeout(tick, 180)" in src
    assert "scrollIntoView" in src


def test_retry_stops_once_the_panel_is_on_screen(rendered):
    ui_theme.scroll_into_view()
    src = rendered[0][0]
    # Landed = top edge visible and in the upper part of the viewport, so the
    # loop can't keep nudging a panel the user is already looking at.
    assert "innerHeight" in src and "landed" in src


def test_knobs_are_substituted(rendered):
    ui_theme.scroll_into_view(margin_top=60, tries=3, interval_ms=250)
    src = rendered[0][0]
    assert 'scrollMarginTop = "60px"' in src
    assert "n < 3" in src
    assert "setTimeout(tick, 250)" in src


def test_no_placeholder_survives(rendered):
    ui_theme.scroll_into_view()
    src = rendered[0][0]
    for token in ("__NONCE__", "__MARGIN__", "__TRIES__", "__INTERVAL__"):
        assert token not in src


def test_component_stays_invisible(rendered):
    ui_theme.scroll_into_view()
    assert rendered[0][1] == {"height": 1, "width": 1}
