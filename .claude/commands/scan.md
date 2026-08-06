---
description: Scan an option chain and rank options by IV vs. a fitted surface (sell or buy candidates)
---

Run the options scanner for the provided ticker and any extra flags:
`$ARGUMENTS`.

## When to Use

Use when asked to:

- Find covered call or cash-secured put candidates for a ticker
- Find IV-cheap options to buy (protective puts, LEAPS)
- Scan a list of tickers and compare opportunities
- Roll an existing short option position

Do **not** use when:

- The user needs full Greeks (gamma, theta, vega) — the scanner
  only displays delta; use the broker's position page instead.

## Command Mapping

| Question | Command |
|---|---|
| Find covered call candidates for AAPL | `run_scanner.py AAPL --calls` |
| Scan AAPL MSFT NVDA for IV-rich puts | `run_scanner.py AAPL MSFT NVDA --puts` |
| Find IV-cheap LEAPS to buy on SPY | `run_scanner.py SPY --buy --min-dte 180` |
| Only show strong signals (≥ 5 pp) | `run_scanner.py AAPL --min-ivpp 5` |
| Scan 30–60 DTE, real-time quotes via Schwab | `run_scanner.py AAPL --min-dte 30 --max-dte 60 --data-source schwab` |
| Roll an existing short call | `run_scanner.py AAPL --roll --type call --strike S --expiration DATE` |
| Limit to safer OTM strikes | `run_scanner.py AAPL --calls --min-strike 200` |
| Use v2 preset (per-expiration, z-score) | `run_scanner.py AAPL --preset v2` |
| Agent/script use (JSON output) | `run_scanner.py AAPL --calls --agent` |

## Execution

Execute from the repo root:

```
uv run options-scanner/run_scanner.py $ARGUMENTS
```

### Mode flags
`--calls`, `--puts`, `--buy`, `--both`, `--roll`

`--both` emits sell *and* buy rankings from a single chain fetch
(cheaper than two runs); in JSON it nests `sell` / `buy` objects, each
with its own `volume_filter_relaxed` flag.

### Roll flags (required with --roll)
`--type {call,put}`, `--strike S`, `--expiration YYYY-MM-DD`

### Filter flags
`--min-dte N` (default: 30), `--max-dte N` (default: 90),
`--min-oi N` (default: 25), `--min-vol N` (default: 10),
`--min-delta D` (default: 0.10), `--max-delta D` (default: 0.75),
`--min-strike X`, `--max-strike X`, `--min-ivpp N`, `--top N`
(default: 4)

### Output flags
`--html`, `--browser` (implies --html), `--output-dir DIR`,
`--json`, `--agent` (implies --json --quiet), `--quiet`,
`--no-legend`

### Data source
`--data-source {yahoo,schwab}`

- **Yahoo** (default) — free, no setup, but IV may be stale on
  thinly-traded strikes
- **Schwab** — real-time NBBO quotes and Greeks; requires a Schwab
  brokerage account and one-time developer setup

### Pluggable IV surface
`--preset {current,v2}` — surface-fit preset (default: current).
`--algorithm {global_poly,per_expiration,earnings_segmented}` — override
the preset's fit algorithm. `--fit-weights {none,oi,inv_spread,vega}`
— regression weighting. `--robust {none,huber,tukey}` — robust fit:
downweight (huber) or reject (tukey) outliers so a stale print can't
drag the surface toward itself. `--score {raw_pp,zscore,relative,
composite_exec,vrp,percentile}` — override the ranking score.
Both presets exclude short-dated (≤60 DTE) options spanning the next
earnings from the fit; longer-dated contracts stay in (one earnings is
a negligible share of their variance).

## Reading IV+pp

| Value | Interpretation |
|---|---|
| < 3 pp | Roughly uniform chain — no strong surface deviation |
| 3–5 pp | Moderate signal |
| > 5 pp | Strong signal — option is meaningfully IV-rich |
| Negative (buy mode) | Option sits below surface — IV-cheap |

IV+pp is a screening signal, not a mispricing guarantee. Verify
with your broker before acting.

## JSON Output Schema (`--agent` or `--json`)

Single ticker returns an object; multiple tickers return an array.

```json
{
  "ticker": "AAPL",
  "spot": 175.50,
  "data_source": "schwab",
  "scan_time": "2026-05-28T14:30:00Z",
  "mode": "sell",
  "candidates": [
    {
      "type": "call",
      "strike": 185.0,
      "expiration": "2026-06-20",
      "dte": 29,
      "bid": 1.50,
      "ask": 1.55,
      "mid": 1.53,
      "iv_pct": 28.5,
      "iv_pp": 6.2,
      "signal_score": 0.062,
      "signal_kind": "IV+pp",
      "delta": 0.28,
      "ann_pct": 4.8,
      "open_interest": 1250,
      "earnings_before_exp": false,
      "hv_20": 0.241,
      "vr_ratio": 1.18
    }
  ]
}
```

- `signal_score` is the ranking key; `signal_kind` names the active
  score (default `IV+pp`, where `signal_score = iv_pp / 100`). Other
  `--score` values use their own units (σ for zscore, ratio for VRP,
  0–100 for percentile).
- `hv_20` — 20-day annualized realized vol. `vr_ratio = iv / hv_20`.
  Both `null` when price history is unavailable.
- `net_credit` is added to each candidate when `--roll` is used.

## Instructions

If `$ARGUMENTS` is empty, ask for a ticker before running. Show the
scanner's stdout to the user. If `--html` or `--browser` was passed,
surface the output path so they can open it.
