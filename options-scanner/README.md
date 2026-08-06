# options-scanner

Scans an option chain and ranks each option by how far its implied
volatility sits above or below a fitted volatility surface. Use it
to surface IV-rich candidates for covered calls, cash-secured puts,
and roll setups — or, in buy mode, IV-cheap candidates.

The ranking is a **screening heuristic, not a mispricing or
arbitrage claim**: vol smiles and skew are real, and a strike
sitting above the fit can reflect demand pressure, event-specific
risk, or stale data as easily as a genuine signal. Treat the
output as a starting point for further analysis on your broker.
See [INTERPRETING_IV.md](INTERPRETING_IV.md) for what IV+pp
actually means mechanically, why an outlier is not the same as
edge, and how to read the magnitudes in practice.

Three entry points:

- **Web UI** — browser-based, no CLI knowledge required. Recommended.
- **CLI scanner** — single ticker, scriptable.
- **Portfolio scanner** — reads a brokerage CSV and scans every open
  position.

Data is sourced from **Yahoo Finance** (default, no setup) or the
**Schwab developer API** (real-time quotes and actual Greeks). See
[SCHWAB_DATA_SOURCE.md](SCHWAB_DATA_SOURCE.md) to enable Schwab.

For repo-wide setup (`uv sync`, etc.) see the
[root README](../README.md#setup).

**Videos:**
[Option Scanner by Claude (Python, GitHub)](https://youtu.be/0H7BGJ3rJoQ)
· [Find the Best Options with Schwab and Claude](https://youtu.be/-MsAMYX0kAM)
· [Find the Best Covered Call — Options Scanner](https://youtu.be/WVGH-Hjbnjs?si=w6FqHtbGoJsx887d)
· [I Asked Claude to Roll My Covered Call](https://youtu.be/qBNh6DIUSQQ?si=6M5g8Eu0mnODyb3g)

## Web UI

The recommended way is to launch the scanner **and** the trading
dashboard together with one command, so the **Live Charts** tab is
populated. Run from the repo root:

```bash
uv run run.py
```

This starts the scanner on `http://localhost:8501` and the dashboard on
`http://localhost:5000`, and opens a browser tab on the scanner. Both
servers stream their logs to the one terminal, line-prefixed `[scanner]`
and `[dashboard]` so you can tell them apart; the dashboard also stays
directly reachable at `http://localhost:5000`. `Ctrl+C` stops both. If a
dashboard is already running on `:5000`, it's reused rather than
restarted.

The scanner has nine tabs (below) — seven for analysis and two
(**Positions**, **Trades**) for managing what you hold at your broker; a
tenth, **Live Charts**, embeds the live trading dashboard.

To run them individually instead — each in its own terminal, with its own
logs (the scanner's Live Charts tab shows a start hint until the dashboard
is up):

```bash
uv run streamlit run options-scanner/run_app.py   # scanner only  (:8501)
uv run trading-dashboard/app.py                    # dashboard only (:5000)
```

Two of them — **Positions** and **Trades** — are filled Schwab blue in
the tab bar and sit side by side. Those read your live Schwab account,
so they need Schwab both configured *and* selected as the data source;
every other tab works on any source. Hovering the tab bar says the same
thing.

The tabs:

- **Single Ticker** — type a symbol, pick Calls/Puts/Both and Sell/Buy,
  hit Scan. Filter inputs: Min DTE / Max DTE, Min OI, Min Vol (today's
  trading volume), delta range, Top N. A **Fit:** preset toggle
  (Global / Per-expiry) plus an **Advanced surface fit** expander let
  you control which options enter the IV surface regression (OTM-only,
  spread threshold, delta range, min OI for fit) and which fit
  algorithm/score runs — all options still appear in results, only the
  fit is affected. You get a volatility-surface chart (dashed line =
  fitted surface — green for Yahoo, blue for Schwab; top picks labeled
  with their rank — `1` is the strongest signal per type), a
  per-expiration chain view sorted by strike with IV+pp row shading and
  a "Top" column showing the same rank, and a top candidates table
  ranked across all expirations. The surface chart marks contracts that
  fed the fit (filled dots) vs. those a filter excluded (hollow), adds a
  **Single / All expirations / 3D surface** view toggle (the 3D option
  renders the whole chain as an interactive strike × DTE × IV plot you
  can drag to rotate, colored by IV+pp deviation), and a
  **Surface-fit diagnostics** expander showing
  where contracts dropped out of the fit and each expiration's fit
  quality. A Market View card explains what your
  Direction × Option Type selection screens for, and selecting any
  candidate row opens an MC Analyze panel with the per-trade P&L
  distribution. The data source (Yahoo Finance / Schwab / Moomoo)
  toggle sits in the title bar so you can flip between sources without
  opening the sidebar.
- **Watchlist** — type a basket of tickers (commas, spaces, or new
  lines) and scan the lot — no broker account needed. Watchlists can
  be **saved by name** and reloaded later along with the filters they
  were saved with; a few starter baskets ship with the app under a
  `Starter:` prefix (e.g. `Starter: Mega Caps`) so they're easy to
  tell apart from your own saved lists, which stay local. A Sell/Buy
  direction toggle ranks IV-rich premium to write or IV-cheap
  contracts to buy.
- **Positions** — everything you hold at Schwab, whether or not it was
  placed here: every option leg, and below them the stock behind them.
  For each leg: moneyness (ITM%, which the rows are shaded and sorted by),
  live delta, Ann% on the remaining time value, Intrinsic | Time, what
  the stock and the option cost at open, market value and P/L per leg.
  Select a row and choose what to do with it. All three actions open on
  the same leg snapshot — spot, bid/ask/mid, last with its print time,
  IV%, delta, OI, volume and IV+pp — so the contract reads the same
  whichever verb you pick:
  - **Close** (the default) — all or part of the leg.
  - **Roll** — scans target strikes/expirations (ranked by IV+pp, with
    the closing leg's own IV+pp shown for comparison and a Net Credit
    column), then submits the buy-to-close and sell-to-open as **one
    atomic net-price order**. This is the only place in the app that
    *places* a roll. Offered on legs that can be rolled — short puts and
    share-backed short calls; a long or naked leg says so and offers the
    close builder instead.
  - **Unwind** — exits a covered call completely: buys the call back
    **and** sells the shares behind it, as one net-credit order. Both
    legs fill together or neither does, so there's no window where the
    call is closed and you still hold the stock (or the shares are gone
    and the call is left naked). Offered on covered calls only — it
    needs both halves — and priced as a net per share (stock proceeds
    minus the buyback). Partial unwinds work at 100 shares per contract;
    any extra shares are left untouched and the panel says how many.
    The share leg gets the equity equivalents beside the call's
    snapshot: bid/ask/mid/last plus today's move and volume, and Schwab's
    mark when it differs from the midpoint — but no OI, IV or IV+pp,
    which mean nothing for stock. Under the account balances
    it shows the call's remaining **time
    value** — per share, whole-leg, and annualized against spot — which
    is what unwinding early hands back to whoever sells you the call.
    Near zero means there's nothing left to wait for. The share sale's
    gain/loss isn't tracked here: the trade log models premium received
    and has no cost basis for your stock.

  All three need `paper = false` — these are real positions, so paper
  mode is view-only for closing, and build-and-price-only for rolling and
  unwinding. Placing is where this tab stops: once an order is in, you
  watch it fill (or cancel it) on the **Trades** tab, where every working
  order lives.

  Below the option legs, **Your Stock Positions** lists every share you
  hold with the calls written against them, so the question "where could
  I still sell a call?" is answered without cross-referencing two tables.
  Each row is shaded by coverage:

  | Shade | State | Meaning |
  |-------|-------|---------|
  | sky | **Uncovered** | No calls written, and a 100-lot is free |
  | yellow | **Partly covered** | Some written; another lot still free |
  | slate | **Covered** | Written to the last whole lot |
  | red | **Over-written** | More calls than shares to back them |

  "Covered" means no *writable* lot is left, not that every share is
  spoken for — 450 shares against 4 calls leaves 50 unwritten, and
  there's nothing you can do with 50 shares. **Over-written** is called
  out separately because it's the only risky state here: the excess call
  is naked, so assignment forces a buy at market. An uncovered odd lot
  (under 100 shares) is left unshaded — it's uncovered, but there's
  nothing to act on.

  Only positions with **100+ uncovered shares** get a select checkbox —
  they're listed first, and everything else (fully covered, an odd lot,
  or over-written) sits below under *Nothing to write*, same columns and
  shading but read-only. So the checkbox itself tells you where a trade
  is available.

  Select a row with a free lot and a **covered-call builder** opens,
  led by the stock's spot and today's move (green up, red down) — where
  the stock is and which way it's going is what decides which strikes
  are worth scanning. Then the same filters as the roll builder:
  Min OI, Min Vol, Min DTE, Max DTE, a |Delta| band, plus
  Min Strike and Max Strike. Hit **Scan calls** and pick
  from candidates ranked by IV+pp, with a Credit column for the full
  coverable size. Setting **Min Strike** at or above your average cost
  (shown in the table, and in the field's tooltip) is how you keep
  assignment from realizing a loss on the shares. Picking a row opens the
  same **Sell Call** dialog the Watchlist leaderboard uses, so the order
  builder, contract cap, confirm gate and trade log are identical
  whichever way you got there.
- **Trades** — every trade placed from the scanner (the Watchlist
  leaderboard's *Sell Put* / *Sell Call* dialogs). Each row shows the
  contract, live cost-to-close, unrealized P/L, and an intraday chart
  of the underlying; the collapsed header carries spot and today's
  change so you can scan the list without expanding. The status word
  distinguishes the two things people conflate: **working** = the
  opening order is still out there unfilled, **held** = it filled and
  you own the position. Then **closing** / **rolling** while an order
  on it is working, and **closed** / **expired** / **assigned** at the
  end. Close all or part
  of a position from here: **Confirm Closing Trade** arms it, **Place
  Closing Trade** sends it. A paper trade closes in the tracker only
  (no broker order); a live one sends a real buy-to-close.
  A roll placed on the Positions tab also shows here while it works, as
  a `rolling` row headed `$150 → $160`. Expand it to watch both legs'
  bid/ask/mid and the net the market is paying now against the limit on
  your order — so you can see whether it's reachable or you're waiting
  for the market to come to you. Cancel it from either tab.
- **Portfolio** — drag in a brokerage CSV (Schwab, Robinhood, Fidelity,
  Merrill, or a hand-written
  [stockpile file](../docs/stockpile-format.md)), pick the format, hit
  Scan Portfolio. Each position gets its own chart and table in a
  collapsible section, with a Recommended Action card above the table
  translating the top IV-rich pick into explicit SELL TO OPEN / ROLL
  instructions. The validator runs automatically on upload and shows
  any problems before you scan. Both Watchlist and Portfolio scans
  produce a cross-ticker IV+pp leaderboard above the per-ticker
  results.
- **GEX** — the Gamma Exposure view: one or more tickers, with
  render-time Side (Calls/Puts/Both) and Min OI filters on the stored
  scan. See the GEX section below.
- **Spreads** — power-user view of 13 multi-leg strategies ranked by
  risk/reward subject to a POP threshold. Click a row to see the
  payoff diagram (at-expiry + current value).
- **Directional** — bullish/bearish strategies only (verticals,
  jade lizard, risk reversal).
- **Neutral** — range-bound and delta-neutral strategies with a
  Max \|Δ\| slider for income hunting on long-DTE underlyings.

### Placing trades: what protects you

Every screen that can send an order — the Sell Put / Sell Call dialogs,
both close builders, the roll, and the unwind — works the same way:

- **`paper` decides everything.** `paper = true` in `config.toml`
  records simulations and sends nothing. `paper = false` sends real
  orders. The mode shows as a **📝 PAPER** / **🔴 LIVE** badge in the
  title bar on every tab, and again on the buttons that would send.
  It's deliberately not editable from the UI — but you don't have to
  restart to change it: the file is re-read on every rerun, so edit it
  and click anything in the app (`config.toml` isn't a watched source
  file, so saving alone won't refresh the page). Watch the title-bar
  badge to confirm the switch took.
- **Two steps, never one.** *Confirm…* arms the order for the exact
  values on screen; only *Place…* sends. Editing the limit or the
  contract count disarms it, so Place can never apply to numbers you
  didn't confirm — as does switching a selected position between Close,
  Roll and Unwind, since those are three different orders. Confirm greys
  out while armed — **Cancel** is the way back — and the two buttons are
  never both live.
- **Your inputs are validated, not silently clamped.** Ask for more
  contracts than you can cover and you get told why ("5 contracts
  exceeds the 4 you can cover"), rather than the field quietly snapping
  back to a number you didn't choose. Confirm stays clickable so a
  correction takes one click.
- **Live orders are market-gated.** Outside 9:30–16:00 ET Mon–Fri, or
  when market hours can't be confirmed, the live path is disabled.

Rolls and unwinds act on real positions, so they're live-only by design
— a simulated one would desync the tracker from your broker.

### Settings (⚙️, top right)

The gear in the title bar opens a Settings dialog, available from every
tab. It holds two sections: **hidden positions** and **masked
balances**.

#### Hidden positions

Tick a symbol to keep it out of the **Positions** tab — useful when a
position is managed elsewhere, or is parked and you don't want it in the
way. Hiding is **all or nothing per symbol**: one tick covers every
option leg on that underlying, including ones you open later, *and* its
shares in the stocks table. What each tick covers is listed underneath
it ("2 leg(s) + 400 shares").

Every symbol you hold is offered, including ones you hold **only shares
of** — a name with no options on it at all is still pickable. A narrower
hand-written rule (one strike, or all puts on a ticker) describes a leg,
so it leaves the stock row alone.

Hiding is **display only**. The position is still held, still
assignable, and still counts toward covered-call coverage and buying
power — the blacklist never touches the sizing or coverage math.

So a hidden position can't be forgotten, it's surfaced four ways:

- The gear itself turns amber and shows the count of hidden symbols
  (**⚙️ 3**) on every tab; hovering says what's hidden.
- The Positions tab shows a hidden count at the bottom of the option
  table, with an expander listing what's hidden and a *show these
  anyway* toggle that lasts only for the session. The stocks table names
  its hidden symbols in a caption of its own — a symbol you hold no
  options on never reaches that expander, so it can't rely on it.
- If everything is hidden, the empty state says so rather than implying
  your account is empty.
- A hidden **short** leg within 7 days of expiration escalates from a
  caption to a warning.

#### Masked balances

For screen-sharing and recording. Tick **Mask account balances** and
every account figure renders as `$•••••` — the buying-power line on the
Positions table, both close builders, and the roll and unwind panels,
plus the Sell dialog's available-funds readout and its full Account info
panel.

A **👁** button appears beside each masked figure. It reveals every
masked number in the app at once, **for the session only** — the tick is
what persists, so you can read a balance without quietly turning masking
off for good. With masking on, the same button re-hides them (🙈); with
masking off, there's no button to clutter the screens.

Like hiding positions, this is **display only**. Sizing, coverage and
affordability checks read the real figures either way, so masking can
never change what an order does. It hides what's *in the account* — not
what a trade costs: order prices, collateral, position values and P/L
stay visible.

Preferences live in `options-scanner/settings/settings.json`
(gitignored), written by the dialog and safe to hand-edit. A narrower
rule written in by hand — one strike/expiration, or all puts on a ticker
— is honored by the tables and removable in the dialog, which lists it
under *Other rules*. It's a
separate layer from `config.toml`, which stays hand-edited only and
keeps everything security- or safety-critical: Schwab credentials and
the `paper` live-order flag are deliberately *not* editable from the UI.
A malformed settings file hides nothing and says why.

See [SPREADS.md](SPREADS.md) for the full strategy catalog, column
reference, POP math, and caveats.

See [MONTE_CARLO.md](MONTE_CARLO.md) for the MC Analyze panel
(per-trade P&L distribution under GBM + earnings jumps), the
Market View card, the Portfolio Recommended Action card, and what
the simulation's assumptions get wrong.

Single Ticker and Portfolio tabs offer a Download HTML Report button.

### What's actually running at localhost:8501

`streamlit run` starts a local **Uvicorn** web server. The browser
loads the page over HTTP, then opens a persistent **WebSocket** that
streams widget changes back to Python; every interaction re-runs
`run_app.py` top-to-bottom and pushes the new output to the page.

By default Streamlit binds to `0.0.0.0`, so the app is reachable from
other machines on your network at the "Network URL" Streamlit prints
(e.g. `http://10.0.0.5:8501`). For solo home use that's harmless. On a
shared/public network, pass `--server.address 127.0.0.1` to bind only
to localhost.

To stop the server: `Ctrl+C` in the terminal where you started it.
There is no in-app shutdown button. If `Ctrl+C` doesn't stop it —
common on Windows with the `uv run run.py` launcher — see **`Ctrl+C`
doesn't stop the running server (Windows)** under Common problems below.

### Changing the ports

By default the scanner runs on `8501` and the dashboard on `5000`. If
either port is already in use, you can move it.

**Dashboard (`5000`)** — set `OSC_DASHBOARD_PORT`. The Flask app, the
`uv run run.py` launcher, and the scanner's Live Charts embed all read
the same variable, so they stay in sync:

```bash
# macOS / Linux
export OSC_DASHBOARD_PORT=5050
uv run run.py

# Windows PowerShell
$env:OSC_DASHBOARD_PORT = "5050"
uv run run.py
```

For remote/cloud access where the dashboard sits behind a reverse proxy
or on a different host, set `OSC_DASHBOARD_URL` to the full embed URL
instead — it's used verbatim as the iframe source and takes precedence
over `OSC_DASHBOARD_PORT`:

```bash
export OSC_DASHBOARD_URL="http://my-server:5050"
```

**Scanner (`8501`)** — this is Streamlit's own port, so use Streamlit's
config. Put it in a `.streamlit/config.toml` file (project-level at the
repo root, or global at `~/.streamlit/config.toml`); it's honored
whether you launch with `uv run run.py` or `streamlit run` directly:

```toml
[server]
port = 8502
```

Or pass `--server.port` when launching the scanner on its own:

```bash
uv run streamlit run options-scanner/run_app.py --server.port 8502
```

Note: the `STREAMLIT_SERVER_PORT` environment variable does **not** work
in current Streamlit — it applies env vars only to a few "sensitive"
options, and `server.port` isn't one. Use `config.toml` or
`--server.port`. The `uv run run.py` launcher doesn't forward a
`--server.port` flag, so for the combined launcher the `config.toml`
route is the one to use.

### Common problems

**`Port 8501 is already in use` (or app appears on `:8502`)**
A previous Streamlit is still running. Stop it with `Ctrl+C` in its
terminal, or pass `--server.port 9000` to use a different port.

**Browser doesn't open automatically**
Happens on some Windows setups and over SSH. Just paste the URL the
terminal printed. Pass `--server.headless true` to suppress the
auto-open attempt.

**Windows Firewall prompt the first time**
Allow on Private networks; deny Public.

**First scan takes 5–15 seconds**
Normal — fetching the chain from Yahoo Finance, fitting the surface,
looking up earnings. There's a spinner.

**Empty chart on a ticker that worked moments ago**
Yahoo throttling. The 5-minute cache mitigates repeated scans of the
same ticker; otherwise wait it out.

**Edited `run_app.py` and the chart still looks wrong**
Streamlit auto-reloads code, but `@st.cache_data` results survive
across reruns. Open the hamburger menu (top-right) → **Clear cache** →
rerun.

**`Ctrl+C` doesn't stop the running server (Windows)**
Most common with the combined `uv run run.py` launcher: it starts the
Flask dashboard in its own process group, so a single `Ctrl+C` — or a
`Ctrl+C` in a terminal that's no longer attached to the launch — can
leave the whole tree (uv → run.py → Streamlit + Flask + their workers)
alive with the ports still bound. The fix is to find the launcher's root
process and kill the **tree** (`/T` reaps every child, `/F` forces it):

```powershell
# Find the `uv run run.py` root PID, then kill its whole process tree
Get-CimInstance Win32_Process -Filter "Name='uv.exe'" |
  Where-Object CommandLine -like '*run.py*' |
  ForEach-Object { taskkill /F /T /PID $_.ProcessId }
```

If you launched Streamlit on its own (no `uv run run.py`, so there's no
`uv` root), tree-kill by port instead:

```powershell
taskkill /F /T /PID (Get-NetTCPConnection -LocalPort 8501 -State Listen).OwningProcess
```

Confirm it's fully down — no output means the ports are clear:

```powershell
Get-NetTCPConnection -LocalPort 5000,8501 -State Listen -ErrorAction SilentlyContinue
```

**Orphan Python processes on Windows (port 8501 already in use)**
If Streamlit was stopped with the terminal closed or crashed, Python
processes can keep running and block port 8501 on the next launch.
Kill them with a targeted PowerShell one-liner:

```powershell
Stop-Process -Id (Get-NetTCPConnection -LocalPort 8501).OwningProcess -Force
```

If that fails (nothing on 8501 yet but the launch still hangs), use
the broader form — caution: this kills **all** Python processes:

```powershell
taskkill /F /IM python.exe
```

Recommendation: always kill orphan processes rather than leaving them
running; they waste memory and will block the port on every future
launch.

**`ModuleNotFoundError` after a `git pull`**
Dependencies changed. Run `uv sync` from the repo root.

## Portfolio scanner (CLI)

```bash
uv run options-scanner/run_portfolio.py --csv input/schwab028.csv
uv run options-scanner/run_portfolio.py --csv input/schwab028.csv \
    --html --tickers AAPL AMD
```

Reads the CSV, finds every open stock position, and runs a sell scan
on each. Positions with an existing covered call get a roll scan
showing the `NetCr` column instead. Add `--html` for one combined
report covering the whole account.

## CLI scanner

Always run from the **repo root** using `uv run`:

```bash
# Covered call selection
uv run options-scanner/run_scanner.py AMD --calls

# Cash-secured put selection
uv run options-scanner/run_scanner.py AMD --puts

# Both calls and puts
uv run options-scanner/run_scanner.py AMD

# Narrow to a delta range (e.g. 0.20–0.45 sweet spot)
uv run options-scanner/run_scanner.py AMD --calls \
    --min-delta 0.20 --max-delta 0.45

# Roll an existing short call
uv run options-scanner/run_scanner.py AMD --roll \
    --type call --strike 600 --expiration 2026-01-16
```

### Index tickers

Each data source uses a different prefix for cash-settled index
options. The scanner normalizes automatically so you can always type
the bare name:

| Index | Yahoo Finance | Schwab |
|-------|--------------|--------|
| S&P 500 | `^SPX` / `^SPXW` | `$SPX` / `$SPXW` |
| Nasdaq 100 | `^NDX` / `^NDXP` | `$NDX` / `$NDXP` |
| Russell 2000 | `^RUT` | `$RUT` |
| VIX | `^VIX` | `$VIX` |
| Dow Jones | `^DJI` / `^INDU` | `$DJI` / `$INDU` |
| S&P 100 | `^OEX` / `^XEO` | `$OEX` / `$XEO` |
| Volatility (Nasdaq/Russell) | `^VXN` / `^RVX` | `$VXN` / `$RVX` |
| Treasury rates | `^TNX` / `^TYX` | `$TNX` / `$TYX` |

All of these forms resolve to the same result:

```
SPX        bare name — works on both Yahoo and Schwab
^SPX       Yahoo Finance native form — also works on Schwab
$SPX       Schwab native form — also works on Yahoo Finance
```

**Escaping:** If a ticker symbol conflicts with a known index name
(e.g. `SPX` is also NYSE-listed SPX Corp), append `!` to bypass
normalization and query the underlying stock directly:

```
SPX!       use exactly "SPX" — fetches the stock, not the index
```

### All options

| Flag | Default | Meaning |
|------|---------|---------|
| `--calls` / `--puts` | both | Show only calls or only puts |
| `--buy` | off | Buy mode: rank by IV vs. surface, lowest first (IV-cheap relative to neighbors) |
| `--min-dte` | 30 | Minimum days to expiration |
| `--max-dte` | 90 | Maximum days to expiration |
| `--min-oi` | 25 | Minimum open interest. Filters the top candidates table only; the volatility-surface chart and per-expiration chain table show all strikes, with low-OI rows shaded yellow as a liquidity warning. |
| `--min-vol` | 10 | Minimum daily volume (same table-only filtering as `--min-oi`) |
| `--min-delta` | 0.10 | Exclude abs(delta) below this |
| `--max-delta` | 0.75 | Exclude abs(delta) above this |
| `--min-strike` / `--max-strike` | — | Restrict candidates to a strike range |
| `--min-ivpp` | — | Only show candidates with IV+pp at or above this |
| `--top` | 4 | Max rows shown in terminal |
| `--html` | off | Save an HTML report (see below) |
| `--browser` | off | Save the report and open it in your browser (implies `--html`) |
| `--output-dir` | `options-scanner/output/` | Directory for HTML files |
| `--json` / `--agent` | off | Emit JSON instead of a table (`--agent` implies `--json --quiet`, for scripting) |
| `--roll` | — | Roll mode (requires `--type`, `--strike`, `--expiration`) |
| `--data-source` | from config | `yahoo` or `schwab` — overrides config.toml. Moomoo is web-UI/config-only, not a flag value |
| `--preset` | `current` | Surface-fit preset: `current` (global poly + IV+pp) or `v2` (per-expiration + z-score) |
| `--algorithm` / `--fit-weights` / `--score` | from preset | Override individual surface-fit stages |

For the complete, authoritative flag list (including `--quiet` and
`--no-legend`) run `uv run options-scanner/run_scanner.py --help`, or
see the `/scan` command reference at `.claude/commands/scan.md`.

### HTML report

Add `--html` to save a self-contained HTML file alongside the
terminal output:

```bash
uv run options-scanner/run_scanner.py AMD --calls --html
```

The file is written to `options-scanner/output/` by default, named
`{TICKER}_{type}_{action}_{date}.html` (e.g.
`AMD_call_sell_20260505.html`). Non-default scans append extra segments
(roll strike, a custom DTE or strike range, a non-default `--top`) so
reports don't overwrite each other. Open it in any browser — columns are
sortable by clicking the headers, and the IV+pp column is
color-coded (green = IV-rich, a candidate to consider selling;
red = IV-cheap, a candidate to consider buying).

Override the directory with `--output-dir path/to/dir`.

## Output columns

| Column | What it means |
|--------|--------------|
| Top | Web UI only. Rank within the top-N list per option type (1 = strongest signal). Blank for rows that didn't make the cut. |
| Strike | Option strike price |
| Expiration | Expiration date |
| DTE | Days to expiration |
| Bid / Ask / Mid | Market prices |
| IV% | Implied volatility (annualized) |
| IV+pp | IV excess above the fitted surface (see below) |
| Delta | Approx. probability of expiring in the money |
| Ann% | Annualized yield on premium (calls vs. spot; puts vs. strike) |
| OI | Open interest |
| Vol | Web UI only. Today's trading volume — short-term liquidity signal complementing OI. |
| NetCr | Roll mode only: new mid minus close cost |

> **Times.** All dates and times shown in the app — including the
> last-trade time beneath the **Last** price in the Sell, Roll, and Trades
> dialogs — are U.S. Eastern (New York) market time (EST/EDT), regardless
> of your local timezone.

## Example output and how to read it

```
--------------------------------------------------------------------
  AMD   spot: $355.26   LT close if opened today: May 06 '27
  Next earnings: May 05
--------------------------------------------------------------------

  CALLS
Strike  Expiration      DTE  Bid     Ask     Mid      IV%  IV+pp  Delta  Ann%    OI
------  ------------  -----  ------  ------  ------  ----  -----  -----  ----  ----
$700    Jun 17 '27      408  $27.15  $29.60  $28.38  65.1   +1.6   0.29   7.1   461
$590    Jun 17 '27      408  $39.40  $42.55  $40.97  65.2   +1.3   0.38  10.3    59
$600    Jun 17 '27      408  $37.90  $40.45  $39.17  64.9   +1.1   0.36   9.9   473
$530    Jun 17 '27      408  $48.75  $52.30  $50.52  65.2   +1.0   0.44  12.7  2179
$520    Jun 17 '27      408  $50.45  $54.45  $52.45  65.3   +1.0   0.45  13.2   474
```

### Is there a genuine IV outlier?

Look at the `IV+pp` column first. If the top value is under ~3pp,
the chain's IV is roughly uniform and the ranking is mostly noise.
In the AMD example above, the max is +1.6pp — all these options
sit close to the fitted surface. When you see IV+pp of 5pp or more
on a specific strike, that's a stronger ranking signal worth a
closer look on your broker.

See [INTERPRETING_IV.md](INTERPRETING_IV.md) for what those numbers
mean mechanically and why an outlier isn't the same as a
mispricing.

### Picking a strike

When IV+pp is flat across the chain (as above), the decision comes
down to your own risk tolerance:

**Lower delta (e.g. $700, delta 0.29):**
- ~29% chance of assignment at expiration
- Collects $28.38 per share (~7% annualized)
- More room for the stock to run before you're called away

**Higher delta (e.g. $530, delta 0.44):**
- ~44% chance of assignment — roughly a coin flip
- Collects $50.52 per share (~12.7% annualized)
- Much better premium, but real risk of losing the shares

A common covered call sweet spot is delta 0.25–0.40, which balances
premium against assignment risk. Use `--min-delta 0.25 --max-delta
0.40` to filter to that range.

### Earnings

The scanner tracks the **next** earnings date only (further-out dates
from the data source are estimates, not company-confirmed). The summary
shows it ("Next earnings: …"), and a `⚠` next to an expiration in the
ranked tables flags one that is **≤60 DTE and expires after that date**
— so its IV+pp includes earnings premium (and it's the slice excluded
from the surface fit). Elevated IV near earnings is expected and is not
a free lunch — see
[INTERPRETING_IV.md](INTERPRETING_IV.md#earnings-and-iv) for why.

### LT capital gains

The header shows the earliest date you could close to qualify for
long-term capital gains treatment (open date + 366 days). If you sell
today and close after that date, the premium is taxed at the LT rate.
In the example: sell today, close any time after **May 06 '27**.

### Ann% for puts

For puts, `Ann%` is calculated as premium divided by the **strike
price** (the capital you'd need to buy 100 shares if assigned),
annualized. This gives the true return on capital at risk.

## Gamma Exposure (GEX)

The web UI shows a **GEX bar chart** on its own **GEX** tab, with
Side (Calls/Puts/Both) and Min OI filters applied at render time —
no rescan needed. It is not available in the CLI.

### What it is

GEX measures the aggregate gamma that market makers (dealers) hold
across every strike in the chain. Because dealers typically sell
options to retail buyers, they end up short gamma. To stay delta-
neutral they must hedge:

- **Short gamma (negative GEX):** dealers buy stock as price rises
  and sell as it falls — amplifying moves in both directions.
- **Long gamma (positive GEX):** dealers sell into rallies and buy
  dips — dampening moves and pinning price near high-OI strikes.

### How to read the chart

The chart shows net GEX per strike as green (positive / pinning) or
red (negative / amplifying) bars. The dashed vertical line marks
the current spot price (with the `Spot $XXX.XX` label next to it).
The chart title carries the ticker symbol and the caption notes how
many expirations and what DTE range were summed — so a screenshot
stays self-explanatory days later.

Three summary metrics appear above the chart:

| Metric | What it means |
|--------|--------------|
| **Total GEX** | Sum across all strikes. Positive = pinning regime; negative = amplifying regime. |
| **Regime** | Plain-English label for the current total GEX sign. |
| **Zero-gamma level** | The strike where cumulative dealer gamma flips from positive to negative. Price above this level tends to behave more volatilely. |

### What it tells a covered call seller

A large **green wall above your strike** means dealers are long gamma
there — their hedging activity tends to cap the stock near that
level, acting like a ceiling. The stock has trouble breaking
through, which is what a covered call seller wants.

A **red zone above your strike** means the opposite — if the stock
enters that range, dealer hedging amplifies the move and your call
is more likely to get tested.

### Caveats

**Long-dated chains are thin.** GEX is most reliable on heavily
traded near-term options (0–60 DTE) where OI is large and IVs are
fresh. LEAPS and other far-dated options have lower OI and wider
bid/ask spreads, so treat the chart as directional context rather
than a precise signal.

**Yahoo Finance data quality.** When using Yahoo Finance, gamma is
estimated via Black-Scholes from Yahoo's IV, which can be hours or
days old on LEAPS and other far-dated strikes. GEX computed from
stale IV is a rougher approximation. The Schwab data source provides
real-time gamma values from Schwab's own model, which are more reliable.

**Dealer positioning is assumed, not measured.** This is GEX's
biggest limitation. The model assigns a *sign* to every contract's
gamma — here, dealers are treated as long calls and short puts (the
standard SqueezeMetrics convention). That direction is only a
tendency and is never verifiable from public data: OPRA, Yahoo, and
Schwab chains report price, size, and open interest, but not whether
the market maker was the buyer or the seller of each contract.
Concentrated flows break the assumption — a fund running a
covered-call program systematically *sells* calls, leaving the dealer
*long* those calls and flipping the sign of their gamma at those
strikes. Distinguishing real buyers from sellers needs trade-level,
market-maker-tagged data (e.g. CBOE's paid Open-Close volume);
inferring it from the tape (a print above or below the mid) is itself
an approximation that fails for trades at the midpoint. Treat GEX as
directional context, not a measured fact.

## Roll mode example

```bash
uv run options-scanner/run_scanner.py AMD --roll \
    --type call --strike 600 --expiration 2026-01-16
```

Adds a `NetCr` column showing what you'd receive net after buying
back the existing position. Positive = net credit roll. The table
shows only calls (same type as the position being rolled), ranked
by IV excess so the richest new premium surfaces first.

## Data sources

The tool supports three data sources, selectable via `config.toml` or
the title-bar toggle in the web UI. The `--data-source` CLI flag
overrides the config for one run, but takes only `yahoo` or `schwab` —
Moomoo has to be set in `config.toml`:

| Source | Setup | Data quality |
|--------|-------|-------------|
| **Yahoo Finance** (default) | None — works out of the box | Delayed IV, no live Greeks |
| **Schwab** | Free Schwab developer account; see [SCHWAB_DATA_SOURCE.md](SCHWAB_DATA_SOURCE.md) | Real-time quotes, actual Greeks |
| **Moomoo** | Run the OpenD local gateway; US Level 2 subscription for live option Greeks | Real-time quotes and Greeks |

When using Schwab, delta comes directly from Schwab's model rather
than being estimated via Black-Scholes from stale IV. Earnings dates
still come from Yahoo Finance — the Schwab API does not provide them.

Before relying on Yahoo Finance output, it's worth understanding
where that data falls short.

### Yahoo Finance limitations

**Stale implied volatility.** Yahoo returns the IV from the last
option trade, not a live market-maker quote. On thinly traded
strikes — common on LEAPS, deep OTM, and low-volume tickers —
that trade may have happened hours or days ago. Stale IV
distorts the surface fit, produces false IV+pp signals, and
makes the Black-Scholes delta unreliable. A lone dot sitting
far from its neighbors with no obvious reason is almost always
a stale quote, not a real signal.

**No live bid/ask.** The bid and ask returned are from the last
market refresh, not a live feed. For actively traded near-term
options this is usually fine; for LEAPS it can be meaningfully
wrong. Always check the live spread on your broker before
placing a trade.

**Greeks not provided.** Yahoo does not return delta, gamma,
theta, or vega. Delta is computed from Black-Scholes using
Yahoo's IV — so if the IV is stale, the delta is too.

**Rate limiting.** Yahoo's unofficial API is unauthenticated and
subject to throttling. Repeated rapid scans can return empty
results. The scanner caches results for 5 minutes to mitigate
this.

**Expiration coverage.** Yahoo may not return the full list of
expirations available at your broker. Some far-dated LEAPS
expirations can be missing entirely.

**Earnings dates.** Yahoo's earnings calendar is sometimes
missing, off by a day, or not yet populated for upcoming
quarters.

### Alternative data sources

These limitations are known, and one or more of the sources
below will likely be added to the tool soon — better data
support is on the roadmap and will be a drop-in improvement
when it lands. In the meantime, the sources below are options
if you want to explore plugging one in yourself:

### Free with a brokerage account

| Source | Notes |
|--------|-------|
| **Schwab** (`schwab-py`) | Real-time, full Greeks, clean REST API. Free for any Schwab account holder. Largest overlap with covered-call sellers. |
| **Tradier** | Very clean REST/JSON API, excellent docs, free developer sandbox with delayed data, real-time with a funded account. Most developer-friendly broker API available. |
| **Tastytrade** | Official API, free for account holders. Options-focused, good Greeks. Popular with the theta-gang crowd. |
| **Interactive Brokers** | Free for account holders via TWS API or newer REST API. Most comprehensive data, but requires their desktop app running as a gateway (`ib_insync` library). |
| **E\*TRADE** | Official OAuth-based API, free for account holders, decent option chain data with Greeks. |

### Free without an account (limited)

| Source | Notes |
|--------|-------|
| **Alpha Vantage** | Free API key, 25 requests/day on free tier. Option chains with some Greeks — workable for single-ticker use, too slow for portfolio scans. |
| **Polygon.io** | Free tier gives 15-minute delayed data; real-time ~$29/mo. Clean API, strong Python SDK, popular in the algo/quant community. |
| **Market Data App** | Free tier with options snapshots. Less well-known but solid. |

### Paid

| Source | Notes |
|--------|-------|
| **Polygon.io** | ~$29/mo for real-time. Most popular among independent developers. |
| **EODHD** | ~$20–50/mo, global coverage, options chains with Greeks. |
| **Intrinio** | Mid-tier pricing, solid quality, good Python SDK. |
| **CBOE LiveVol** | Professional grade, expensive — overkill for this use case. |

### Practical recommendation

**Schwab** (`schwab-py`) is now supported — see
[SCHWAB_DATA_SOURCE.md](SCHWAB_DATA_SOURCE.md) to enable it.
**Tradier** is the easiest next step for a second alternative — the
free developer sandbox lets you test without an active account, and
the REST/JSON responses map cleanly onto how `options_scanner/chain.py` fetches
data.

## Roadmap

Planned improvements, roughly in priority order:

- **IV Rank / IV Percentile (IVR/IVP)** — show how current IV compares
  to its 52-week range. High IVR means premiums are rich relative to
  recent history — the most important context for deciding whether to
  sell options on a given ticker.

- **Expected move** — derive the market-implied move for each
  expiration from the at-the-money straddle price. Useful when picking
  a strike: the expected move is the range the market thinks the stock
  will stay within by expiration.

- **Theta** — add time decay (per day) to the output alongside delta
  and the other Greeks. Options sellers care about how much premium
  they collect each day the position is held.

- **Tradier data source** — the free developer sandbox lets you test
  without a funded account; REST/JSON responses map cleanly onto the
  existing chain fetcher. Easiest next broker integration to add.

- **Interactive Brokers CSV support** — several users have requested
  this. Waiting on an example export file to spec the parser.

- **TastyTrade CSV support and investigate using as data source ** —
- user has requested this. Requres R&D, TT Volunteer contributers?

- **GEX on portfolio tab** — extend the GEX chart to each position
  in the portfolio scan, not just the single-ticker tab.

- **GEX-aware option ranking** — fold dealer-gamma context into the
  chain output: tag strikes sitting just below a large positive GEX
  wall (pinning resistance — favorable for covered calls), strikes
  inside a negative-GEX amplifying zone (caution for sellers), and
  proximity to the zero-gamma flip level. GEX is most reliable on
  near-term chains where OI is dense, which lines up well with the
  scanner's default DTE range (30–90).

- **IV term structure chart** — plot IV by expiration (rather than by
  strike) to show whether near-term or far-dated vol is elevated.
  Helps identify which expiration has the richest premium environment.

- **Skew chart** — plot IV by strike for a single expiration to
  visualize the put/call skew. Shows how the market is pricing
  downside vs. upside risk at a glance.

- **Portfolio-level Greeks summary** — aggregate delta, theta, and
  vega across all open positions so you can see total book exposure
  at a glance.

- **Roll expiration picker — monthly expirations** *(minor)* — replace
  the free-form date picker in Roll mode with a selectbox of upcoming
  monthly expirations (third Friday of each month, computed from today).
  More correct for options trading and avoids entering invalid dates.
  Trade-off: drops support for weekly expirations.

- **Third-party Schwab / Yahoo client libraries** — evaluate whether
  community-maintained CLIs / Python clients (e.g. `schwab-py`,
  `schwabdev`, `yfinance`) are worth adopting in place of the
  hand-rolled HTTP calls and OAuth flow currently in `options_scanner/chain.py`
  and `schwab_auth.py`. Tradeoffs: less code to maintain and easier
  access to endpoints we haven't wired yet (streaming quotes,
  account history) vs. taking on an external dependency that could
  go stale or change shape. Pick one per data source and prototype
  before committing.

## Disclaimer

This software is provided free of charge, as-is, with no warranty
of any kind. There is no guarantee of accuracy, completeness, or
fitness for any particular purpose.

Data is sourced from Yahoo Finance or the Schwab developer API
depending on your configuration. Output quality is limited by what
those sources return. Implied volatility figures can be stale,
especially on thinly traded strikes; bid/ask spreads on LEAPS can
be wide; and data may occasionally be missing or incorrect. Nothing
this tool produces should be taken as a guarantee of any particular
result.

This is not financial advice. Options trading involves substantial
risk of loss and is not appropriate for all investors. Do your own
research before acting on anything this tool surfaces. The authors
are not responsible for any trading losses or other damages arising
from use of this software.

## License

This project is free for personal, non-commercial use under the
[Creative Commons Attribution-NonCommercial 4.0 International
(CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/)
license. Commercial use is not permitted without a separate agreement.
If you're interested in licensing this for commercial purposes, reach
out to driekhof@gmail.com.