# CLAUDE.md — options-scanner

## Purpose

Scan an option chain and rank each option by IV excess — how far its
implied volatility sits above or below a fitted 2-D surface — to
surface IV-rich candidates for covered calls, cash-secured puts, and
roll setups (or IV-cheap candidates in buy mode).

The output is a **screening heuristic, not a mispricing or arbitrage
claim**. Vol smiles and skew are legitimate, the no-arbitrage
principle does not require the surface to be smooth, and IV+pp
deviations can reflect demand pressure, event risk, or stale prints
as easily as a tradeable signal. Phrase user-facing copy accordingly
— "mispriced", "overpriced", "underpriced", "anomaly" are out;
"IV-rich", "IV-cheap", "outlier", "stands above/below the surface"
are in. "Rich premium" / "cheap premium" are conventional trader
vernacular and remain fine.

## How it works

1. Fetch all expirations with DTE >= min_dte from Yahoo Finance
2. Annotate earnings events within each expiration window (elevated IV
   around earnings is expected, not a signal)
3. Fit an IV surface and score each option, via a three-stage
   **pluggable pipeline**:
   - **Filter** (`iv_filters.py`) — which options feed the regression.
     Defaults: OTM-only, spread ≤ 50% of mid, delta 0.10–0.95, and
     *short-dated* (≤ 60 DTE) earnings-spanning options excluded — long-
     dated contracts stay in the fit, since one earnings is a negligible
     share of their variance, and a guard keeps the filter from emptying
     the fit; an always-on sanity stage (IV noise floor/ceiling, DTE > 0)
     is prepended via `with_sanity`. Opt-in: min-OI and `fresh_quotes`
     (drop known-stale Yahoo quotes)
   - **Algorithm** (`iv_algorithms.py`) — `global_poly` (default) or
     `per_expiration`; produces `iv_fitted`. Both accept `weights`
     (`oi` / `inv_spread` / `vega`) and `robust` (`huber` / `tukey`
     IRLS so outliers can't drag the surface toward themselves).
     `global_poly` drops its m²·√T curvature term below 3 expirations
   - **Score** (`iv_scores.py`) — the ranking key `signal_score`;
     defaults to `raw_pp` (= IV excess), with z-score, relative,
     execution-cost composite, VRP, and historical-percentile options
4. Compute IV excess = actual IV − fitted IV (positive = IV-rich,
   sits above the fitted surface)
5. Display ranked table including delta, annualized yield, and OI

The Single Ticker tab exposes a **Global / Per-expiry** preset toggle
(the "Fit:" radio) plus an **Advanced surface fit** expander to mix the
three stages; the CLI mirrors this via `--preset {current,v2}` /
`--algorithm` / `--fit-weights` / `--robust` / `--score` (the CLI
preset names differ from the UI labels). The
`percentile` score persists scans to a gitignored SQLite store
(`options-scanner/cache/`) and is blank until history accumulates.

## Running the tool

Always run from the **repo root** using `uv run`:

```bash
# Both calls and puts (default)
uv run options-scanner/run_scanner.py AAPL

# Covered call selection only
uv run options-scanner/run_scanner.py AAPL --calls

# Cash-secured put selection only
uv run options-scanner/run_scanner.py AAPL --puts

# Roll an existing short call
uv run options-scanner/run_scanner.py AAPL --roll \
    --type call --strike 185 --expiration 2026-01-16

# Adjust filters
uv run options-scanner/run_scanner.py AAPL --calls \
    --min-dte 400 --min-oi 50 --top 20
```

Never use `python` directly — dependencies won't be resolved.
Run `uv sync` from repo root after any `pyproject.toml` change.

## Order entry: the Confirm → Place gate

Every screen that can send an order (Sell Put/Call dialog, tracked-trade
close, live-position close, roll) goes through `confirm_gate.py`. The
invariants, which any new order screen must keep:

- **Place** renders only when Confirm was pressed *on the values now on
  screen* and those values still validate.
- Editing the limit or the contract count **disarms** — back to Confirm.
  The confirm step attests to specific numbers, not to a general
  intention to trade.
- Confirm is disabled while armed; **Cancel** is the way back. The two
  buttons are never both live.
- Confirm stays **clickable while the inputs are invalid** — it just
  refuses to arm, and the error stays on screen. Never disable it for a
  bad value: Streamlit commits a number box only on blur, so a disabled
  button forces "click away, wait for it to re-enable, then click
  Confirm". Pass `validate=` to `confirm_gate.arm()` instead; the
  callback sees the values as of the click. Only blocks the user can't
  fix by editing (paper mode, market hours) disable the button.
- An emptied number box returns `None` from `st.number_input` — check
  `confirm_gate.valid_values()` before casting, or `int(None)` raises.
- **Never put `min_value`/`max_value` on an order-entry number input.**
  Streamlit refuses to *commit* an out-of-range entry: it shows its own
  "must be ≤ N" message and keeps serving the last valid value. Typing 5
  contracts against a 4-contract cover therefore left the app holding 1
  — valid, so the order built and Place armed for a size nobody typed.
  Leave the widget unbounded and validate in our code
  (`build_option_sell_order`, `build_roll_order`,
  `close_input_error`), which sees the real number and explains the
  rejection. Keep the cap in the label ("Contracts (Max 4)") as
  guidance. Scan-filter inputs may keep their bounds — they gate
  nothing.

Arm with the `on_click` callbacks (`arm` / `disarm`), never an inline
`if st.button(...)`: a callback runs before the rerun renders, so the
button states are consistent within a single frame.

Also: don't write a live default straight into a keyed input on every
rerun. Use `confirm_gate.reseed_on_change()`, which re-seeds only when
the *basis* changes — an unconditional write clobbers the number the
user is mid-correction, so the error describing it never renders.

**After placing**, every Place path must:

1. disarm the gate,
2. on success, queue the center banner (`st.session_state["_osc_toast"]`)
   **and** drop the stored result so it can't also render inline,
3. `st.rerun()` — a **full** rerun, not `scope="fragment"`: `run_app`
   renders the banner.

The rerun is not optional. Disarming only takes effect on the *next*
run, so without it the panel the click came from stays on screen with
Place still live (which is exactly what the paper close used to do).
Dialogs are the one exception: `st.rerun()` closes a dialog, so the Sell
and Roll dialogs rerun on success only and keep a failure visible inline.

## Settings: two config layers, kept disjoint

- `config.toml` — machine + secrets layer. Schwab credentials, the
  `paper` live-order gate, default provider. **Hand-edited only; the app
  never writes it.** `tomllib` is read-only, a TOML writer would drop
  the comments that document the file, and `config.py`'s lenient loader
  exists because this file gets hand-edited and breaks.
- `settings/settings.json` — preference layer, written by the ⚙️
  Settings dialog (`settings_store.py` + `settings_ui.py`). Nothing
  security- or safety-critical goes here, so a mis-click can't arm live
  trading. Read per rerun, so edits apply with no restart.

Never add a credential or the `paper` flag to the JSON layer, and never
put a preference in both files — disjoint keys mean there's no
precedence question.

**Hidden positions are display-only.** `position_filters` rules are
applied where the Close/Roll tabs *render* (never inside
`positions_cache` or `trade_actions`), because coverage and sizing must
keep seeing every leg — hiding a short call must not free its shares for
a second covered call. Filtering after the cached read also means a
settings change lands on the next rerun instead of waiting out the 60s
TTL.

## Output columns

| Column  | Meaning                                            |
|---------|----------------------------------------------------|
| Top     | Web UI only. Rank within the top-N list per type (1 = strongest signal); blank if not in top N |
| Strike  | Option strike price                                |
| Expiration | Expiration date                                 |
| DTE     | Days to expiration                                 |
| Bid/Ask/Mid | Market prices                                 |
| IV%     | Implied volatility (annualized %)                  |
| IV+pp   | IV excess above surface fit (positive = rich)      |
| _score_ | Active-score column (z-score, VRP, etc.) shown next to IV+pp when a non-default score drives ranking; header is the score's label |
| Delta   | Black-Scholes delta (call: 0–1, put: −1–0)         |
| Ann%    | Annualized yield: calls vs. spot, puts vs. strike  |
| OI      | Open interest                                      |
| Vol     | Web UI only. Today's trading volume (short-term liquidity) |
| NetCr   | Roll mode only: net credit received if rolled here |

## LT capital gains note

Selling an option and holding the short position for 366+ days
qualifies the premium for long-term capital gains rates. The tool
prints the earliest qualifying close date for a position opened today.

## YouTube production materials (sibling private repo)

Scripts, slide HTML, and image assets for the YouTube tutorials
about this tool live in a separate private repo at
`../stockpile-private/options-scanner/youtube/` (sibling directory
to this one). They are active working material and Claude should
treat them as in-scope when asked.

Layout: one subfolder per episode, under
`../stockpile-private/options-scanner/youtube/`.

- `ep1/` — first episode: full tool walkthrough. Script at
  `ep1/script.md`, slide HTMLs (`*-slide.html`), and `ep1/images/`
  with thumbnails and screenshots.
- `ep2/` — second episode in active drafting. Focused on the
  Schwab data source plus features added since ep1 (GEX chart,
  index tickers, stockpile CSV, polish). Script at `ep2/script.md`.
- Future episodes follow the same `epN/` pattern.

When the user asks about "the script", "the episode", or "the
YouTube video" without naming one, assume the most recent episode
folder. Read the existing script before making edits — episodes
follow a consistent template (slide cues in `[NN ...]`, on-camera
directions in parens, content blocks separated by `---`).
