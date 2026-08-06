"""Yahoo rate-limit detection and propagation.

A throttled fetch must surface as RateLimitError (so the portfolio scan
can wait and retry) rather than being swallowed as a generic per-ticker
failure or a skipped expiration.
"""

import pytest

from options_scanner import chain
from stocks_shared.yahoo import RateLimitError, is_rate_limit_error


class _FakeYFRateLimit(Exception):
    pass


_FakeYFRateLimit.__name__ = "YFRateLimitError"


@pytest.mark.parametrize("exc, expected", [
    (_FakeYFRateLimit("anything"), True),          # matched by type name
    (Exception("429 Client Error: Too Many Requests"), True),
    (Exception("Yahoo rate limit exceeded"), True),
    (Exception("No data found for ticker"), False),
    (ValueError("Could not fetch live price for X"), False),
])
def test_is_rate_limit_error(exc, expected):
    assert is_rate_limit_error(exc) is expected


def test_spot_rate_limit_propagates(monkeypatch):
    """A throttled spot lookup raises RateLimitError out of the chain
    fetch instead of the generic could-not-fetch ValueError."""
    def _throttled(t, **kw):
        raise RateLimitError(f"Yahoo rate limit hit fetching {t}")
    monkeypatch.setattr(chain, "fetch_live_price", _throttled)
    monkeypatch.setattr(chain, "normalize_ticker", lambda t: t)
    with pytest.raises(RateLimitError):
        chain._fetch_chain_yahoo("AAPL", min_dte=0, max_dte=60)


def test_degraded_chain_raises(monkeypatch):
    """Yahoo's soft throttle serves HTTP-200 chains with every bid/ask
    zeroed and IV at the 0.00001 placeholder. That must surface as
    RateLimitError, not as an empty (or nearly empty) result."""
    import datetime
    import pandas as pd
    import yfinance as yf
    from types import SimpleNamespace

    exp = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    degraded = pd.DataFrame({
        "strike": [90.0 + i for i in range(12)],
        "bid": [0.0] * 12,
        "ask": [0.0] * 12,
        "lastPrice": [1.0] * 12,
        "impliedVolatility": [0.00001] * 12,
        "openInterest": [10] * 12,
        "volume": [0] * 12,
    })

    class _Ticker:
        def __init__(self, t):
            self.options = (exp,)

        def option_chain(self, e):
            return SimpleNamespace(calls=degraded, puts=degraded.copy())

    monkeypatch.setattr(chain, "fetch_live_price", lambda t, **kw: 100.0)
    monkeypatch.setattr(chain, "normalize_ticker", lambda t: t)
    monkeypatch.setattr(yf, "Ticker", _Ticker)
    with pytest.raises(RateLimitError, match="degraded"):
        chain._fetch_chain_yahoo("AAPL", min_dte=0, max_dte=60)


def test_expiration_rate_limit_propagates(monkeypatch):
    """A throttle on a per-expiration chain call re-raises instead of
    being 'skipped' like an ordinary bad expiration."""
    import datetime
    import yfinance as yf

    exp = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    class _Ticker:
        def __init__(self, t):
            self.options = (exp,)

        def option_chain(self, e):
            raise _FakeYFRateLimit("Too Many Requests. Rate limited.")

    monkeypatch.setattr(chain, "fetch_live_price", lambda t, **kw: 100.0)
    monkeypatch.setattr(chain, "normalize_ticker", lambda t: t)
    monkeypatch.setattr(yf, "Ticker", _Ticker)
    with pytest.raises(RateLimitError):
        chain._fetch_chain_yahoo("AAPL", min_dte=0, max_dte=60)


class TestPortfolioRetryLoop:
    """The CLI portfolio scan loop (_scan_positions) must wait+retry a
    throttled ticker up to _RL_MAX_RETRIES times, then — once one ticker burns
    its whole budget — fail every remaining ticker fast instead of crashing or
    waiting per-ticker."""

    @staticmethod
    def _ok(pos):
        return {"position": pos, "error": None, "df": None, "spot": 1.0,
                "earnings_dates": [], "roll_close_costs": {}}

    def _run(self, monkeypatch, scan_impl):
        from options_scanner import portfolio
        monkeypatch.setattr(portfolio.time, "sleep", lambda s: None)  # no real wait
        monkeypatch.setattr(portfolio, "scan_position", scan_impl)
        positions = [{"ticker": t} for t in ("AAA", "BBB", "CCC")]
        return portfolio, list(
            portfolio._scan_positions(positions, 30, 25, 0.7, "yahoo", None))

    def test_retries_then_succeeds(self, monkeypatch):
        """Throttled twice, then clears — the ticker still scans (no error)."""
        calls = {}

        def scan(pos, *a, **k):
            calls[pos["ticker"]] = calls.get(pos["ticker"], 0) + 1
            if pos["ticker"] == "AAA" and calls["AAA"] <= 2:
                raise RateLimitError("429 Too Many Requests")
            return self._ok(pos)

        _, out = self._run(monkeypatch, scan)
        assert calls["AAA"] == 3          # 2 throttles + 1 success
        assert all(r["error"] is None for _, r in out)
        assert [p["ticker"] for p, _ in out] == ["AAA", "BBB", "CCC"]

    def test_gives_up_after_budget_and_fast_fails_rest(self, monkeypatch):
        """Persistent throttle: AAA burns 1 initial + _RL_MAX_RETRIES attempts,
        then BBB/CCC fail fast with a single attempt each (no more waiting)."""
        n = {"calls": 0}

        def scan(pos, *a, **k):
            n["calls"] += 1
            raise RateLimitError("429 Too Many Requests")

        portfolio, out = self._run(monkeypatch, scan)
        assert n["calls"] == (1 + portfolio._RL_MAX_RETRIES) + 1 + 1
        assert all(r["error"] for _, r in out)          # every ticker failed
        assert "still throttling" in out[0][1]["error"]
        assert len(out) == 3
