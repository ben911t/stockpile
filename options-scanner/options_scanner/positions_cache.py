"""Cached, read-only reads of the live broker option positions.

One home for them so the **Positions** tab and the ⚙️ **Settings**
dialog share a single 60s cache instead of each paying its own Schwab
round-trip — and so the dialog can ask what you hold without importing a tab
module.

Read-only: nothing here places or modifies an order. Each reader returns None
when a client can't be built (the caller then shows the re-auth hint) and a list
(possibly empty) otherwise.

These readers deliberately return **every** leg. The hidden-position blacklist
(``position_filters``) is applied by the caller at render time, never here, for
two reasons: the cache should hold the account's truth, and a settings change
then takes effect on the next rerun instead of waiting out the TTL.
"""

from __future__ import annotations

import streamlit as st

from options_scanner import trade_actions


def _client(app_key: str, app_secret: str, callback_url: str,
            token_file: str):
    """A Schwab client, or None when one can't be built (expired token, missing
    credentials, …)."""
    from stocks_shared.schwab_live import get_client
    try:
        return get_client(app_key, app_secret, callback_url, token_file)
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def option_positions(app_key: str, app_secret: str, callback_url: str,
                     token_file: str) -> list | None:
    """Every live option leg in the Schwab account — short AND long. The Close
    tab's source, and what the Settings dialog offers as hideable."""
    client = _client(app_key, app_secret, callback_url, token_file)
    if client is None:
        return None
    return trade_actions.open_option_positions(client)


@st.cache_data(ttl=60, show_spinner=False)
def stock_positions(app_key: str, app_secret: str, callback_url: str,
                    token_file: str) -> list | None:
    """Every stock position in the Schwab account, each with its covered-call
    standing (see `trade_actions.classify_coverage`). The Positions tab's
    stocks table. None when Schwab can't be reached — distinct from [], which
    means "reached it, you hold no stock"."""
    client = _client(app_key, app_secret, callback_url, token_file)
    if client is None:
        return None
    return trade_actions.equity_positions(client)


@st.cache_data(ttl=60, show_spinner=False)
def account_capacity(app_key: str, app_secret: str, callback_url: str,
                     token_file: str) -> dict | None:
    """Cached (60s) read-only account balances — cash, available funds and
    buying power. None when the client can't be built or the read fails.

    Shared by the Sell dialog (sizing a new position) and the Positions tab (can I
    afford to buy this back?), so one fetch serves both. Keyed on the
    credentials, so a re-auth busts it the same way it busts every other read.
    """
    from options_scanner import trade_actions
    client = _client(app_key, app_secret, callback_url, token_file)
    if client is None:
        return None
    cap = trade_actions.fetch_account_capacity(client)
    if cap is None:
        return None
    return {"cash": cap.cash_available, "bp": cap.buying_power,
            "amount": cap.amount, "type": cap.account_type,
            "mask": cap.account_mask, "balances": cap.balances,
            # What `amount` actually is, so callers can label it honestly — on a
            # margin account it isn't the cash balance.
            "amount_label": cap.amount_label, "amount_note": cap.amount_note}


@st.cache_data(ttl=60, show_spinner=False)
def rollable(app_key: str, app_secret: str, callback_url: str,
             token_file: str) -> list | None:
    """Rollable positions only — short covered calls + short puts. The Roll
    tab's source."""
    client = _client(app_key, app_secret, callback_url, token_file)
    if client is None:
        return None
    return trade_actions.rollable_positions(client)
