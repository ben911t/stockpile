"""The DTE cell shared by the Close and Roll tabs: "18 (44)".

Days to expiration, then how long the position has been open. The broker
reports the first and not the second — days-open comes from the app's trade
log, so it's present only for legs opened through the scanner.
"""

from datetime import date, datetime, timedelta

import pytest

from options_scanner.format import days_since, dte_cell


def test_both_figures_render_together():
    assert dte_cell(18, 44) == "18 (44)"


def test_a_leg_opened_outside_the_app_shows_dte_alone():
    # No parens rather than empty ones — most positions predate the trade log.
    assert dte_cell(18) == "18"
    assert dte_cell(18, None) == "18"


def test_day_zero_is_shown_not_swallowed():
    # Opened today is 0, which is a real answer; only None means "unknown".
    assert dte_cell(18, 0) == "18 (0)"


def test_expiring_today_is_zero_dte():
    assert dte_cell(0, 3) == "0 (3)"


def test_an_unparseable_expiration_still_renders_a_cell():
    assert dte_cell(None) == "—"
    assert dte_cell(None, 44) == "— (44)"


def test_days_since_counts_whole_days():
    opened = datetime.now() - timedelta(days=44, hours=3)
    assert days_since(opened.isoformat(timespec="seconds")) == 44


def test_days_since_is_zero_on_the_day_it_opened():
    assert days_since(datetime.now().isoformat(timespec="seconds")) == 0


def test_days_since_accepts_a_bare_date():
    assert days_since(date.today().isoformat()) == 0


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 42, "2026-13-45"])
def test_days_since_gives_up_quietly_on_junk(bad):
    # A malformed record must cost the row its parenthetical, not the table.
    assert days_since(bad) is None
