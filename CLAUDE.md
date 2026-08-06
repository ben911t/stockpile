# CLAUDE.md — stockpile

Claude Code instructions for this monorepo.

## Running the tools

Always run from the **repo root** using `uv run`. Never use `python`
or `python3` directly.

### Positions tracker (Google Sheets)

```bash
uv run positions/run_tracker.py
uv run positions/run_tracker.py --brokerage schwab
uv run positions/run_tracker.py --csv input/OTHER.csv
```

Reads `positions/config.toml` (account sheet IDs + CSV paths).
Credentials at `~/.config/google-sheets-oauth.json`. First run opens
a browser for OAuth; subsequent runs are silent.

### Cost basis charts

```bash
uv run cost-basis-charts/run_charts.py
uv run cost-basis-charts/run_charts.py --symbol SCHW
```

Reads `cost-basis-charts/config.toml`. Writes HTML (and optional PNG)
to `cost-basis-charts/output/`.

### Options scanner — web UI (recommended)

```bash
uv run streamlit run options-scanner/run_app.py
```

Opens at `http://localhost:8501` with tabs for Single Ticker, Watchlist,
**Positions**, Trades, Portfolio, GEX, Spreads/Directional/Neutral, and
**Live Charts** (the trading dashboard, embedded). The **Trades** tab lists
trades placed from the scanner; the **Positions** tab manages every live
Schwab option leg *and* the stock behind them. Select an option row and
pick the action: **Close** (all or part of the leg), **Roll**, or
**Unwind**. Close is the default. All three open on the same leg
snapshot — bid/ask/mid, last with its print time, IV%, delta, OI, volume
and IV+pp — so the contract reads the same whichever verb you pick;
IV+pp costs a chain fetch (~2s, cached 5 min), which these panels can
afford because they only render once a row is selected. Roll is offered
only on legs that can be rolled (short puts, share-backed short calls);
Unwind only on covered calls, since it buys the call back *and* sells
the shares behind it as one net-credit order (both legs fill together or
neither). A long or naked leg says why and offers the close builder.

Below the legs, **Your Stock Positions** lists every share you hold,
each row shaded by how much is written against it — uncovered, partly
covered, covered, or **over-written** (more calls than shares to back
them, the one risky state). Only positions with 100+ uncovered shares
get a select checkbox; picking one opens a covered-call builder that
scans that ticker and hands the chosen strike to the same **Sell Call**
dialog the Watchlist uses.

Positions is the only place that *places* a roll: it submits a
buy-to-close + sell-to-open as one atomic net-price order (Schwab only;
the Portfolio/Single "Roll" views stay analysis-only and point here).
Placing is where it stops — a working roll is monitored and
canceled on the **Trades** tab, listed as a `rolling` row with both
legs' live quotes and the net available now vs the order's limit.

The title bar carries a **📝 PAPER / 🔴 LIVE** badge (whenever Schwab is
configured) so the order mode is visible on every tab, and a **⚙️
Settings** gear beside it holding two display-only preferences: hide a
chosen underlying from the Positions tab entirely — its option legs
*and* its shares (the gear shows the hidden count, `⚙️ 3`) — and mask
account balances behind
`$•••••` everywhere they appear, with a 👁 button beside each masked
figure that reveals them for the session only. Preferences persist in
`options-scanner/settings/settings.json`; `config.toml` stays
hand-edited and keeps the credentials and the `paper` flag.
To launch the scanner **and** the dashboard together (so Live Charts is
populated), run `uv run run.py` from the repo root instead.

### Options scanner — CLI (single ticker)

```bash
uv run options-scanner/run_scanner.py AMD --calls
uv run options-scanner/run_scanner.py AMD --puts
uv run options-scanner/run_scanner.py AMD
uv run options-scanner/run_scanner.py AMD --roll \
    --type call --strike 600 --expiration 2026-01-16
```

### Options scanner — portfolio (brokerage CSV)

```bash
uv run options-scanner/run_portfolio.py --csv input/schwab028.csv
uv run options-scanner/run_portfolio.py --csv input/schwab028.csv \
    --html --tickers AAPL AMD
```

### Trading dashboard (live charts)

```bash
uv run trading-dashboard/app.py
```

Flask app at `http://localhost:5000` — multi-pane live candlestick
charts. Per-pane data source: **Yahoo Finance** or **Schwab** for
stocks, **Hyperliquid** for crypto. Schwab bars + real-time mark reuse
`stocks_shared.schwab_live` and the **shared** `[schwab]` credentials
in `options-scanner/config.toml` (same `~/.config/schwab-token.json`,
7-day token TTL — re-run `schwab_auth.py` if Schwab quotes go empty).
`run.cmd` / `run.sh` wrap the same `uv run`. The scanner embeds this
dashboard as its **Live Charts** tab; `uv run run.py` from the repo root
launches both together (Flask :5000 + Streamlit :8501), and :5000 stays
directly reachable too. The Live Charts iframe derives its host from the
browser's request, so remote/cloud access works when port 5000 is also
reachable from the client. Two env knobs (read by the Flask app, `run.py`,
and the embed alike): `OSC_DASHBOARD_PORT` moves the dashboard off 5000 when
that port is taken; `OSC_DASHBOARD_URL` overrides the whole embed URL for
reverse-proxy or single-exposed-port setups (and takes precedence over the
port).

## Project structure

- `shared/` — pip-installable `stocks-shared` package: CSV parsers,
  Yahoo Finance helpers, FIFO analysis, Black-Scholes pricing
- `positions/` — Google Sheets position tracker
- `cost-basis-charts/` — cost basis vs. price charts
- `options-scanner/` — options scanner (web UI + CLI)
- `trading-dashboard/` — Flask live candlestick dashboard
  (yfinance / Schwab / Hyperliquid data sources)
- `google-sheets-setup/` — Google Sheets API setup docs
- `input/` — brokerage CSV exports (gitignored)

## Sibling repo

YouTube production materials and the long-form ideas / research
parking lot live in a **separate private repo** at
`../stockpile-private/`:

- `options-scanner/youtube/epN/` — per-episode `script.md`, slide
  HTML, and image assets (GIMP `.xcf` sources alongside `.png`
  exports)
- `IDEAS.md` — speculative project ideas, Schwab-API sketches, and
  strategy research questions for this codebase

That repo holds no code. When its scripts or IDEAS.md reference files
like `schwab_auth.py` or `options-scanner/run_scanner.py`, those
paths are here.

## Keeping the two repos in sync

This repo and `../stockpile-private/` evolve together. Watch the
boundary and surface what you notice — don't act across it
unilaterally:

- **Code change here that affects the active episode** — if a
  feature, UI label, command flag, or behavior shown in the current
  in-flight `epN/script.md` changes, the script likely needs an
  update. Check which episode is in active drafting before assuming
  (ask the user, or look for the most recently edited `script.md`
  under `../stockpile-private/options-scanner/youtube/`).
- **Script change in the private repo that contradicts current
  code** — if a spoken description has drifted from what this code
  actually does, flag the mismatch rather than guessing which side
  is right.
- **Misplaced content** — if something here looks like it belongs in
  the private repo (a script draft, slide source, idea log) or vice
  versa (a config file or library that ended up under `youtube/`),
  call it out.

## Slash commands

Inside a Claude Code session, `/` shows available project commands:

| Command | What it does |
|---------|--------------|
| `/scan TICKER [flags]` | Options scanner CLI for one ticker |
| `/scan-portfolio --csv FILE` | Scan every open position in a CSV |
| `/scan-ui` | Launch the options scanner web UI |
| `/charts [--symbol X]` | Generate cost-basis charts |
| `/positions` | Run the Google Sheets position tracker |

## Environment

- Python 3.12+, managed by `uv`
- Single shared `.venv/` at repo root (`uv sync` to create/update)
- `stocks-shared` is installed as an editable local package
- Brokerage CSVs go in `input/` (gitignored)
- Config files are gitignored; examples are in `*.toml.example`
