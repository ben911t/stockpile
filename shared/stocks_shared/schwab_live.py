"""Schwab developer API helpers — mirrors stocks_shared/yahoo.py."""

import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

log = logging.getLogger(__name__)

# Schwab refresh tokens expire a fixed 7 days after the initial OAuth login.
SCHWAB_REFRESH_TOKEN_TTL_DAYS = 7.0

# Bound every Schwab HTTP request so a slow or unreachable endpoint can't hang
# a caller (e.g. the Trades tab) indefinitely. Applied best-effort to the
# client's httpx session in get_client.
SCHWAB_HTTP_TIMEOUT_S = 10.0


def token_age_days(token_file: str) -> float | None:
    """Days since the token's initial OAuth login, or None if unknown.

    schwab-py token files wrap the OAuth token with a `creation_timestamp`
    set at login. Age ≥ SCHWAB_REFRESH_TOKEN_TTL_DAYS means the refresh
    token has definitively expired and schwab_auth.py must be re-run.
    Returns None when the file is missing, unreadable, or has no timestamp.
    """
    try:
        with open(Path(token_file).expanduser()) as f:
            ts = json.load(f).get("creation_timestamp")
        return None if ts is None else (time.time() - float(ts)) / 86400.0
    except (OSError, ValueError, TypeError):
        return None


def token_remaining_seconds(token_file: str) -> float | None:
    """Seconds until the Schwab refresh token's 7-day TTL elapses.

    Negative once expired; None when the token file is missing/unreadable.
    Lets the UI show a countdown to the next required re-auth.
    """
    age = token_age_days(token_file)
    if age is None:
        return None
    return (SCHWAB_REFRESH_TOKEN_TTL_DAYS - age) * 86400.0

# Schwab uses $NAME for cash-settled index options.
_SCHWAB_INDEX_TICKERS = frozenset({
    "SPX", "SPXW", "NDX", "NDXP", "RUT",
    "VIX", "DJI", "OEX", "XEO", "VXN", "RVX",
})


# Class shares (BRK.B, BF.A, …) — NYSE tape uses a dot, Yahoo a dash,
# Schwab a slash. Accept any of the three and rewrite per provider.
# (Mirrors _CLASS_SHARE_RE in stocks_shared/yahoo.py.)
_CLASS_SHARE_RE = re.compile(r"^([A-Z]{1,5})[./-]([A-Z])$")


def normalize_ticker_schwab(ticker: str) -> str:
    """Prepend $ for index tickers that Schwab lists under $NAME.

    Class-share notation (BRK.B / BRK-B / BRK/B) is rewritten to Schwab's
    slash form (BRK/B). Trailing ! disables normalization — the bare
    symbol is used as-is.
    """
    t = ticker.strip().upper()
    if t.endswith("!"):
        return t[:-1]
    t = t.lstrip("^$")
    if t in _SCHWAB_INDEX_TICKERS:
        return f"${t}"
    m = _CLASS_SHARE_RE.match(t)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return t

# Cache maps (app_key, token_path) -> (client, token_file_mtime). The mtime
# is the cache-invalidation key: see get_client.
_client_cache: dict = {}


def _token_mtime(token_path: Path) -> float | None:
    """Modification time of the token file, or None if it doesn't exist."""
    try:
        return token_path.stat().st_mtime
    except OSError:
        return None


def token_mtime(token_file: str) -> float | None:
    """Public mtime (epoch seconds) of the token file, or None if missing.

    Lets a long-running UI detect re-auth — a freshly minted token
    rewrites the file — so it can drop cached fetches without a restart.
    """
    if not token_file:
        return None
    return _token_mtime(Path(token_file).expanduser())


def get_client(app_key: str, app_secret: str, callback_url: str,
               token_file: str):
    """Return authenticated schwab-py client; cached per (app_key, token_file).

    First run (no token file): opens browser for OAuth login.
    Subsequent runs: silently refresh the access token via the stored
    refresh token. The refresh token itself has a fixed 7-day TTL from
    the initial OAuth — once expired, every quote/chain call returns
    None and the user must re-run schwab_auth.py.

    The cached client is rebuilt whenever the token file's mtime changes,
    so re-running schwab_auth.py is picked up by a long-running process
    (the Streamlit server, the trading dashboard) without a restart.
    schwab-py's own periodic access-token refresh also rewrites the file,
    which triggers a harmless rebuild from the freshly-refreshed token.

    Raises ValueError with a user-friendly message on auth failure.
    """
    import schwab

    token_path = Path(token_file).expanduser()
    cache_key = (app_key, str(token_path))
    mtime = _token_mtime(token_path)

    cached = _client_cache.get(cache_key)
    if cached is not None and cached[1] == mtime:
        return cached[0]

    try:
        client = schwab.auth.client_from_token_file(
            str(token_path), app_key, app_secret
        )
    except FileNotFoundError:
        try:
            log.info("No Schwab token found — starting OAuth login flow...")
            client = schwab.auth.client_from_login_flow(
                app_key, app_secret, callback_url, str(token_path)
            )
        except Exception as exc:
            raise ValueError(
                f"Schwab authentication failed: {exc}\n"
                "Run: uv run options-scanner/schwab_auth.py"
            ) from exc
    except Exception as exc:
        raise ValueError(
            f"Schwab token error: {exc}\n"
            "Run: uv run options-scanner/schwab_auth.py"
        ) from exc

    # Best-effort: bound every HTTP request this client makes (schwab-py wraps
    # an httpx session) so a slow/unreachable Schwab endpoint can't hang the
    # caller. If the client internals ever change, fall back to the library
    # default rather than break.
    try:
        import httpx
        client.session.timeout = httpx.Timeout(SCHWAB_HTTP_TIMEOUT_S)
    except Exception:
        pass

    # Re-stat after building: client_from_token_file may refresh-and-rewrite
    # the file on load, and the login flow just created it — cache the
    # post-build mtime so the next call doesn't rebuild needlessly.
    _client_cache[cache_key] = (client, _token_mtime(token_path))
    return client


def fetch_live_price_schwab(client, ticker: str) -> float | None:
    """Return live mark price for ticker, or None on error.

    Uses the plural get_quotes endpoint: get_quote puts the symbol in the
    URL path, which 404s on class shares like BRK/B; get_quotes passes it
    as a query parameter and handles the slash fine.
    """
    try:
        ticker = normalize_ticker_schwab(ticker)
        resp = client.get_quotes([ticker])
        resp.raise_for_status()
        data = resp.json()
        quote = data.get(ticker, {}).get("quote", {})
        price = quote.get("mark") or quote.get("lastPrice")
        return float(price) if price is not None else None
    except Exception as exc:
        log.warning("Could not fetch Schwab price for %s: %s", ticker, exc)
        return None


def fetch_option_chain_raw(client, ticker: str, min_dte: int,
                           max_dte: int | None) -> dict | None:
    """Fetch full option chain from Schwab API.

    Returns raw API response dict, or None on error.
    """
    import datetime
    from schwab.client import Client

    ticker = normalize_ticker_schwab(ticker)
    today = datetime.date.today()
    from_date = today + datetime.timedelta(days=min_dte)
    to_date = (today + datetime.timedelta(days=max_dte)
               if max_dte is not None else None)

    try:
        kwargs: dict = {
            "contract_type": Client.Options.ContractType.ALL,
            "from_date": from_date,
            "strategy": Client.Options.Strategy.SINGLE,
            "include_underlying_quote": True,
        }
        if to_date is not None:
            kwargs["to_date"] = to_date

        resp = client.get_option_chain(ticker, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("Could not fetch Schwab option chain for %s: %s", ticker, exc)
        return None


def fetch_option_chain_schwab(client, ticker: str, exp_str: str):
    """Fetch a single expiration for roll close-cost lookup.

    Returns an object with .calls and .puts DataFrames (columns: strike, bid,
    ask, lastPrice, volume, openInterest, volatility, delta, last_trade_ms) —
    same interface as stocks_shared.yahoo.fetch_option_chain(). last_trade_ms is
    the last print's epoch-ms (Schwab tradeTimeInLong), 0 when absent.
    """
    import datetime
    from schwab.client import Client

    _empty = SimpleNamespace(
        calls=pd.DataFrame(columns=["strike", "bid", "ask", "lastPrice", "volume", "openInterest",
                  "volatility", "delta", "last_trade_ms"]),
        puts=pd.DataFrame(columns=["strike", "bid", "ask", "lastPrice", "volume", "openInterest",
                  "volatility", "delta", "last_trade_ms"]),
    )

    try:
        exp_date = datetime.date.fromisoformat(exp_str)
    except ValueError:
        return _empty

    try:
        resp = client.get_option_chain(
            ticker,
            contract_type=Client.Options.ContractType.ALL,
            from_date=exp_date,
            to_date=exp_date,
            strategy=Client.Options.Strategy.SINGLE,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("Could not fetch Schwab chain for %s %s: %s",
                    ticker, exp_str, exc)
        return None

    def _parse_side(exp_date_map: dict) -> pd.DataFrame:
        rows = []
        for key, strikes in exp_date_map.items():
            if not key.startswith(exp_str):
                continue
            for opts in strikes.values():
                for opt in opts:
                    rows.append({
                        "strike":    float(opt.get("strikePrice", 0)),
                        "bid":       float(opt.get("bid", 0) or 0),
                        "ask":       float(opt.get("ask", 0) or 0),
                        "lastPrice": float(opt.get("last", 0) or 0),
                        "volume":    int(opt.get("totalVolume", 0) or 0),
                        "openInterest": int(opt.get("openInterest", 0) or 0),
                        "volatility": float(opt.get("volatility", 0) or 0),
                        "delta":     float(opt.get("delta", 0) or 0),
                        # Epoch-ms of the last print; 0 when Schwab omits it.
                        # Kept for the pre-confirm "Last @ time" annotation.
                        "last_trade_ms": int(opt.get("tradeTimeInLong", 0) or 0),
                    })
        return pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["strike", "bid", "ask", "lastPrice", "volume", "openInterest",
                  "volatility", "delta", "last_trade_ms"])

    return SimpleNamespace(
        calls=_parse_side(data.get("callExpDateMap", {})),
        puts=_parse_side(data.get("putExpDateMap", {})),
    )


# Dashboard interval -> Schwab native get_price_history_* method.
_SCHWAB_NATIVE = {
    "1m":  "get_price_history_every_minute",
    "5m":  "get_price_history_every_five_minutes",
    "15m": "get_price_history_every_fifteen_minutes",
    "30m": "get_price_history_every_thirty_minutes",
    "1d":  "get_price_history_every_day",
    "1w":  "get_price_history_every_week",
}

# Intervals Schwab has no native bar for: (finer native interval, pandas rule).
_SCHWAB_RESAMPLE = {
    "3m": ("1m",  "3min"),
    "1h": ("30m", "60min"),
    "4h": ("30m", "240min"),
    "1M": ("1d",  "MS"),
}


# Epoch seconds for any realistic date top out around 4.1e9 (year ~2100),
# while epoch milliseconds are >= ~1e11 for any date from 1973 onward. So
# 1e11 separates the two units; a higher cut (e.g. 1e12) wrongly reads the
# ms timestamps of pre-2001 dates — common in long daily histories — as
# seconds, throwing them tens of thousands of years into the future.
_MS_EPOCH_THRESHOLD = 10**11


def _to_unix_seconds(value) -> int:
    """Normalize Schwab candle timestamps to UTC epoch seconds."""
    if isinstance(value, bool):
        raise ValueError("Invalid candle datetime value")

    if isinstance(value, (int, float)):
        unit = "ms" if abs(float(value)) >= _MS_EPOCH_THRESHOLD else "s"
        ts = pd.to_datetime(value, unit=unit, utc=True, errors="coerce")
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value)
        unit = "ms" if abs(number) >= _MS_EPOCH_THRESHOLD else "s"
        ts = pd.to_datetime(number, unit=unit, utc=True, errors="coerce")
    else:
        ts = pd.to_datetime(value, utc=True, errors="coerce")

    if pd.isna(ts):
        raise ValueError(f"Invalid candle datetime value: {value!r}")

    return int(ts.timestamp())


def _parse_schwab_candles(data: dict) -> list[dict]:
    """Convert a Schwab price-history payload into dashboard candle dicts."""
    candles = (data or {}).get("candles") or []
    rows = [
        {
            "time":   _to_unix_seconds(c["datetime"]),
            "open":   round(float(c["open"]), 4),
            "high":   round(float(c["high"]), 4),
            "low":    round(float(c["low"]), 4),
            "close":  round(float(c["close"]), 4),
            "volume": int(c.get("volume", 0) or 0),
        }
        for c in candles
    ]
    rows.sort(key=lambda r: r["time"])
    return rows


def _fetch_native_candles(client, ticker: str, interval: str) -> list[dict]:
    """Fetch one of Schwab's natively-supported bar sizes."""
    resp = getattr(client, _SCHWAB_NATIVE[interval])(ticker)
    resp.raise_for_status()
    return _parse_schwab_candles(resp.json())


def _resample_candles(rows: list[dict], rule: str) -> list[dict]:
    """Aggregate finer candles up to `rule` (e.g. '60min', 'MS')."""
    if not rows:
        return rows
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    agg = (
        df.resample(rule, label="left", closed="left")
        .agg(open=("open", "first"), high=("high", "max"),
             low=("low", "min"), close=("close", "last"),
             volume=("volume", "sum"))
        .dropna(subset=["open", "high", "low", "close"])
    )
    return [
        {
            "time":   int(ts.timestamp()),
            "open":   round(float(r["open"]), 4),
            "high":   round(float(r["high"]), 4),
            "low":    round(float(r["low"]), 4),
            "close":  round(float(r["close"]), 4),
            "volume": int(r["volume"]),
        }
        for ts, r in agg.iterrows()
    ]


def fetch_price_history_schwab(client, ticker: str, interval: str,
                               limit: int = 300) -> list[dict]:
    """Return OHLCV candles for ticker at a trading-dashboard interval.

    Output matches the dashboard's other data sources: a list of
    {"time": <unix seconds>, "open", "high", "low", "close", "volume"}
    dicts, oldest first, at most `limit` bars.

    Schwab natively supports 1m/5m/15m/30m/1d/1w; 3m, 1h, 4h and 1M are
    resampled from the nearest finer native bar.
    """
    ticker = normalize_ticker_schwab(ticker)

    if interval in _SCHWAB_NATIVE:
        rows = _fetch_native_candles(client, ticker, interval)
    elif interval in _SCHWAB_RESAMPLE:
        native_iv, rule = _SCHWAB_RESAMPLE[interval]
        rows = _resample_candles(_fetch_native_candles(client, ticker, native_iv), rule)
    else:
        raise ValueError(f"Unsupported Schwab interval: {interval!r}")

    if not rows:
        raise ValueError(f"Schwab: no price history for '{ticker}'")
    return rows[-limit:]
