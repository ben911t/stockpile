# How to place your first cash-secured put

A step-by-step guide to selling your first **cash-secured put (CSP)**
through the scanner's assisted put-selling flow. You'll set a limit,
preview the order, and send it to Schwab with a two-step confirm — all
without leaving the app.

> **Real money, no Schwab sandbox.** When `paper = false`, this sends a
> live order to your linked Schwab account. Sending always takes **two
> clicks**: **Confirm Trade** only arms the order — nothing is
> transmitted until you click **Place Trade** beside it. Start with
> **1 contract** on a **liquid** name you'd genuinely be fine owning.

Selling a **covered call** works the same way — pick Calls instead of
Puts on the Watchlist scan and the dialog becomes **Sell Call**, sized
against the shares you hold rather than cash. And once you hold a leg,
the **Positions** tab is where you close, roll, or unwind it; this guide
covers opening and closing a trade the scanner placed.

## What a cash-secured put is

You **sell** a put and set aside enough cash to buy 100 shares per
contract at the strike. You keep the premium (the credit). If the stock
closes below your strike at expiration you may be **assigned** — obligated
to buy those 100 shares at the strike. So only sell puts on stocks you'd
be happy to own, at a strike you'd be happy to pay.

## Before you start

- [ ] **Both Schwab API products enabled** on your developer app —
      **Market Data Production** (the quotes and option chains that fill
      the scan and the Sell Put dialog) **and Accounts and Trading
      Production** (reads your balances to size the put and places/closes
      the order). With only Market Data the scan works but the dialog
      can't size or send a trade. If you just added the product, re-run
      `schwab_auth.py` so the new token carries the access. See
      `SCHWAB_DATA_SOURCE.md`.
- [ ] **Schwab options approval** for cash-secured puts on the account
      you'll trade. Check at your broker first — the order is rejected
      without it.
- [ ] **Schwab linked and authenticated.** The 7-day token must be
      fresh; if quotes are empty or you see "Couldn't resolve the target
      account," re-run `schwab_auth.py`.
- [ ] **`paper = false`** in `options-scanner/config.toml` — `paper =
      true` only *simulates* (Confirm records a fake trade and sends
      nothing live). Keep it `true` if you'd rather rehearse first (see
      "Rehearse risk-free" below). No restart needed: the file is re-read
      on every rerun, so save it and click anything in the app
      (`config.toml` isn't a watched source file, so saving alone won't
      refresh the page). The title-bar badge flips **📝 PAPER** →
      **🔴 LIVE** when it takes — check it before you trade.
- [ ] **Schwab** selected as the data source. The toggle shows the token
      countdown; with `paper = false` the Sell Put dialog title shows
      **🔴 LIVE** too.
- [ ] **Market open** for live orders — equity options trade 9:30–16:00
      ET, Mon–Fri. **Confirm Trade** is disabled (with the reason)
      outside those hours.
- [ ] Keep **Schwab.com / thinkorswim** open in another tab to verify
      fills and manage orders directly if you ever need to.

## Key idea: which way the limit price leans

You're **selling** a put, so the limit is the **minimum credit per
share** you'll accept:

- **Higher limit credit → LESS likely to fill** (you're asking for more
  premium than buyers will pay). Set it well above the ask to sit
  unfilled.
- **Lower limit credit → fills FAST.** At or just below the current
  **bid** it typically fills right away.

The dialog suggests a limit at the **mid** when the market is liquid; you
can accept it or set your own.

## Rehearse risk-free first (optional, recommended)

If this is genuinely your first time, prove out the mechanics before you
commit real premium. Two ways:

- **Paper mode** — set `paper = true` and run the whole flow. Confirm
  records a simulated trade (no live order); the Trades tab shows it
  tagged **PAPER**. Switch back to `paper = false` when you're ready for
  real, and watch the title-bar badge to confirm the switch took.
- **A live order priced not to fill** — with `paper = false`, place
  **1 contract** with a limit credit set **well above the ask** (e.g.
  ~2–3× the mid, or a round number clearly above the ask — high enough
  no one fills it, not so absurd it looks like a typo). This exercises
  the full live round-trip (auth → account → place → status → cancel)
  with no position, then you cancel it (see below). It's the most
  realistic dry run.

## Place your first cash-secured put

1. **Watchlist** tab → enter a list of tickers → Option Type Puts
   **Sell** mode → **Schwab**  source → **Scan Watchlist**.
2. In the leaderboard's **Puts** table, **select a put row** to open the
   **Sell Put** dialog. Its title reads
   `🔍 Sell Put — TICKER $spot · 🔴 LIVE` (📝 PAPER in paper mode).
3. Review the snapshot: the left tables show the contract terms and
   bid/ask/mid; any thin-market warnings appear as ⚠ notes.
4. Set **Contracts = 1** to start.
5. Set the **Limit price**. The suggested mid usually fills, just not
   instantly; at or just below the **bid** fills promptly. (For the
   risk-free dry run, set it well *above* the ask instead.)
6. Check the **order preview** line — it shows the credit you'd collect
   and the cash collateral held — and the **Cash for puts** caption,
   which tells you how many contracts your account can secure.
7. Click **Confirm Trade**. A yellow review panel opens showing the
   order, account, credit, collateral vs. your cash, and **🔴 LIVE**.
8. Click **Place Trade** (the green button; red **Cancel** backs out).
   The dialog closes and a green confirmation banner with the order id
   appears in the center — it fades after ~60s, or click **×**.

## Track it on the Trades tab

The **Trades** tab updates automatically after placing.

1. Expand your trade (labeled `TICKER $STRIKE PUT — exp · 1x · status`).
2. The **Broker order** status line tells you the real state:
   - **⏳ Broker order WORKING** — accepted but not yet filled.
   - **✅ Broker order FILLED (1 of 1 contracts)** — you now hold the
     short put, with cash collateral set aside.
   - The cards show **Credit received**, **Cost to close**, and live
     **Unrealized P/L**. (Status is cached ~15s — re-interact to
     refresh.)
3. Always cross-check the fill at your broker.

> The tracker marks a trade **open** when Schwab **accepts** the order,
> not when it fills — which is why the **Broker order** line (WORKING vs.
> FILLED) is what matters. Verify fills at your broker.

## Cancel an unfilled order

While the order still shows **⏳ WORKING** (this is how you back out of
the risk-free dry run):

1. Expand the trade → click **Cancel working order**.
2. The status flips to **CANCELED** — no position changes. Confirm at
   your broker.

(**Remove from Tracker** only discards the local record; it does **not**
cancel a live order. Use **Cancel working order** for that.)

## Close the position early (optional)

Once the order shows **✅ Broker order FILLED**, the close controls
appear inline in the expanded trade:

1. Set the **Close limit** (the debit per share you'll pay to buy it
   back — it defaults to the current mid), and how many contracts to
   close if you don't want all of them.
2. Click **Confirm Closing Trade** — the same two-step gate as opening.
   It arms the order against the numbers on screen (editing either one
   disarms it) and reveals **Place Closing Trade**, which is what
   actually sends the buy-to-close. Red **Cancel** backs out.
3. The status flips to **closed**; verify the buy-to-close at your
   broker.

(Or just let it ride to expiration if that was the plan.)

## Safety reminders

- **Two clicks to send:** **Confirm Trade** opens the review panel;
  **Place Trade** in that panel actually transmits. Red **Cancel** backs
  out.
- **Cancel working order** only works pre-fill (WORKING). After a fill,
  use the **close** flow (a separate buy-to-close order) or manage at
  the broker.
- **This is a real, cash-secured position** with assignment risk if it
  goes in-the-money. Keep it small and on a name you'd accept owning.
- To go back to simulation, set `paper = true` — no restart; the badge
  in the title bar tells you which mode is live.

## If something goes wrong

- **Confirm Trade is disabled** → market is closed, the order is invalid
  (check the preview error above the button), or Schwab hours couldn't
  be confirmed.
- **"Order rejected: …"** → Schwab declined it; the message carries the
  reason (e.g. buying power, options-approval level). Fix and retry.
- **No WORKING status or Cancel button on the Trades tab after placing**
  → the order id wasn't captured from Schwab's response. The order may
  still be live — manage it at your broker, and tell me so I can harden
  it.
- **"Couldn't resolve the target account"** → re-run `schwab_auth.py`,
  or check the account is still linked
  (`uv run options-scanner/show_accounts.py`).
- **Status shows unavailable** → transient; verify directly at the
  broker.
- Anything unexpected with a live order → manage it at Schwab.com /
  thinkorswim; the tool never cancels or closes without your click.
