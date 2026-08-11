# Assisted put-selling — implementation plan

Feature behind the ep9 episode ("the scanner can place a trade"). This
doc is the build/design plan; the YouTube concept lives in the sibling
private repo at
`../stockpile-private/options-scanner/youtube/ep9-place-put-trades/script.md`.

> **Status: DELIVERED and since superseded — kept as a design record.**
> Every phase below shipped; put-selling went live 2026-06-17. The work
> that followed widened it past this plan's original scope: covered-call
> selling (**Sell Call**), and a **Positions** tab that closes, rolls,
> and unwinds any live Schwab leg — so guardrail 1 below ("sell puts
> only, no calls, no multi-leg") no longer describes the code. Read this
> for *why* the gates are shaped the way they are; read `README.md`,
> `SCHWAB_DATA_SOURCE.md`, and the "Order entry" section of `CLAUDE.md`
> for what the app does today.

## Hard guardrails (enforce in code, not just UI)

1. **Sell puts only.** Single-leg, sell-to-open, cash-secured puts. No
   buys, no calls, no multi-leg — reject anything else before it can
   reach the order builder, even if the Schwab API would accept it.
   *(Superseded: covered calls, rolls, and unwinds all ship now. What
   survived is the principle — each order type gets its own validated
   builder, and nothing reaches Schwab that a builder didn't construct.)*
2. **Human approval always.** The scanner recommends; it never fires.
   Every order stops at a "Place Trade" button the user clicks.
   *(Still true, and now formalized in `confirm_gate.py`: Confirm arms,
   Place sends, and any edit disarms.)*
3. **Schwab only.** Placing orders needs Schwab trading scope beyond the
   read-only quotes Yahoo/Schwab give us today. The whole flow is
   Schwab-gated. *(Still true.)*

## Scope today (what's built — DONE)

- Per-row **investigate** control on the watchlist **Puts leaderboard**,
  gated to watchlist + Sell + Schwab (`allow_investigate` in
  `tabs/portfolio.py` → `display/leaderboard.py`).
  - Implemented as single-row selection on the existing `st.dataframe`
    (mirrors the spreads-tab pattern), so the per-row control reuses the
    styled table rather than rebuilding it.
- Selecting a put row opens `_investigate_put_dialog` (`@st.dialog`):
  shows the contract snapshot (bid/ask/mid/last, IV, volume, open
  interest).
- **Fill-quality + limit price (`trade_actions`).** `assess_fill` judges
  executability from spread (% of mid OR small absolute) + open-interest
  floor, volume as a soft note. Liquid → an **editable limit** (mid
  rounded to tick) above the Place Trade button with credit/collateral.
  Illiquid → **warn why**, but still offer an editable limit defaulted to
  an **IV-aligned model price** (`model_limit` = Black-Scholes put at the
  contract's own IV, carrying the IV+pp edge) so the user can place their
  own limit anyway. `Place Trade` stays disabled (placement not built).
- **2D IV chart in the dialog** — embeds `show_iv_chart` for the
  ticker's puts so you can see how rich this one is vs the chain.
- **Sizing** — Schwab buying capacity (`fetch_account_capacity`, cached
  60s, read-only) + a **contracts-to-sell** input capped at
  `puts_affordable`, and a validated **order preview**
  (`build_put_sell_order`) showing the exact order, credit, collateral.
- **Trades tab + store** (`tabs/trades.py`, `trades_store.py`) — lists
  recorded put-sells with live **cost-to-close** + **unrealized P/L**
  (Schwab re-quote) and a **closing-order preview**.

The ONE thing deliberately left off *at the time this was written*:
**actual order submission** — no `client.place_order` call existed
anywhere, and the Place buttons were disabled. Everything else was built
around that gate. **That gate has since been opened** — see Phase 2.5
below, which was implemented as sketched; the app places live orders
today.

## Phase 1 — Fill-quality check (read-only, no order)

The "is this *executable* well right now?" judgment — distinct from the
IV+pp ranking, which already answers "is this a good trade?".

- **DONE — module** `options_scanner/trade_actions.py`:
  - `assess_fill(...) -> FillAssessment` from bid, ask, mid, volume, OI.
  - Signals: bid/ask spread (% of mid **or** small absolute), OI floor;
    volume is a soft note (it's 0 for everything while the market is
    closed, so it can't gate). Output: `liquid` + `reasons` +
    `suggested_limit` + `notes`. Still TODO: last-vs-mid sanity.
  - Limit-price policy: liquid → **mid**, rounded to tick
    (`round_to_tick`); illiquid → **IV-aligned** BS price (`model_limit`)
    at the contract's own IV. TODO: one-tick-inside / "aggressive vs.
    patient" choice (open question); authoritative tick rules from Schwab.
- **DONE — dialog.** Editable limit `st.number_input` (override) with
  credit/collateral, directly above Place Trade. Liquid anchors on the
  mid; illiquid warns but still defaults the input to the IV-aligned
  model price so a trade can be priced and placed regardless.
- **DONE — 2D IV chart in the dialog.** Embeds `show_iv_chart` for the
  ticker's puts (full chain threaded from `results` through
  `render_leaderboard` → `_render_table`), so the user sees how IV-rich
  this put is vs the chain. TODO: highlight the selected strike (the
  chart shows all puts; the selected one isn't called out yet).
- **DONE — `requote_put`** (read-only, reuses
  `schwab_live.fetch_option_chain_schwab`). Used for cost-to-close in the
  Trades tab. TODO: also re-quote inside the dialog before assessing and
  show a fetch time (dialog still uses the scan snapshot).
- **DONE — tests** (`tests/test_trade_actions.py`): tick rounding,
  assess_fill (liquid/wide+thin/cheap-rescue/one-sided), model_limit,
  puts_affordable, build_put_sell_order + validation/capacity guard.

## Phase 2 — Place the order (the gate)

- **Schwab trading scope.** Confirm what the Schwab API requires to
  place an options order vs. read quotes: OAuth scope, account
  trading-enabled, the order-entry endpoint. Builds on the existing
  `schwab_auth.py` flow (7-day token TTL). Document the re-auth /
  permission steps the way ep2 did for quotes.
- **DONE (config) — paper flag.** `get_schwab_config` exposes
  `paper` (default **True**) so live orders are an explicit opt-in. TODO:
  confirm Schwab's paper/sandbox order path exists; add a paper-mode
  badge once placement is wired.
- **DONE — put-selling capacity.** `fetch_account_capacity` reads
  available cash / buying power (read-only, `get_account_numbers` +
  `get_account`), cached 60s in the dialog. `puts_affordable` =
  capacity ÷ (strike × 100) caps the quantity input.
- **DONE — order builder + LIVE placement.** `build_put_sell_order` returns
  a validated `PutSellOrder` (guardrail #1: qty ≥ 1, limit > 0, strike > 0,
  collateral ≤ capacity); `place_put_sell_order` maps it to schwab-py
  `option_sell_to_open_limit` → `client.place_order` (now WIRED — see
  Phase 2.5).
- **DONE — approval gate.** Contracts-to-sell (capped by capacity), capacity
  metric, live order preview, **two-step confirm**, paper/live badge, and
  recording to the store — all built.
- **DONE — Schwab trading scope.** "Accounts and Trading Production" access
  confirmed live on the linked account.

## Phase 2.5 — Placement (2026-06-16: BUILT)

**STATUS: order placement + closing are now wired** (uncommitted working
tree, 2026-06-16). The sketch below was implemented as-is. Opening:
`trade_actions.place_put_sell_order` (`OptionSymbol` →
`option_sell_to_open_limit` → `client.place_order`). Closing:
`place_put_close_order` (`option_buy_to_close_limit`). Both behind: a
**market-hours gate**
(`market_is_open`), a **two-step inline confirm**, and the **`paper` flag**
(live only when `paper=false`); they target the account from
`resolve_account_hash` and log to `trades_store`. See the Sell Put dialog
(`leaderboard.py`) and the Trades tab (`tabs/trades.py`).

**Access confirmed.** The Schwab app now has **"Accounts and Trading
Production"** in *Ready For Use* status, with a MARGIN, options-enabled
account linked. `get_account_numbers` / `get_account` return real data;
before activation they returned HTTP 401 "Client not authorized" while
market-data quotes still worked. `options-scanner/show_accounts.py`
(read-only) dumps every linked account's balances and is the quick
"is trading live yet?" check.

**Capacity now correct (Phase 2 item updated).** `fetch_account_capacity`
derives `amount` from the cash-secured figure — `cashAvailableForTrading`
(cash acct) → `availableFundsNonMarginableTrade` (margin acct) →
`availableFunds`, **never** `buyingPower`. On a margin account that's the
non-marginable cash, not the (larger) margin buying power. The full
`currentBalances` is surfaced read-only in the dialog's collapsed
**Account info** panel.

Real money — there is no Schwab paper/sandbox for schwab-py (it talks only
to production), so a `paper=false` `place_order` hits the live account. The
`paper` flag + market gate + confirm step are the guardrails. The detailed
design that was implemented follows.

1. **Resolve the account (don't blind-index `nums[0]`).** Add `account_id`
   (the account's last-4) to `[schwab]` config; match it against
   `get_account_numbers()` to pick the hash. Refuse on zero/multiple matches.
   Today there's one linked account so `nums[0]` happens to be the right one,
   but placement must be explicit about *which* account it trades.
2. **Build the order.** `PutSellOrder` → OSI symbol via
   `schwab.orders.options.OptionSymbol(underlying, "YYYY-MM-DD", "P",
   strike).build()` → `option_sell_to_open_limit(symbol, qty, price)`.
   Re-assert guardrail #1 at build time: single-leg, SELL_TO_OPEN, PUT,
   qty ≥ 1, and `collateral ≤ capacity.amount` (keeps it cash-secured even
   though a margin account would *let* you sell more on margin).
3. **Confirm gate.** After Place Trade, a second explicit confirm (the order
   JSON + credit + collateral + a paper/live badge from config `paper`).
   Nothing submits on the first click.
4. **Submit.** `client.place_order(account_hash, spec)`; a 201 carries the
   order id in the `Location` header — read it with
   `schwab.utils.Utils(client, account_hash).extract_order_id(resp)`. On a
   non-201, surface Schwab's `{"errors":[...]}` payload (same shape as the
   401 we hit).
5. **Record + track.** `trades_store.add(...)` with `order_id`, status
   `WORKING`, `paper` flag; the Trades tab polls `client.get_order(order_id,
   account_hash)` and maps WORKING/FILLED/REJECTED/CANCELED.
6. **Closing orders** mirror this: `option_buy_to_close_limit` →
   `build_put_close_order` → same confirm + submit + store status flip.

**Safety knobs.** `paper=True` (default) should mean "build + validate but
never call `place_order`" until a real Schwab paper path is confirmed (none
exists today). Disable the button post-click and dedupe against an existing
open order in the store for idempotency.

**Touch points to add when enabling:** `trade_actions` (account resolver +
`place_put_sell_order` wrapper), the dialog confirm gate + submit, `config`
(`account_id`), `trades_store` status polling. Keep all of it behind the
"don't enable yet" flag.

## Phase 3 — Trade tracker tab (P/L + closing)

**DONE.** Top-level "Trades" tab (`tabs/trades.py`, wired into `run_app.py`
after Watchlist). Lists placed put-sells with live P/L + cost-to-close, and
closes them (see closing flow below). Populated once you place a trade.

- **DONE — store** (`trades_store.py`): single gitignored JSON
  (`options-scanner/trades/`) with `load/add/update/remove`; records
  ticker, strike, expiration, qty, credit, status, paper, order_id,
  opened_at, close_cost, closed_at. TODO: optional reconcile against a
  live Schwab positions/orders pull.
- **DONE — per-position view:** live **cost-to-close** (`requote_put`
  mid, cached 30s) and **unrealized P/L** (credit − close cost) × 100 ×
  qty, plus status. TODO: DTE + LT-cap-gains qualifying date.
- **DONE — closing flow (WIRED):** suggested close limit (re-quote mid →
  tick), editable, then **Place Closing Trade** →
  `place_put_close_order` (`option_buy_to_close_limit` → `place_order`) under
  the same market-hours gate + two-step confirm + `paper` flag. A live
  position closes with a real order only when `paper=false`; a paper trade
  closes in the tracker only. On success the store flips to `closed` with
  `close_cost`/`closed_at`.
- **DONE — verify-at-broker caveat** shown throughout.

## Schwab API specifics to confirm

- Exact OAuth scope / account flag for order entry.
- Order-entry endpoint + payload schema for single-leg options.
- Paper/sandbox availability and how to target it.
- Account hash retrieval (already used for quotes? confirm).
- Account balances endpoint — available cash and margin available — to
  compute how much we can sell (capacity = cash / margin ÷ collateral)
  and to validate qty before sending.
- Tick-size / price-increment rules for the limit price.

## Open questions (from the concept)

- Recommendation surface — dialog (current stub) vs. inline expander vs.
  side panel. Stub uses a dialog; revisit if it feels cramped.
- Tracker persistence — local JSON, read-back from Schwab, or hybrid.
- Investigate per-row-on-click (current) vs. bulk auto-run across the
  whole leaderboard. Per-row keeps it deliberate.
- Limit-price opinionation — single suggested price vs. an
  aggressive/patient range.

## Touch points (current code)

- `options_scanner/display/leaderboard.py` — investigate control +
  dialog stub (Phase 1 UI grows here / into `trade_actions.py`).
- `options_scanner/tabs/portfolio.py` — `allow_investigate` gating
  (`_render_scan_tab`, watchlist branch).
- `stocks_shared/schwab_live.py` — Schwab client, quotes, chain fetch
  (extend for order entry / single-contract re-quote).
- `options_scanner/config.py` — `[schwab]` config (add paper flag).
- `run_app.py` — add the "Trades" tab (Phase 3, confirm first).
