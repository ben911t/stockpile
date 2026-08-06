# stockpile

Monorepo of stock portfolio tools: options scanner, position
tracker, cost basis charts, trading dashboard, and shared
parsing/finance utilities.

> **Disclaimer** — This software is provided free of charge for
> non-commercial use, as-is, with no warranty of any kind. There is
> no guarantee of accuracy, completeness, or fitness for any particular
> purpose. All tools rely on third-party data sources (Yahoo Finance,
> Schwab developer API, brokerage CSV exports, Google Sheets) whose
> availability, accuracy, and format can change without notice; output
> quality is limited by what those sources return. Nothing produced by
> any tool in this repository constitutes financial advice. Investing
> and options trading involve substantial risk of loss. Do your own
> research before making any financial decision. The authors are not
> responsible for any trading losses or other damages arising from use
> of this software.

## Videos

These tools were built by me together with Claude Code, with help
from open-source contributors. The YouTube videos walk through how
each one works and how to use it with your own brokerage data.

### Stockpile tools

| Video | Tool |
|-------|------|
| [Claude Built My Stock & Options Tracker in Google Sheets from my Schwab Transactions](https://youtu.be/9uf3cyOWPBQ) | positions tracker |
| [Charts Your Broker Doesn't Show You (Using Claude Code)](https://youtu.be/LqroeMNC7AU) | cost-basis-charts |
| [Option Scanner by Claude (Python, GitHub)](https://youtu.be/0H7BGJ3rJoQ) | options-scanner |
| [Find the Best Options with Schwab and Claude](https://youtu.be/-MsAMYX0kAM) | options-scanner — Schwab data source |
| [Find the Best Covered Call — Options Scanner](https://youtu.be/WVGH-Hjbnjs?si=w6FqHtbGoJsx887d) | options-scanner — covered calls |
| [I Asked Claude to Roll My Covered Call](https://youtu.be/qBNh6DIUSQQ?si=6M5g8Eu0mnODyb3g) | options-scanner — CLI agent |
| [Find the Best Puts to Sell (Scanner + Claude)](https://youtu.be/ssSYnkTXnw4?si=57vhjK3o2AGr2wqZ) | options-scanner — cash-secured puts |
| [Option Scanner by Claude, with 3D Volatility Chart](https://youtu.be/81ZfOvXa70M?si=S7ViBfNhYK7sKRN9) | options-scanner — 3D IV surface |

### Related — Yahoo Finance CLI series

A companion series on building a Yahoo Finance CLI with Claude Code
(separate repo, [printingpress.dev](https://printingpress.dev)):

- [Yahoo Finance CLI with Claude | Part 1 of 3](https://youtu.be/fvHzGLpac14)
- [Stock Report Webapp using Yahoo Finance CLI, Made by Claude](https://youtu.be/o0re4J8iiNo)

## Projects

- **[options-scanner](options-scanner/README.md)** — Rank options by
  IV vs. a fitted surface to surface covered call, cash-secured put,
  and roll candidates. Web UI, CLI, and portfolio scanner. Supports
  Yahoo Finance, Schwab API, and Moomoo. With Schwab connected it also
  *places* trades — sell puts and covered calls, then close, roll, or
  unwind any live leg — behind a two-step confirm and a `paper` flag
  that defaults to simulation.
- **[positions](positions/README.md)** — Google Sheets position
  tracker fed from brokerage CSV exports.
- **[cost-basis-charts](cost-basis-charts/README.md)** — Interactive
  cost basis vs. price charts.
- **[trading-dashboard](trading-dashboard/README.md)** — Live
  multi-pane charting dashboard with technical indicators for crypto
  (Hyperliquid) and equities (Yahoo Finance or Schwab). Runs standalone
  as a Flask app, and appears as the scanner's **Live Charts** tab.
- **[google-sheets-setup](google-sheets-setup/README.md)** — Google
  Sheets API setup docs.
- **shared** — pip-installable `stocks-shared` package: CSV parsers
  (Schwab, Robinhood, Fidelity, Merrill Edge, and the
  [stockpile manual format](docs/stockpile-format.md)), Yahoo Finance
  and Schwab live API helpers, FIFO analysis, Black-Scholes pricing.

## Quick start

The fastest way to see something useful after cloning is the options
scanner web UI — no CLI knowledge required:

```bash
git clone https://github.com/medloh/stockpile.git
cd stockpile
uv sync
uv run streamlit run options-scanner/run_app.py
```

A browser tab opens at http://localhost:8501. Type a ticker on the
**Single Ticker** tab and hit Scan, or drag a brokerage CSV onto the
**Portfolio** tab.

To also start the live trading dashboard — it appears as a **Live Charts**
tab in the scanner — run `uv run run.py` instead of the `streamlit`
command above.

For setup details, brokerage CSV configuration, and tool-specific
commands, see each tool's README linked above.

## Updating

To pull in the latest changes:

**If you cloned with git:**

```bash
cd stockpile
git pull
uv sync          # picks up any new/changed dependencies
```

**If you downloaded the ZIP from GitHub:** download it again (green
**Code** button → **Download ZIP**), unzip it, and copy the contents
over your existing `stockpile` folder, replacing files when prompted.
Then run `uv sync` to pick up any dependency changes.

Either way your local data is preserved: `config.toml`, brokerage CSVs
in `input/`, and your own saved watchlists are gitignored, so updating
never overwrites them. (The bundled starter watchlists may update.)

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — install once with:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv sync` creates a single shared `.venv/` for all sub-projects and
installs everything. `uv run` activates it automatically — no manual
`activate` needed.

## Using Claude Code with this repo

All of the tools in this repo were built with a Claude Code
subscription. Clone the repo, open Claude Code in the project
directory, and ask it to help you configure and run any tool with
your own brokerage export.

Get Claude Code at: https://claude.ai/code

### Project slash commands

Inside a Claude Code session, type `/` to see project commands:

| Command | What it does |
|---------|--------------|
| `/scan TICKER [flags]` | Run the options-scanner CLI for one ticker |
| `/scan-portfolio --csv FILE` | Scan every open position in a brokerage CSV |
| `/scan-ui` | Launch the options scanner web UI |
| `/charts [--symbol X]` | Generate cost-basis charts |
| `/positions` | Run the Google Sheets position tracker |

## Troubleshooting

### Windows: `ImportError: DLL load failed while importing base`

This is Windows blocking pandas' C extension DLLs due to an
Application Control policy. Run from an **administrator PowerShell**:

1. Right-click PowerShell → **Run as administrator**
2. `cd path\to\stockpile`
3. Run the tool normally with `uv run ...`

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the fork → PR
workflow, and guidelines.

## License

This project is free for personal, non-commercial use under the
[Creative Commons Attribution-NonCommercial 4.0 International
(CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/)
license. For commercial licensing, contact driekhof@gmail.com.
