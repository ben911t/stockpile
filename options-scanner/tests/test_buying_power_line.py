"""The account balances readout — "Cash $X · with margin $Y".

Shown on the Positions table ("Available to close") and on the roll confirm
dialog ("Account"), because both raise the same question: can I afford this?
Buying back a short is a debit, and so is a net-debit roll. Both figures come
straight from Schwab and are labeled for what they are — the app must not
invent a derived "power" number, and must not print the same figure twice under
two different labels.

Every amount is markdown-escaped: two bare "$" in one string are LaTeX
delimiters, and Streamlit eats both dollar signs, which is what made these read
as plain numbers instead of currency.
"""

import inspect

from options_scanner.tabs import rolls, trades
from options_scanner.tabs.trades import _buying_power_line as bpl


def _cap(cash=None, bp=None, **balances):
    return {"cash": cash, "bp": bp, "balances": balances}


# ── the two figures ──────────────────────────────────────────────────────────

def test_margin_account_shows_both_figures():
    line = bpl(_cap(cash=12_500.0, bp=48_000.0))
    assert r"Cash **\$12,500**" in line
    assert r"with margin **\$48,000**" in line


def test_cash_falls_back_to_the_cash_balance():
    # cashAvailableForTrading is a cash-account field; a margin account reports
    # cashBalance instead, and that's still the "without borrowing" figure.
    assert r"Cash **\$9,000**" in bpl(_cap(cashBalance=9_000.0))


def test_cash_available_wins_over_the_raw_balance():
    line = bpl(_cap(cash=7_000.0, cashBalance=9_000.0))
    assert "7,000" in line and "9,000" not in line


def test_margin_alone_still_renders():
    assert bpl(_cap(bp=48_000.0)) == r"with margin **\$48,000**"


# ── not implying capacity that isn't there ───────────────────────────────────

def test_no_margin_line_when_it_matches_cash():
    # A cash account reports buying power equal to cash; printing it twice under
    # two labels would imply borrowing capacity that doesn't exist.
    assert bpl(_cap(cash=9_000.0, bp=9_000.0)) == r"Cash **\$9,000**"


def test_near_equal_margin_is_still_suppressed():
    # Sub-dollar differences are rounding, not borrowing capacity.
    assert bpl(_cap(cash=9_000.0, bp=9_000.40)) == r"Cash **\$9,000**"


def test_nothing_to_show_returns_none():
    # None (not an empty or zeroed line) so the caller drops the caption
    # entirely rather than implying an account with no money in it.
    assert bpl(_cap()) is None
    assert bpl(None) is None
    assert bpl({}) is None


# ── currency formatting ──────────────────────────────────────────────────────

def test_figures_are_whole_dollars_with_separators():
    assert r"Cash **\$1,234,568**" in bpl(_cap(cash=1_234_567.89))


def test_every_dollar_sign_is_escaped():
    # THE regression: with two bare "$" in one markdown string, Streamlit reads
    # them as math delimiters and swallows both — the amounts then render as
    # bare numbers in serif type rather than as currency.
    line = bpl(_cap(cash=12_500.0, bp=48_000.0))
    assert line.count("$") == line.count("\\$") == 2


# ── one caption, every screen that spends money ──────────────────────────────
# Rolling and closing both raise "can I afford this?", so all three order
# screens show the same two figures — through one helper, so the tooltip (what
# "with margin" means, and that long options aren't marginable) can't drift.

def test_every_order_screen_renders_it_through_the_shared_helper():
    for func in (rolls._render_confirm,          # roll confirm dialog
                 trades._render_option_close,    # Positions tab close builder
                 trades._scanner_trades):        # Trades tab close panel
        assert "render_buying_power_caption(" in inspect.getsource(func), func


def test_the_roll_dialog_shows_it_next_to_the_net_figure():
    # Under the net credit/debit line it qualifies — not at the bottom of the
    # dialog, where it would sit below the Confirm button.
    src = inspect.getsource(rolls._render_confirm)
    assert src.index("net {_kind} ") < src.index("render_buying_power_caption")
    assert src.index("render_buying_power_caption") < src.index("Confirm Roll")


def test_the_close_builders_show_it_before_the_confirm_button():
    for func, confirm in ((trades._render_option_close, "Confirm Close"),
                          (trades._scanner_trades, "Confirm Closing Trade")):
        src = inspect.getsource(func)
        assert src.index("render_buying_power_caption") < src.index(confirm)


def test_a_paper_close_does_not_show_account_balances():
    # A paper close books a simulated result and sends nothing, so the real
    # account's money has no bearing on it.
    src = inspect.getsource(trades._scanner_trades)
    guard = src.index("if close_live:")
    assert guard < src.index("render_buying_power_caption") < guard + 400


def test_the_tooltip_warns_that_long_options_are_not_marginable():
    assert "not marginable" in trades._BUYING_POWER_TIP
