# Trading Dashboard

Live Trading Dashboard — lightweight Flask app serving market data endpoints and a minimal UI.

Key features
- Flask API providing OHLCV and latest-price endpoints
- Pluggable data-source registry (yfinance, schwab, hyperliquid) in data_source.py
- Schwab stock quotes (real-time mark + bars) reuse the repo's shared
  `stocks_shared.schwab_live` helpers; crypto stays on Hyperliquid
- In-memory caching with per-interval TTLs and md5-based keys
- Shared requests.Session with retries and lock-based rate-limiting

Table of contents
- Quickstart
- Startup layout (config.toml)
- API Endpoints
- Data sources
- Caching & rate limiting
- Contributing
- License

Quickstart
This app is part of the stockpile uv workspace. Prerequisites: `uv` and
Python 3.12+.

From the repo root:
1. uv sync
2. uv run trading-dashboard/app.py

Or use the helper scripts from this directory (they call `uv run`):
- POSIX (macOS / Linux): ./run.sh
- Windows: run.cmd

The app listens on 0.0.0.0:5000 by default. Set `OSC_DASHBOARD_PORT` to
move it if 5000 is taken — the Flask app, the repo-root `run.py`
launcher, and the scanner's embed all read the same variable, so they
stay in sync.

To run this dashboard **and** the options scanner together, use
`uv run run.py` from the repo root instead; the dashboard then also
appears as the scanner's **Live Charts** tab at :8501, and stays
directly reachable at :5000. For remote or reverse-proxy setups, set
`OSC_DASHBOARD_URL` to the full URL the browser should embed — it takes
precedence over `OSC_DASHBOARD_PORT`.

Startup layout (config.toml)
The startup layout — how many panes open and what each one shows — is
read from `trading-dashboard/config.toml` on every page load (edit and
refresh; no server restart needed). The file is gitignored: copy
`config.toml.example` to `config.toml` and edit. If it's missing, the
app falls back to a single yfinance pane.

Keys:
- `chart_count` — panes visible on first load (1, 2, 4, 6, or 8).
- `default_source` — source for any pane not listed below, and the
  fallback when a `[[pane]]` omits `source`: yfinance, schwab, or
  hyperliquid.
- `[[pane]]` — an ordered list (up to 8) of startup panes, each with
  `source`, `symbol`, and `timeframe` (one of 1m, 3m, 5m, 15m, 30m,
  1h, 4h, 1d, 1w, 1M).

Example:

```toml
chart_count = 2
default_source = "yfinance"

[[pane]]
source = "yfinance"
symbol = "AVGO"
timeframe = "1d"

[[pane]]
source = "hyperliquid"
symbol = "ETH"
timeframe = "15m"
```

A `schwab` pane needs Schwab credentials configured first (see the
Schwab data source section below); without that it just shows a setup
error, and the other panes are unaffected.

This is the dashboard's own layout file. It is separate from
`options-scanner/config.toml`, which holds the shared Schwab API
credentials.

API Endpoints
- GET /api/ohlcv?source={source}&symbol={symbol}&interval={interval}&limit={n}
  - Returns OHLCV arrays for the requested source/symbol/interval. Response JSON: {ok: true, data: [...]}
- GET /api/price?source={source}&symbol={symbol}
  - Returns latest price JSON
- GET /api/sources
  - Lists available data sources, plus the startup layout
    (default_source, chart_count, panes) read from config.toml
- GET /api/health
  - Health check (200 OK)
- GET /
  - Serves templates/index.html (basic UI)

Data sources (data_source.py)
- Implement fetch_{source}(symbol, interval, limit) and register it in _DATASOURCE_REGISTRY.
- Use the shared _SESSION (requests.Session with Retry) for external HTTP calls.
- Call _rate_limit() before outbound requests to respect provider limits.
- Interval mappings are provided (_YF_INTERVAL/_YF_PERIOD/_HL_INTERVAL/_HL_MINS). Follow those conventions.

Schwab data source
- Stock bars come from `stocks_shared.schwab_live.fetch_price_history_schwab`
  and the live ticker number from the real-time `mark`.
- Credentials are shared with the options-scanner: the `[schwab]` section of
  `options-scanner/config.toml` plus the token at `~/.config/schwab-token.json`.
  If you've already set Schwab up in the scanner, it just works here.
- **First-time Schwab setup** (register a free developer app, get the App Key /
  App Secret, copy `config.toml.example` → `config.toml`, authenticate): follow
  [`options-scanner/SCHWAB_DATA_SOURCE.md`](../options-scanner/SCHWAB_DATA_SOURCE.md).
- The Schwab refresh token has a 7-day TTL. If Schwab quotes come back empty,
  re-auth with: `uv run options-scanner/schwab_auth.py`.
- Schwab natively serves 1m/5m/15m/30m/1d/1w bars; 3m, 1h, 4h and 1M are
  resampled from the nearest finer native bar. Schwab has no crypto, so coins
  stay on Hyperliquid.

Caching & rate limiting
- Cache keys are md5(source:symbol:interval:limit). TTLs are configured in _CACHE_TTL and _PRICE_TTL.
- Rate limiting uses _RATE_LOCK, _LAST_CALL, and _MIN_INTERVAL to avoid provider throttling.

Developer notes
- Entry point: app.py
- Core logic: data_source.py
- Startup layout (panes + default source) comes from config.toml; see
  "Startup layout (config.toml)" above. Absent file → one yfinance pane.
- No unit test suite currently; add pytest tests under tests/ if desired.

Contributing
- Open issues or PRs for bugs and features.
- When adding a new data source, reuse _SESSION, call _rate_limit(), and register it in _DATASOURCE_REGISTRY.

License
This project is licensed under the MIT License — see the LICENSE file for details.

Maintainer
- Repo: trading-dashboard
- For questions, open an issue on this repository.
