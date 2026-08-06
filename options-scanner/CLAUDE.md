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
   - **Algorithm** (`iv_algorithms.py`) — `global_poly` (default),
     `per_expiration`, or `earnings_segmented`; produces `iv_fitted`
     (`svi` is registered as a seam but not implemented). All accept `weights`
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
close, live-position close, roll, unwind) goes through `confirm_gate.py`.
The invariants, which any new order screen must keep:

- **Place** renders only when Confirm was pressed *on the values now on
  screen* and those values still validate.
- Editing the limit or the contract count **disarms** — back to Confirm.
  The confirm step attests to specific numbers, not to a general
  intention to trade. Switching a selected position between **Close**
  **Roll** and **Unwind** on the Positions tab disarms for the same
  reason: those are different orders, so an arm made against one must
  not survive into the other.
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

## The tab bar's blue tint

`ui_theme.mark_broker_tabs(TAB_NAMES, BROKER_TABS)` fills the chips for
tabs that read a live broker account — **Positions** and **Trades** — in
Schwab blue, so the dependency is visible before you click into an empty
table. Add a tab that needs Schwab and it belongs in `BROKER_TABS`
(`run_app.py`); indices are derived from `TAB_NAMES` at call time, so
reordering can't shift the fill onto a neighbour (the pair has already
been swapped once). The colors are the four constants above the
function.

**The selected chip is opaque, and that's load-bearing.** Streamlit
draws the selected label in white — it normally sits on a filled dark
background — and an `!important` background override leaves that white
text in place. A *translucent* fill then resolves against whatever
canvas is behind it: `rgba(0,160,223,0.62)` landed on `#5EC2EA` under
the light theme, putting white text at **2.0:1** (unreadable), while
the same value landed on `#056B9A` under the dark theme at a fine
5.8:1. `_BROKER_TAB_ACTIVE_BG` is therefore a solid `#0077A8` with an
explicit `_BROKER_TAB_ACTIVE_FG` — one opaque pair renders identically
on both themes and clears 4.5:1. A test pins that ratio. The *idle*
chip stays translucent on purpose: it keeps the theme's own label
color, which adapts with the canvas.

Set the label color on the child elements too (`button p`, `button
span`), not just the button — Streamlit wraps the label, so a color on
the button alone never reaches the text. Same trap as the roll dialog's
red Cancel button.

**Watch the selectors.** The first version scoped to
`[data-testid="stSegmentedControl"]` — a test id Streamlit doesn't
define — and silently matched nothing through two rounds of "why is
there no tint". The real ids are `stButtonGroup` for the row and
`stBaseButton-segmented_control` / `…_controlActive` for the chips.
Scoping now runs through `st-key-osc_tabbar`, which comes from our own
`st.container(key=...)` and can't be renamed by a version bump. A
Markdown `:blue-background[…]` label via `format_func` also works and is
a firmer contract, but it tints only the text run, not the chip.

Note that **several existing rules still target that dead test id** —
the tab-bar underline styling in `inject_theme` and the data-source
pill's border in `styles.css`. They have no effect today; see the
`stButtonGroup` note above before rewriting them, and expect the tab bar
to change appearance when they start applying.

Live Charts is deliberately **not** tinted: its panes take Yahoo and
Hyperliquid too, so Schwab is one option there rather than a
requirement. Trades is tinted despite still working without a broker
(it lists locally-tracked trades and closes paper ones) because
everything broker-side on it — cost-to-close, P/L, order status, closing
a live position — is Schwab-gated.

## Stock coverage: the Positions tab's second table

`trade_actions.classify_coverage(shares, short_calls)` is pure arithmetic
on two numbers (so it tests without a broker) and owns the four states:
`over_written`, `uncovered`, `partial`, `covered`. `equity_positions`
pairs shares with the calls written on them from **one** positions fetch
— two reads of a moving account could disagree about what's covered.

Two rules worth keeping straight:

- **"Covered" means no writable lot remains**, not that every share is
  written. 450 shares against 4 calls leaves a 50-share stub, and there
  is nothing you can do with 50 shares.
- **`over_written` exists because `calls_coverable` clamps at 0.** With
  150 shares and 2 calls it returns 0, identical to fully covered — so
  a naked call would render as safe. Never collapse the two.

Row shades follow `MONEYNESS_BANDS`' rules: translucent rgba (they
overlay the cell in both themes) and **no green**, since these tables
color P/L green and a green row would read as "profitable".

The stocks table calls `filter_hidden` but must **never** call
`render_hidden_notice` — that keys its checkbox on the scope alone, so a
second call on the same tab is a duplicate-key crash. The option table
renders it; the toggle is session-wide and governs both.

**Only rows with a free 100-lot are selectable**, and that means two
tables: Streamlit's dataframe selection is all-or-nothing per table, so
a checkbox on some rows and not others can only be done by splitting
them (`leaderboard._render_calls_by_coverage` solves the same problem
the same way). The two halves share one column config and one styler so
they can't drift into looking like different data, and they need
distinct widget keys. The selection indexes into the **writable subset**
— indexing the full row list would build a call for whichever position
sat at the same index.

Selecting a row with a free lot opens the covered-call builder, which
hands off through the leaderboard's own `contract_from_row` and
`open_investigate`. Build the contract dict by hand and it will drift
from what the dialog expects (its `:,d` formats need real ints, and
`open_investigate` also clears stale confirm/result state) — the point
of sharing them is that a call sold from Positions is identical to one
sold from a watchlist scan.

## Leg snapshots: one contract, one description

`format.leg_rows` + `format.kv_table_html` render the leg table on all
three Positions-tab actions (close, roll, unwind) and are the reason a
contract can't read three different ways depending on the verb you
picked. They live in `format.py` — a leaf module — so `tabs/trades`
(close) and `tabs/rolls` (roll, unwind) can both import them; the
trades → rolls direction only exists as a lazy import for the roll
monitor, and adding a module-level one would cycle.

The leg tables pass `pairs=2`, which lays two label/value pairs per line
and **fills column-major** — first half of the rows down the left pair,
the rest down the right. That halves the height (a 10-row leg is 5
lines) and, because `leg_rows` emits prices before analytics, lands
Spot/Bid/Ask/Mid/Last in one column and IV%/Delta/OI/Vol/IV+pp in the
other. Row-major would interleave the two. `pairs` defaults to 1, which
is what the Sell dialog's terms/prices tables still use.

Everything on a leg rides along on the re-quote except **IV+pp**, which
isn't a quote field: it's the leg's IV against a fitted surface, so it
needs a chain fetch (`trades.leg_iv_pp`, ~2s measured, `fetch_and_enrich`
caches 5 min; the fit itself is ~4ms). It's fetched automatically on all
three actions — they only render once a row is selected, so nothing is
paid on tab load. `leg_iv_pp` never raises: IV+pp is context, not a
precondition for an order, so a throttled chain drops that one row and
the panel still prices and places.

The color scale is a **buyback** reading — below the surface is green
(cheap to close), above it red — the opposite of the sell-side scale,
because every screen sharing these rows is buying a leg back.

## Manual test plan

`TRADING_TEST_PLAN.html` (tracked, open it in a browser) is the
checklist for the trading paths — sell, close, roll, and the
cross-cutting safety gates. It splits into a **Paper pass** (rows marked
Paper/Either, safe any time) and a **Live pass** (real orders, market
hours). Progress is stored per row id in browser localStorage, so
editing a row's text keeps existing checkmarks; adding or renaming a row
id resets that one. Keep it in step with the UI — a behavior change that
makes a row unrunnable is a bug in one of the two.

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
applied where the Positions tab *renders* (never inside
`positions_cache` or `trade_actions`), because coverage and sizing must
keep seeing every leg — hiding a short call must not free its shares for
a second covered call. Filtering after the cached read also means a
settings change lands on the next rerun instead of waiting out the 60s
TTL.

**Hiding is per symbol, and covers stock too.** `_held_positions()`
merges `option_positions` with `stock_positions` so a name you hold only
shares of is pickable at all — it wasn't, when the dialog enumerated
option legs. Three things that follow:

- Stock rows are normalized to carry `underlying` (`equity_positions`
  calls it `ticker`), because that's the field `pf.matches` keys on.
- They must reach `split_rules_for_ui` as well as the screen. It decides
  tick-vs-carry from the symbols it's given, so omitting them exiles an
  existing shares-only rule to *Other rules — matches nothing you hold*.
- They must **never** reach `pf.leg_label`, which builds "TICKER
  expiration $strike RIGHT" and renders a share row as `PLNH ? — ?`.
  `_is_stock()` is the discriminator.

The stocks table names its hidden symbols in its own caption rather than
pointing at the option table's *Hidden positions* expander — hide a
symbol you hold no options on and that expander never renders.

**Masked balances are display-only too**, and for the same reason:
`settings_ui.mask_money()` is a formatter over already-rendered text, so
sizing, coverage and affordability checks never see the mask. A test
pins that `trade_actions` and `position_filters` don't mention it. The
⚙️ tick (`mask_balances`) is the persistent default; the 👁 button beside
each masked figure overrides it **for the session only**
(`_osc_reveal_balances`), the same shape as the hidden-position "show
these anyway" toggle — revealing a number to read it must not quietly
turn the preference off.

Masking covers *account* balances only — the buying-power line above the
Positions table and on each of the four order screens (tracked close,
live close, roll, unwind), plus the Sell dialog's account figures. Order
prices, collateral, position values and P/L stay visible: they say what a
trade costs, not what you hold.

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

Folders are named `epN-slug` (e.g. `ep1-scanner-intro`,
`ep9-place-put-trades`), each holding `script.md`, slide HTMLs
(`*-slide.html`), and an `images/` directory of thumbnails and
screenshots. Episodes to date: `ep1-scanner-intro` (full tool
walkthrough), `ep2-schwab`, `ep3-covered-calls`, `ep4-cli-agent`,
`ep5-puts`, `ep7-3d-surface`, `ep8-watchlists`,
`ep9-place-put-trades`, and `ep10-trading-assistant` — the most
recently edited, so the one likely in active drafting. Unnumbered
`future-*` folders (e.g. `future-rolls`, `future-wheel`) park concepts
that haven't been scheduled.

When code on this branch changes something an in-flight script shows,
the script needs updating too — see "Keeping the two repos in sync" in
the root `CLAUDE.md`.

When the user asks about "the script", "the episode", or "the
YouTube video" without naming one, assume the most recent episode
folder. Read the existing script before making edits — episodes
follow a consistent template (slide cues in `[NN ...]`, on-camera
directions in parens, content blocks separated by `---`).
