"""Cached, read-only reads of the live broker option positions.

One home for them so the **Close** tab, the **Roll** tab, and the ⚙️ **Settings**
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
def rollable(app_key: str, app_secret: str, callback_url: str,
             token_file: str) -> list | None:
    """Rollable positions only — short covered calls + short puts. The Roll
    tab's source."""
    client = _client(app_key, app_secret, callback_url, token_file)
    if client is None:
        return None
    return trade_actions.rollable_positions(client)
