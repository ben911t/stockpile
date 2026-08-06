"""Assisted put-selling logic — fill-quality assessment + limit pricing.

Pure helpers behind the watchlist "investigate put-sell" dialog (see
``options-scanner/assisted-put-selling-implementation-plan.md``). Order
*placement* is NOT here yet — these only judge whether a cash-secured put
looks executable at favorable terms and suggest a limit price. Kept free of
Streamlit so they're unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# US Eastern — options print/quote timestamps are shown in market time, so a
# stale "Last" reads naturally against the 9:30–16:00 ET session.
_ET = ZoneInfo("America/New_York")


def fmt_last_trade_et(epoch_ms) -> str | None:
    """Format a last-trade timestamp (epoch **milliseconds**, from Schwab's
    ``tradeTimeInLong`` or a converted Yahoo ``lastTradeDate``) as
    ``MM/DD HH:MM`` in US Eastern (24-hour). Returns None for absent/zero/NaN or
    an unparseable value — so a missing print simply shows no time. Annotates the
    ``Last`` cell in the pre-confirm detail tables so a stale print is obvious."""
    try:
        ms = float(epoch_ms)
    except (TypeError, ValueError):
        return None
    if ms != ms or ms <= 0:  # NaN, None-coerced, or 0 = "no print"
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(_ET)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%m/%d %H:%M")

# Liquidity thresholds — a deliberately conservative first guess at "would a
# limit order here have a good chance of filling at favorable terms?". These
# are about *executability*, distinct from the IV+pp ranking (which already
# judged whether it's a good trade). Tune as real fills come in.
MIN_OI = 50            # open-interest floor
MAX_SPREAD_PCT = 0.15  # bid/ask spread as a fraction of mid
MAX_SPREAD_ABS = 0.10  # absolute spread tolerance (rescues cheap contracts
                       # whose % spread is high but whose dollar spread is tiny)

# Matches options_scanner.chain._RISK_FREE_RATE so the model limit is priced
# on the same footing as the greeks the scan already showed.
RISK_FREE_RATE = 0.045


@dataclass
class FillAssessment:
    """Verdict on one contract's executability.

    `suggested_limit` is the mid rounded to tick — set whenever there's a
    two-sided market, None only when bid/ask are missing. `liquid` says
    whether to *trust* a fill there; `reasons` says why not when False.
    `notes` are soft caveats shown either way (e.g. zero volume).
    """

    liquid: bool
    suggested_limit: float | None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def tick_for(price: float) -> float:
    """Conventional option price increment: $0.05 at/above $3, else $0.01."""
    return 0.05 if price >= 3.0 else 0.01


def round_to_tick(price: float) -> float:
    """Round to the conventional option tick (a suggestion only).

    The authoritative increment rules come from Schwab when order placement
    is built; this is the penny-pilot split good enough to propose a limit.
    """
    tick = tick_for(price)
    return round(round(price / tick) * tick, 2)


def ceil_to_tick(price: float) -> float:
    """Round UP to the conventional option tick.

    Used for buy-to-close limits, where rounding the mid up to the next tick
    biases the proposed limit toward a fill rather than away from one (e.g. a
    3.92 mid → 3.95). A tiny epsilon keeps a price already sitting on a tick
    from jumping a full tick on float-division noise (3.95 stays 3.95).
    """
    tick = tick_for(price)
    return round(math.ceil(price / tick - 1e-9) * tick, 2)


def floor_to_tick(price: float) -> float:
    """Round DOWN to the conventional option tick.

    Used to cap a suggested SELL limit at the current ask: the largest valid
    tick not exceeding ``price``, so a rich IV model (or a stale ``last`` above
    the ask) can never push the suggestion past the best offer. Mirrors
    ceil_to_tick's epsilon so a price already on a tick doesn't drop a full tick
    on float-division noise (3.95 stays 3.95).
    """
    tick = tick_for(price)
    return round(math.floor(price / tick + 1e-9) * tick, 2)


def assess_fill(*, bid, ask, mid=None, volume=None, open_interest=0,
                min_oi: int = MIN_OI, max_spread_pct: float = MAX_SPREAD_PCT,
                max_spread_abs: float = MAX_SPREAD_ABS) -> FillAssessment:
    """Judge whether a put looks executable and suggest a limit (credit/share).

    Liquid requires a two-sided market, open interest >= `min_oi`, and a
    spread that's either <= `max_spread_pct` of mid OR <= `max_spread_abs` in
    dollars. The suggested limit is the mid rounded to the tick. Volume is a
    soft note only — it's 0 for every contract while the market is closed, so
    it can't be a hard gate.
    """
    reasons: list[str] = []
    notes: list[str] = []

    b = float(bid or 0.0)
    a = float(ask or 0.0)
    if b <= 0 or a <= 0:
        reasons.append("no two-sided market (missing bid or ask)")
        return FillAssessment(False, None, reasons, notes)

    mid_val = float(mid) if (mid and mid > 0) else (b + a) / 2
    spread = a - b
    spread_pct = spread / mid_val if mid_val > 0 else float("inf")

    if spread_pct > max_spread_pct and spread > max_spread_abs:
        reasons.append(
            f"wide bid/ask spread (${spread:.2f}, {spread_pct * 100:.0f}% of mid)"
        )
    if open_interest < min_oi:
        reasons.append(f"thin open interest ({open_interest} < {min_oi})")

    if volume is not None:
        if volume == 0:
            notes.append("no volume today (0) — normal while the market is closed")
        elif volume < 10:
            notes.append(f"low volume today ({volume})")

    liquid = not reasons
    # Always suggest a mid-anchored limit; `liquid` tells the caller whether
    # to trust a fill there. The illiquid path layers an IV-aligned model
    # price on top (see model_limit) so a trade can still be priced.
    suggested = round_to_tick(mid_val)
    return FillAssessment(liquid, suggested, reasons, notes)


def model_limit(*, spot, strike, dte, iv, option_type: str = "put"
                ) -> float | None:
    """IV-aligned limit — the Black-Scholes price at the contract's own IV.

    Anchors the limit to the option's implied vol (which carries the IV+pp
    edge) rather than a wide/thin market mid that may not be a meaningful
    number. `option_type` ("call"/"put", or "C"/"P") picks which side to price —
    a covered call must use the call price, not the (often far pricier ITM) put
    at the same strike. Used on the illiquid path to still propose a price, even
    though a fill there is unlikely. Returns None when inputs are missing/
    degenerate.
    """
    if spot is None or iv is None or not dte:
        return None
    if spot <= 0 or strike <= 0 or iv <= 0:
        return None
    T = dte / 365.0
    if T <= 0:
        return None
    kind = "call" if str(option_type).lower() in ("c", "call") else "put"
    from stocks_shared.black_scholes import bs_price
    price = bs_price(spot, strike, T, RISK_FREE_RATE, iv, kind)
    return round_to_tick(price) if price > 0 else None


# ── Account capacity (read-only) ─────────────────────────────────────────────

def _mask_account(num: str | None) -> str | None:
    """Last-4 mask of an account number — safe to show on a screen-share."""
    if not num:
        return None
    s = str(num)
    return "..." + s[-4:] if len(s) > 4 else s


@dataclass
class AccountCapacity:
    """Read-only Schwab balances for sizing cash-secured puts.

    `amount` is the cash that can *collateralize* a CSP — deliberately NOT
    margin buying power. Cash accounts expose ``cashAvailableForTrading``;
    margin accounts don't (it's a cash-account field), so for a cash-secured
    put the right figure there is ``availableFundsNonMarginableTrade`` (funds
    that aren't borrowed), never ``buyingPower`` (margin BP would over-size).
    `balances` keeps the full numeric ``currentBalances`` for the dialog's
    account-info panel.
    """

    cash_available: float | None = None   # cashAvailableForTrading (cash acct)
    non_marginable: float | None = None   # availableFundsNonMarginableTrade
    available_funds: float | None = None  # availableFunds
    buying_power: float | None = None     # buyingPower (margin BP — info only)
    account_type: str | None = None       # CASH / MARGIN
    account_mask: str | None = None       # account number, last-4 masked
    balances: dict = field(default_factory=dict)  # full numeric currentBalances

    @property
    def amount(self) -> float | None:
        """Funds that can secure a CSP: cash-account field, else a margin
        account's non-marginable funds, else plain available funds. Excludes
        margin buying power on purpose."""
        for v in (self.cash_available, self.non_marginable, self.available_funds):
            if v is not None:
                return v
        return None

    @property
    def amount_field(self) -> str | None:
        """Which Schwab balance `amount` came from. The UI labels the figure
        with this: only the cash-account field is literally cash, so calling all
        three "Avail Cash" overstated what a margin account has sitting there."""
        if self.cash_available is not None:
            return "cashAvailableForTrading"
        if self.non_marginable is not None:
            return "availableFundsNonMarginableTrade"
        if self.available_funds is not None:
            return "availableFunds"
        return None

    @property
    def amount_label(self) -> str:
        """Short label for `amount` — "Avail Cash" only when it really is cash."""
        return ("Avail Cash"
                if self.amount_field == "cashAvailableForTrading"
                else "Avail Funds")

    @property
    def amount_note(self) -> str:
        """One line saying what the figure is, for under the sizing readout."""
        if self.amount_field == "cashAvailableForTrading":
            return "Cash available for trading."
        if self.amount_field == "availableFundsNonMarginableTrade":
            return ("Schwab's *availableFundsNonMarginableTrade* — a margin "
                    "account figure, not your cash balance.")
        if self.amount_field == "availableFunds":
            return "Schwab's *availableFunds* — not your cash balance."
        return ""


def fetch_account_capacity(client) -> AccountCapacity | None:
    """Read available cash / buying power from the first Schwab account.

    Read-only — no order entry. Returns None on any failure so the UI
    degrades gracefully (capacity is informational, never a hard dependency).
    """
    try:
        nums = client.get_account_numbers().json()
        entry = nums[0]
        account_hash = entry["hashValue"]
        acct = (client.get_account(account_hash).json()
                .get("securitiesAccount", {}))
        bal = acct.get("currentBalances", {})

        def _f(key):
            v = bal.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        return AccountCapacity(
            cash_available=_f("cashAvailableForTrading"),
            non_marginable=_f("availableFundsNonMarginableTrade"),
            available_funds=_f("availableFunds"),
            buying_power=(_f("buyingPower") if bal.get("buyingPower") is not None
                          else _f("optionBuyingPower")),
            account_type=acct.get("type"),
            account_mask=_mask_account(entry.get("accountNumber")),
            balances={k: float(v) for k, v in bal.items()
                      if isinstance(v, (int, float))},
        )
    except Exception:
        return None


def puts_affordable(capacity: float | None, strike: float | None) -> int | None:
    """How many cash-secured puts `capacity` covers at `strike`.

    capacity ÷ (strike × 100), floored. None when inputs are missing.
    """
    if capacity is None or strike is None or strike <= 0:
        return None
    return int(capacity // (strike * 100))


def calls_coverable(long_shares: float | None,
                    existing_short_calls: int = 0) -> int | None:
    """How many *new* covered calls `long_shares` supports.

    floor(shares / 100) minus calls already written against the position,
    clamped at >= 0. None when the share count is unknown.
    """
    if long_shares is None:
        return None
    return max(0, int(float(long_shares) // 100)
               - int(existing_short_calls or 0))


def _account_positions(client, account_hash: str | None = None) -> list[dict]:
    """Raw Schwab positions list for the account — ONE positions fetch. [] on
    any failure. Resolves the first linked account when `account_hash` is
    omitted. Shared by every positions reader below so the account round-trip
    happens once per caller. Read-only."""
    try:
        from schwab.client import Client
        if account_hash is None:
            account_hash = client.get_account_numbers().json()[0]["hashValue"]
        acct = (client.get_account(
            account_hash, fields=Client.Account.Fields.POSITIONS)
            .json().get("securitiesAccount", {}))
        return acct.get("positions", []) or []
    except Exception:
        return []


def _parse_option_symbol(symbol: str):
    """Parse a Schwab/OSI option symbol → (root, expiration_iso, "C"/"P",
    strike). None when it isn't a well-formed option symbol.

    Inverse of ``_osi`` / schwab-py's ``OptionSymbol.build``: a left-justified
    root followed by a fixed 15-char tail ``YYMMDD`` + ``C``|``P`` + an 8-digit
    strike (price × 1000). E.g. ``"AMD   260116C00200000"`` → ("AMD",
    "2026-01-16", "C", 200.0)."""
    if not symbol:
        return None
    s = str(symbol).strip()
    if len(s) < 16:  # need at least a 1-char root + the 15-char tail
        return None
    tail, root = s[-15:], s[:-15].strip()
    yy, mm, dd, cp, strike_raw = (tail[0:2], tail[2:4], tail[4:6],
                                  tail[6].upper(), tail[7:15])
    if cp not in ("C", "P") or not (yy + mm + dd + strike_raw).isdigit():
        return None
    try:
        exp_iso = f"{2000 + int(yy):04d}-{mm}-{dd}"
        strike = int(strike_raw) / 1000.0
    except ValueError:
        return None
    return root, exp_iso, cp, strike


COVERAGE_STATES = ("over_written", "uncovered", "partial", "covered")


def classify_coverage(shares: float | None, short_calls: int) -> dict:
    """How a stock position stands against the calls written on it.

    Returns ``{state, lots, written, covered_shares, uncovered_shares,
    coverable, naked_calls}``. ``state`` is one of:

    * ``over_written`` — more short calls than 100-share lots. Part of the call
      position is NOT backed by stock, so assignment forces a buy at market.
      Called out separately because `calls_coverable` clamps at 0, which would
      otherwise render this identically to a fully covered position.
    * ``uncovered`` — no calls written against the shares.
    * ``partial``    — some written, and a further 100-lot is still free.
    * ``covered``    — written up to the last whole lot; nothing left to write.

    "Covered" means no *writable* lot remains, not that every share is spoken
    for: 450 shares against 4 calls leaves 50 shares unwritten, and there is
    nothing you can do with 50 shares, so it reads as covered.

    Pure arithmetic on two numbers so it can be tested without a broker.
    """
    try:
        sh = max(0.0, float(shares or 0))
    except (TypeError, ValueError):
        sh = 0.0
    try:
        written = max(0, int(short_calls or 0))
    except (TypeError, ValueError):
        written = 0

    lots = int(sh // 100)
    coverable = max(0, lots - written)
    naked_calls = max(0, written - lots)
    covered_shares = min(written * 100, int(sh))
    uncovered_shares = max(0, int(sh) - written * 100)

    if naked_calls:
        state = "over_written"
    elif written == 0:
        state = "uncovered"
    elif coverable:
        state = "partial"
    else:
        state = "covered"
    return {"state": state, "lots": lots, "written": written,
            "covered_shares": covered_shares,
            "uncovered_shares": uncovered_shares,
            "coverable": coverable, "naked_calls": naked_calls}


def equity_positions(client, account_hash: str | None = None) -> list[dict]:
    """Every stock position in the account, each with its covered-call standing.

    ONE positions fetch (the same call the option readers make), so the shares
    and the calls written against them can't come from two different reads of a
    moving account. Read-only; [] on any failure.

    Each entry carries the raw position (`ticker`, `shares`, `avg_price`,
    `market_value`, `pl`) plus everything `classify_coverage` returns. Long
    equity only — a short stock position can't back a call, and nothing here
    would know what to do with one.
    """
    positions = _account_positions(client, account_hash)
    if not positions:
        return []
    shares: dict[str, dict] = {}
    calls: dict[str, int] = {}
    for p in positions:
        inst = p.get("instrument", {}) or {}
        atype = str(inst.get("assetType", "")).upper()
        if atype == "EQUITY":
            tkr = str(inst.get("symbol", "")).upper()
            qty = float(p.get("longQuantity", 0) or 0)
            if not tkr or qty <= 0:
                continue
            rec = shares.setdefault(
                tkr, {"shares": 0.0, "cost": 0.0, "market_value": 0.0,
                      "pl": 0.0, "pl_known": False})
            rec["shares"] += qty
            # Weighted cost, so two lots of the same ticker average correctly.
            rec["cost"] += qty * float(p.get("averagePrice", 0) or 0)
            rec["market_value"] += float(p.get("marketValue", 0) or 0)
            _pl = p.get("longOpenProfitLoss")
            if _pl is not None:
                rec["pl"] += float(_pl or 0)
                rec["pl_known"] = True
        elif (atype == "OPTION"
              and str(inst.get("putCall", "")).upper() == "CALL"):
            tkr = str(inst.get("underlyingSymbol", "")).upper()
            if tkr:
                calls[tkr] = calls.get(tkr, 0) + int(
                    float(p.get("shortQuantity", 0) or 0))

    out: list[dict] = []
    for tkr, rec in shares.items():
        qty = rec["shares"]
        cov = classify_coverage(qty, calls.get(tkr, 0))
        # Fall back to market value − cost when Schwab omits the P/L field;
        # None (not 0) when neither is usable, so the column can stay blank
        # rather than claiming the position is flat.
        pl = rec["pl"] if rec["pl_known"] else (
            rec["market_value"] - rec["cost"] if rec["market_value"] else None)
        out.append({
            "ticker": tkr,
            "shares": qty,
            "avg_price": (rec["cost"] / qty) if qty else 0.0,
            "market_value": rec["market_value"],
            "pl": pl,
            **cov,
        })
    return sorted(out, key=lambda r: r["ticker"])


def held_shares_and_short_calls_map(client, account_hash: str | None = None
                                    ) -> dict[str, dict]:
    """{TICKER: {"shares": float, "short_calls": int}} for every underlying with
    a position in the account — ONE Schwab positions fetch for the whole basket
    (vs. one call per ticker). Lets the watchlist Calls board gate which rows are
    coverable without N round-trips. Resolves the first linked account when
    `account_hash` is omitted. Read-only; returns {} on any failure so callers
    degrade to "no coverage" rather than erroring.
    """
    out: dict[str, dict] = {}
    for p in _account_positions(client, account_hash):
        inst = p.get("instrument", {}) or {}
        atype = str(inst.get("assetType", "")).upper()
        if atype == "EQUITY":
            tkr = str(inst.get("symbol", "")).upper()
            if not tkr:
                continue
            rec = out.setdefault(tkr, {"shares": 0.0, "short_calls": 0})
            rec["shares"] += float(p.get("longQuantity", 0) or 0)
        elif (atype == "OPTION"
              and str(inst.get("putCall", "")).upper() == "CALL"):
            tkr = str(inst.get("underlyingSymbol", "")).upper()
            if not tkr:
                continue
            rec = out.setdefault(tkr, {"shares": 0.0, "short_calls": 0})
            rec["short_calls"] += int(float(p.get("shortQuantity", 0) or 0))
    return out


def open_option_positions(client, account_hash: str | None = None
                          ) -> list[dict]:
    """Every option leg held in the account, one entry per leg — the source for
    the roll builder. ONE positions fetch. Read-only; [] on any failure.

    Direction-agnostic on purpose (both short and long legs) so future
    long-option rolling reuses it without a rewrite. Each entry:

      underlying, option_type ("C"/"P"), strike, expiration (YYYY-MM-DD),
      quantity (int > 0), direction ("short"/"long"), avg_price (open
      premium/share), market_value, shares_held (of the underlying), and —
      for calls — covered (short call backed by 100+ shares/contract).
    """
    positions = _account_positions(client, account_hash)
    if not positions:
        return []
    # Shares held per underlying first, so a short call can be judged covered.
    shares_by_tkr: dict[str, float] = {}
    for p in positions:
        inst = p.get("instrument", {}) or {}
        if str(inst.get("assetType", "")).upper() == "EQUITY":
            tkr = str(inst.get("symbol", "")).upper()
            if tkr:
                shares_by_tkr[tkr] = (shares_by_tkr.get(tkr, 0.0)
                                      + float(p.get("longQuantity", 0) or 0))
    legs: list[dict] = []
    for p in positions:
        inst = p.get("instrument", {}) or {}
        if str(inst.get("assetType", "")).upper() != "OPTION":
            continue
        parsed = _parse_option_symbol(inst.get("symbol", ""))
        if parsed is None:
            continue
        root, exp_iso, opt_type, strike = parsed
        short_q = int(float(p.get("shortQuantity", 0) or 0))
        long_q = int(float(p.get("longQuantity", 0) or 0))
        if short_q > 0:
            direction, qty = "short", short_q
        elif long_q > 0:
            direction, qty = "long", long_q
        else:
            continue
        underlying = str(inst.get("underlyingSymbol", "")).upper() or root
        shares_held = shares_by_tkr.get(underlying, 0.0)
        entry = {
            "underlying": underlying,
            "option_type": opt_type,
            "strike": strike,
            "expiration": exp_iso,
            "quantity": qty,
            "direction": direction,
            "avg_price": float(p.get("averagePrice", 0) or 0),
            "market_value": float(p.get("marketValue", 0) or 0),
            "shares_held": shares_held,
        }
        if opt_type == "C":
            entry["covered"] = (direction == "short"
                                and shares_held >= 100 * qty)
        legs.append(entry)
    return legs


def is_rollable(leg: dict) -> bool:
    """Whether one held leg can be rolled: short covered calls + short puts.

    A short call qualifies when the underlying is share-backed (>= 100 shares —
    a covered-call program). We deliberately do NOT require every contract to be
    individually covered: a ticker's several call legs share one share pool, and
    each open leg must still be rollable. (The strict per-leg `covered` flag
    double-counts that pool, so it must not gate this.) Truly naked calls — no
    shares of the underlying — are excluded. All short puts qualify (treated as
    cash-secured). Long legs are never rollable here: a roll replaces premium
    you sold, and the Trades P/L model is credit-received.

    The Positions tab asks this per row (to offer or refuse the Roll action) and
    `rollable_positions` asks it of a whole account, so the rule has one home.
    """
    if str(leg.get("direction", "")).strip().lower() != "short":
        return False
    if str(leg.get("option_type", "")).upper() == "C":
        try:
            return float(leg.get("shares_held", 0) or 0) >= 100
        except (TypeError, ValueError):
            return False
    return True


def rollable_positions(client, account_hash: str | None = None) -> list[dict]:
    """Short covered calls + short puts held in the account — ONE entry per
    strike/expiration leg. A thin filter (`is_rollable`) over
    `open_option_positions`; the general reader stays reusable for future
    long-option rolling. Read-only; [] on any failure.
    """
    return [leg for leg in open_option_positions(client, account_hash)
            if is_rollable(leg)]


def held_shares_and_short_calls(client, ticker: str,
                                account_hash: str | None = None
                                ) -> tuple[float, int]:
    """(long shares, short call contracts) held for `ticker` in the account.

    Reads the account's positions (Schwab `get_account` with the positions
    field) so a covered call can be sized against shares actually owned, net of
    calls already written on the same underlying. Resolves the first linked
    account when `account_hash` is omitted (mirrors `account_capacity`).
    Read-only; returns (0.0, 0) on any failure so the UI degrades to "no
    coverage" rather than erroring.
    """
    rec = held_shares_and_short_calls_map(client, account_hash).get(
        str(ticker).upper(), {})
    return float(rec.get("shares", 0.0)), int(rec.get("short_calls", 0))


# ── Order building (validation only — placement is a later, separate step) ───

@dataclass
class OptionSellOrder:
    """A single-leg, sell-to-open short option — a cash-secured put
    (``option_type="P"``) or a covered call (``option_type="C"``).

    Describes exactly what will be sent. Building it never places anything;
    ``place_option_sell_order`` (schwab-py ``option_sell_to_open_limit`` →
    ``client.place_order``) performs the LIVE submission, and only after the
    user's explicit confirm in the dialog.
    """

    ticker: str
    strike: float
    expiration: str  # YYYY-MM-DD
    limit: float     # credit per share
    quantity: int
    option_type: str = "P"  # "P" = cash-secured put, "C" = covered call

    @property
    def credit(self) -> float:
        """Total premium received if filled at the limit."""
        return round(self.limit * 100 * self.quantity, 2)

    @property
    def collateral(self) -> float:
        """Cash required to secure the put(s) — puts only."""
        return round(self.strike * 100 * self.quantity, 2)

    @property
    def shares_to_cover(self) -> int:
        """Shares needed to cover the call(s): 100 per contract — calls only."""
        return 100 * int(self.quantity)

    def describe(self) -> str:
        exp = datetime.strptime(self.expiration, "%Y-%m-%d").strftime("%b %d '%y")
        word = "CALL" if self.option_type == "C" else "PUT"
        return (f"SELL {self.quantity} {self.ticker} {exp} ${self.strike:g} "
                f"{word} @ ${self.limit:.2f} limit")


# Back-compat alias — older callers/tests built put orders by this name.
PutSellOrder = OptionSellOrder


def build_option_sell_order(*, ticker: str, strike: float, expiration: str,
                            limit: float, quantity: int,
                            option_type: str = "P",
                            capacity: float | None = None,
                            max_contracts: int | None = None) -> OptionSellOrder:
    """Validate and return a single-leg short-option sell order (no placement).

    Enforces guardrail #1 in code (single-leg, sell-to-open, qty >= 1,
    limit > 0, strike > 0). For a cash-secured put (``option_type="P"``), when
    `capacity` is given the collateral must fit. For a covered call
    (``option_type="C"``), when `max_contracts` is given the quantity must not
    exceed the shares-covered count. Raises ValueError on any violation.
    """
    if int(quantity) < 1:
        raise ValueError("quantity must be at least 1 contract")
    if float(limit) <= 0:
        raise ValueError("limit price must be positive")
    if float(strike) <= 0:
        raise ValueError("strike must be positive")
    if option_type not in ("P", "C"):
        raise ValueError("option_type must be 'P' or 'C'")
    order = OptionSellOrder(
        ticker=str(ticker), strike=float(strike), expiration=str(expiration),
        limit=float(limit), quantity=int(quantity), option_type=option_type,
    )
    if capacity is not None and order.collateral > capacity + 1e-6:
        raise ValueError(
            f"collateral ${order.collateral:,.0f} exceeds available "
            f"${capacity:,.0f}"
        )
    if max_contracts is not None and order.quantity > int(max_contracts):
        raise ValueError(
            f"{order.quantity} contracts exceeds the {int(max_contracts)} you "
            "can cover")
    return order


# Back-compat alias — older callers/tests use this name.
build_put_sell_order = build_option_sell_order


def close_input_error(limit, contracts, held: int) -> str | None:
    """Why this close can't be placed, or None when the inputs are usable.

    The closing screens have no order-builder to validate through (a close is
    assembled at submit time), so this is their equivalent: a positive limit and
    a contract count between 1 and the size actually held. It exists as a
    function rather than widget ``min_value``/``max_value`` because Streamlit
    won't commit an out-of-range entry — it keeps the last valid value and shows
    its own message, which would leave a Place button armed for a number the user
    never typed. Returns the message so the caller can show it *and* use it as
    the disabled Confirm button's tooltip.
    """
    if limit is None or contracts is None:
        return "Enter a limit price and a contract count."
    try:
        limit, contracts = float(limit), int(contracts)
    except (TypeError, ValueError):
        return "Enter a limit price and a contract count."
    if limit <= 0:
        return "Limit price must be positive."
    if contracts < 1:
        return "Close at least 1 contract."
    if contracts > int(held):
        return (f"You hold {int(held)} contract(s) — can't close "
                f"{contracts}.")
    return None


# ── Market hours + LIVE placement (only ever reached behind a confirm step) ──

def market_is_open(client) -> bool | None:
    """True/False if the equity-options market is open RIGHT NOW, None unknown.

    Schwab's `isOpen` flag only means "today is a trading day" — it stays True
    after the close — so we also test the current instant against the day's
    regular-session window. Schwab returns those session times as tz-aware ISO
    timestamps (…-04:00); comparing them to a tz-aware UTC `now` is correct no
    matter the machine's local timezone (it never reads the local clock's
    zone). `isOpen` still gates weekends/holidays. None on any failure → the
    caller fails safe and keeps placement disabled.
    """
    try:
        from schwab.client import Client
        resp = client.get_market_hours(
            markets=[Client.MarketHours.Market.OPTION])
        eqo = resp.json().get("option", {}).get("EQO", {})
        if not eqo.get("isOpen"):
            return False  # weekend / holiday — not a trading day
        now = datetime.now(timezone.utc)
        sessions = (eqo.get("sessionHours") or {}).get("regularMarket") or []
        for s in sessions:
            start = datetime.fromisoformat(s["start"])
            end = datetime.fromisoformat(s["end"])
            if start <= now <= end:
                return True
        return False  # trading day, but outside the regular session
    except Exception:
        return None


def resolve_account_hash(client, last4: str | None = None):
    """Return (account_hash, masked_number) for the order's target account.

    With a single linked account, uses it. With several, requires `last4` to
    pick exactly one — so a live order can never land in the wrong account.
    Returns None when nothing matches unambiguously or the lookup fails.
    """
    try:
        nums = client.get_account_numbers().json()
        if not isinstance(nums, list) or not nums:
            return None
        if last4:
            matches = [n for n in nums
                       if str(n.get("accountNumber", "")).endswith(str(last4))]
            if len(matches) == 1:
                m = matches[0]
                return m["hashValue"], _mask_account(m.get("accountNumber"))
            return None
        if len(nums) == 1:
            return nums[0]["hashValue"], _mask_account(
                nums[0].get("accountNumber"))
        return None
    except Exception:
        return None


def _osi(ticker: str, strike: float, expiration: str, right: str = "P") -> str:
    """OSI option symbol; `right` is "P" or "C". schwab-py's OptionSymbol wants
    a date object (or YYMMDD), not the YYYY-MM-DD string the rest of the app
    uses."""
    from schwab.orders.options import OptionSymbol
    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    return OptionSymbol(ticker, exp_date, right, f"{float(strike):g}").build()


def _submit_spec(client, account_hash: str, spec) -> dict:
    """POST a built order spec; return {ok, order_id, error}. Surfaces
    Schwab's error payload on a non-2xx rather than raising."""
    try:
        resp = client.place_order(account_hash, spec)
    except Exception as exc:  # noqa: BLE001 — surface any transport error
        return {"ok": False, "order_id": None, "error": str(exc)}
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("errors", [{}])[0].get("detail")
        except Exception:
            detail = None
        return {"ok": False, "order_id": None,
                "error": detail or f"HTTP {resp.status_code}"}
    order_id = None
    try:
        from schwab.utils import Utils
        order_id = Utils(client, account_hash).extract_order_id(resp)
    except Exception:
        order_id = None
    if not order_id:
        # Fallback: pull the trailing id from the Location header ourselves, so
        # in-app status/cancel still work if schwab-py's parser comes up empty.
        loc = getattr(resp, "headers", {}).get("Location", "") or ""
        tail = loc.rstrip("/").rsplit("/", 1)[-1] if loc else ""
        if tail.isdigit():
            order_id = tail
    return {"ok": True, "order_id": order_id, "error": None}


def place_option_sell_order(client, order: OptionSellOrder,
                            account_hash: str) -> dict:
    """Submit a single-leg, sell-to-open short option (put or call). LIVE.

    Only ever called after the user's explicit confirm. Re-asserts guardrail
    #1 (qty >= 1, positive limit/strike, single-leg sell-to-open) before
    sending. The put/call right comes from ``order.option_type``. Returns
    {"ok", "order_id", "error"}.
    """
    if order.quantity < 1 or order.limit <= 0 or order.strike <= 0:
        return {"ok": False, "order_id": None, "error": "invalid order"}
    try:
        from schwab.orders.options import option_sell_to_open_limit
        spec = option_sell_to_open_limit(
            _osi(order.ticker, order.strike, order.expiration,
                 getattr(order, "option_type", "P")),
            int(order.quantity), f"{order.limit:.2f}")
    except Exception as exc:  # noqa: BLE001 — bad date / build failure
        return {"ok": False, "order_id": None, "error": str(exc)}
    return _submit_spec(client, account_hash, spec)


def place_option_close_order(client, *, ticker: str, strike: float,
                             expiration: str, limit: float, quantity: int,
                             account_hash: str, option_type: str = "P",
                             direction: str = "short") -> dict:
    """Submit a limit order to close an existing option leg. LIVE.

    A short leg (``direction="short"``, the default) closes with a
    BUY_TO_CLOSE; a long leg (``direction="long"``) closes with a
    SELL_TO_CLOSE. Only ever called after the user's explicit confirm. `limit`
    is the price per share; `option_type` is "P" or "C".
    """
    if int(quantity) < 1 or float(limit) <= 0 or float(strike) <= 0:
        return {"ok": False, "order_id": None, "error": "invalid order"}
    try:
        from schwab.orders.options import (option_buy_to_close_limit,
                                           option_sell_to_close_limit)
        _build = (option_sell_to_close_limit if str(direction) == "long"
                  else option_buy_to_close_limit)
        spec = _build(
            _osi(ticker, strike, expiration, option_type),
            int(quantity), f"{float(limit):.2f}")
    except Exception as exc:  # noqa: BLE001 — bad date / build failure
        return {"ok": False, "order_id": None, "error": str(exc)}
    return _submit_spec(client, account_hash, spec)


# Back-compat aliases — older callers/tests use the put-named forms.
place_put_sell_order = place_option_sell_order
place_put_close_order = place_option_close_order


# ── Rolls (atomic buy-to-close + sell-to-open, one net-price order) ──────────

@dataclass
class RollOrder:
    """A roll: buy-to-close a held short option and sell-to-open a new one on
    the same underlying + right, submitted as ONE net-price order so both legs
    fill together (no leg-in risk).

    ``net_limit`` is the per-share net price, SIGNED: positive = a net credit
    (you collect), negative = a net debit (you pay). Building never places
    anything; ``place_roll_order`` performs the LIVE submission, and only after
    the user's explicit confirm.
    """

    ticker: str
    option_type: str  # "P" or "C" — same right on both legs
    close_strike: float
    close_expiration: str  # YYYY-MM-DD (the held leg)
    open_strike: float
    open_expiration: str   # YYYY-MM-DD (the new leg)
    quantity: int
    net_limit: float       # per-share net; + = credit, - = debit

    @property
    def is_credit(self) -> bool:
        """True when the roll collects premium net (a net-credit roll)."""
        return self.net_limit >= 0

    @property
    def net_amount(self) -> float:
        """Total net cash if filled at ``net_limit`` — signed (+ = received)."""
        return round(self.net_limit * 100 * self.quantity, 2)

    def describe(self) -> str:
        ce = datetime.strptime(self.close_expiration,
                               "%Y-%m-%d").strftime("%b %d '%y")
        oe = datetime.strptime(self.open_expiration,
                               "%Y-%m-%d").strftime("%b %d '%y")
        word = "CALL" if self.option_type == "C" else "PUT"
        kind = "credit" if self.is_credit else "debit"
        return (f"ROLL {self.quantity} {self.ticker} {word} "
                f"${self.close_strike:g} {ce} → ${self.open_strike:g} {oe} "
                f"@ ${abs(self.net_limit):.2f} net {kind}")


def build_roll_order(*, ticker: str, option_type: str,
                     close_strike: float, close_expiration: str,
                     open_strike: float, open_expiration: str,
                     quantity: int, net_limit: float) -> RollOrder:
    """Validate and return a ``RollOrder`` (no placement).

    Both legs must share the underlying (implicit — one ``ticker``) and the
    right, quantity ≥ 1, both strikes > 0, and the target leg must differ from
    the held one (else it isn't a roll). Raises ``ValueError`` on any
    violation. Mirrors ``build_option_sell_order``.
    """
    if option_type not in ("P", "C"):
        raise ValueError("option_type must be 'P' or 'C'")
    if int(quantity) < 1:
        raise ValueError("quantity must be at least 1 contract")
    if float(close_strike) <= 0 or float(open_strike) <= 0:
        raise ValueError("strikes must be positive")
    if (float(close_strike) == float(open_strike)
            and str(close_expiration) == str(open_expiration)):
        raise ValueError("roll target must differ from the current leg")
    return RollOrder(
        ticker=str(ticker), option_type=option_type,
        close_strike=float(close_strike),
        close_expiration=str(close_expiration),
        open_strike=float(open_strike), open_expiration=str(open_expiration),
        quantity=int(quantity), net_limit=float(net_limit))


def roll_net_quote(close_q: dict | None, open_q: dict | None) -> dict | None:
    """What the market is paying for a roll *right now*, per share.

    A roll buys back the held leg and sells the new one as one net order, so the
    net you'd get depends on which side of each spread you cross:

    * ``worst`` — sell the new leg at its **bid**, buy the old back at its
      **ask**. What you'd collect crossing both spreads immediately.
    * ``mid``   — both legs at their mids. The realistic fill estimate.
    * ``best``  — new leg at its **ask**, old at its **bid**. Only if both
      sides come to you.

    Positive is a net credit, negative a net debit — the same sign convention as
    the limit on the order, so ``mid`` vs that limit answers "is my price
    reachable, or am I waiting for a move?".

    None when either leg has no usable two-sided quote.
    """
    def _f(q, key):
        try:
            v = float((q or {}).get(key))
        except (TypeError, ValueError):
            return None
        return v if v == v and v > 0 else None

    c_bid, c_ask, c_mid = _f(close_q, "bid"), _f(close_q, "ask"), _f(close_q, "mid")
    o_bid, o_ask, o_mid = _f(open_q, "bid"), _f(open_q, "ask"), _f(open_q, "mid")
    if None in (c_bid, c_ask, o_bid, o_ask):
        return None
    if c_mid is None:
        c_mid = (c_bid + c_ask) / 2
    if o_mid is None:
        o_mid = (o_bid + o_ask) / 2
    return {"worst": round(o_bid - c_ask, 2),
            "mid": round(o_mid - c_mid, 2),
            "best": round(o_ask - c_bid, 2)}


def place_roll_order(client, roll: RollOrder, account_hash: str) -> dict:
    """Submit a roll as ONE two-leg net-price order. LIVE.

    Only ever called after the user's explicit confirm. Builds a single order
    with a BUY_TO_CLOSE leg (the held option) and a SELL_TO_OPEN leg (the new
    option), priced NET_CREDIT / NET_DEBIT per ``roll.net_limit``'s sign so both
    legs execute together. Returns {"ok", "order_id", "error"}.
    """
    if (roll.quantity < 1 or roll.close_strike <= 0 or roll.open_strike <= 0):
        return {"ok": False, "order_id": None, "error": "invalid order"}
    try:
        from schwab.orders.generic import OrderBuilder
        from schwab.orders.common import (
            Duration, OptionInstruction, OrderStrategyType, OrderType, Session,
        )
        close_sym = _osi(roll.ticker, roll.close_strike, roll.close_expiration,
                         roll.option_type)
        open_sym = _osi(roll.ticker, roll.open_strike, roll.open_expiration,
                        roll.option_type)
        order_type = (OrderType.NET_CREDIT if roll.is_credit
                      else OrderType.NET_DEBIT)
        spec = (OrderBuilder()
                .set_session(Session.NORMAL)
                .set_duration(Duration.DAY)
                .set_order_type(order_type)
                .set_order_strategy_type(OrderStrategyType.SINGLE)
                .set_price(f"{abs(roll.net_limit):.2f}")
                .add_option_leg(OptionInstruction.BUY_TO_CLOSE, close_sym,
                                int(roll.quantity))
                .add_option_leg(OptionInstruction.SELL_TO_OPEN, open_sym,
                                int(roll.quantity)))
    except Exception as exc:  # noqa: BLE001 — bad date / build failure
        return {"ok": False, "order_id": None, "error": str(exc)}
    return _submit_spec(client, account_hash, spec)


@dataclass
class UnwindOrder:
    """An unwind: buy-to-close a covered call **and** sell the shares backing it,
    submitted as ONE net-price order so both legs fill together.

    The whole point is atomicity. Closing the call and selling the stock as two
    orders leaves a window where the call is gone and the shares aren't (or
    worse, the shares are gone and the call is naked); as one net order Schwab
    fills both or neither. A multi-contract order can still fill *partially* —
    fewer contracts with proportionally fewer shares — so you can end up half
    unwound, but never half a leg.

    ``net_limit`` is the per-share net CREDIT: stock proceeds minus what the
    buyback costs. Always positive for a covered call (a call can't be worth
    more than the stock it's written on), so unlike a roll there's no sign to
    carry. ``shares`` is 100 per contract — the ratio Schwab requires.
    """

    ticker: str
    strike: float
    expiration: str        # YYYY-MM-DD
    quantity: int          # contracts to close
    net_limit: float       # per-share net credit (stock − option)

    @property
    def shares(self) -> int:
        """Shares sold alongside — 100 per contract."""
        return int(self.quantity) * 100

    @property
    def net_amount(self) -> float:
        """Total cash received if filled at ``net_limit``."""
        return round(self.net_limit * 100 * self.quantity, 2)

    def describe(self) -> str:
        exp = datetime.strptime(self.expiration, "%Y-%m-%d").strftime("%b %d '%y")
        return (f"UNWIND {self.quantity} {self.ticker} CALL "
                f"${self.strike:g} {exp} + {self.shares} shares "
                f"@ ${self.net_limit:.2f} net credit")


def is_unwindable(leg: dict) -> bool:
    """Whether a held leg can be unwound: a short call with the shares to sell.

    Unwinding means "close the option and get out of the stock too", so it needs
    both halves of a covered call. A cash-secured put has no shares behind it
    (unwinding one is just closing it), and a long option isn't a position you
    exit by selling stock — both get the plain Close builder instead.

    Requires full share cover for the contracts held (100 each): with fewer
    shares than that, some of the calls are naked and selling the shares would
    leave them more so. Partial unwinds are still available inside the panel —
    this gates whether the action is offered at all.
    """
    if str(leg.get("direction", "")).strip().lower() != "short":
        return False
    if str(leg.get("option_type", "")).upper() != "C":
        return False
    try:
        return (float(leg.get("shares_held", 0) or 0)
                >= 100 * int(leg.get("quantity", 0) or 0) > 0)
    except (TypeError, ValueError):
        return False


def build_unwind_order(*, ticker: str, strike: float, expiration: str,
                       quantity: int, net_limit: float,
                       shares_held: float) -> UnwindOrder:
    """Validate and return an ``UnwindOrder`` (no placement).

    Raises ``ValueError`` with a user-facing message on any violation. The share
    check is the one that matters: selling more shares than are held would leave
    the remaining calls uncovered, which is the opposite of what an unwind is
    for. Mirrors ``build_roll_order``.
    """
    if int(quantity) < 1:
        raise ValueError("quantity must be at least 1 contract")
    if float(strike) <= 0:
        raise ValueError("strike must be positive")
    need = 100 * int(quantity)
    if float(shares_held or 0) < need:
        raise ValueError(
            f"unwinding {int(quantity)} contract(s) sells {need} shares, but "
            f"only {int(shares_held or 0)} are held")
    if float(net_limit) <= 0:
        raise ValueError("net limit must be a credit (stock proceeds exceed "
                         "the cost to buy the call back)")
    return UnwindOrder(ticker=str(ticker), strike=float(strike),
                       expiration=str(expiration), quantity=int(quantity),
                       net_limit=float(net_limit))


def unwind_net_quote(stock_q: dict | None, option_q: dict | None) -> dict | None:
    """What an unwind pays right now, per share — ``{worst, mid, best}``.

    You sell stock and buy the call back, so the net depends on which side of
    each spread you cross:

    * ``worst`` — stock at its **bid**, call bought at its **ask**.
    * ``mid``   — both at their mids. The realistic fill estimate.
    * ``best``  — stock at its **ask**, call at its **bid**.

    All three are credits (positive). None when either side lacks a usable
    two-sided quote. Mirrors ``roll_net_quote``.
    """
    def _f(q, key):
        try:
            v = float((q or {}).get(key))
        except (TypeError, ValueError):
            return None
        return v if v == v and v > 0 else None

    s_bid, s_ask, s_mid = (_f(stock_q, "bid"), _f(stock_q, "ask"),
                           _f(stock_q, "mid"))
    o_bid, o_ask, o_mid = (_f(option_q, "bid"), _f(option_q, "ask"),
                           _f(option_q, "mid"))
    if None in (s_bid, s_ask, o_bid, o_ask):
        return None
    if s_mid is None:
        s_mid = (s_bid + s_ask) / 2
    if o_mid is None:
        o_mid = (o_bid + o_ask) / 2
    return {"worst": round(s_bid - o_ask, 2),
            "mid": round(s_mid - o_mid, 2),
            "best": round(s_ask - o_bid, 2)}


def place_unwind_order(client, unwind: UnwindOrder, account_hash: str) -> dict:
    """Submit an unwind as ONE two-leg net-credit order. LIVE.

    One BUY_TO_CLOSE option leg and one SELL equity leg, priced NET_CREDIT, so
    the option close and the share sale execute together — never one without the
    other. Only ever called after the user's explicit confirm. Returns
    {"ok", "order_id", "error"}.
    """
    if unwind.quantity < 1 or unwind.strike <= 0 or unwind.net_limit <= 0:
        return {"ok": False, "order_id": None, "error": "invalid order"}
    try:
        from schwab.orders.generic import OrderBuilder
        from schwab.orders.common import (
            Duration, EquityInstruction, OptionInstruction, OrderStrategyType,
            OrderType, Session,
        )
        sym = _osi(unwind.ticker, unwind.strike, unwind.expiration, "C")
        spec = (OrderBuilder()
                .set_session(Session.NORMAL)
                .set_duration(Duration.DAY)
                .set_order_type(OrderType.NET_CREDIT)
                .set_order_strategy_type(OrderStrategyType.SINGLE)
                .set_price(f"{unwind.net_limit:.2f}")
                .add_option_leg(OptionInstruction.BUY_TO_CLOSE, sym,
                                int(unwind.quantity))
                .add_equity_leg(EquityInstruction.SELL, unwind.ticker,
                                int(unwind.shares)))
    except Exception as exc:  # noqa: BLE001 — bad date / build failure
        return {"ok": False, "order_id": None, "error": str(exc)}
    return _submit_spec(client, account_hash, spec)


# Order statuses where a cancel still makes sense (not yet terminal).
CANCELLABLE_STATUSES = frozenset({
    "WORKING", "QUEUED", "ACCEPTED", "NEW", "PENDING_ACTIVATION",
    "PENDING_ACKNOWLEDGEMENT", "AWAITING_PARENT_ORDER", "AWAITING_CONDITION",
    "AWAITING_MANUAL_REVIEW", "AWAITING_RELEASE_TIME",
    "AWAITING_STOP_CONDITION", "AWAITING_UR_OUT",
})


def _avg_fill_price(order: dict) -> float | None:
    """Quantity-weighted average execution price from a Schwab order, or None.

    Schwab reports each fill under ``orderActivityCollection[].executionLegs[]``
    with a per-share ``price`` and ``quantity``; a marketable order can fill in
    several legs/partials, so average them by quantity. None when no execution
    legs are present yet (e.g. still working / unfilled)."""
    px_qty = 0.0
    qty = 0.0
    for act in (order.get("orderActivityCollection") or []):
        if act.get("activityType") != "EXECUTION":
            continue
        for leg in (act.get("executionLegs") or []):
            try:
                _px = float(leg.get("price"))
                _q = float(leg.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                continue
            if _q > 0:
                px_qty += _px * _q
                qty += _q
    return round(px_qty / qty, 4) if qty > 0 else None


def get_order_status(client, order_id, last4: str | None = None) -> dict | None:
    """Read-only broker status for one order.

    Returns {status, filled, quantity, remaining, cancelable, filled_at,
    fill_price} or None on any failure. `cancelable` is True while the order is
    live but not yet terminal (so the UI can offer Cancel and avoid implying an
    unfilled order is a real position). `fill_price` is the quantity-weighted
    average execution price once any legs have filled, else None.
    """
    if not order_id:
        return None
    resolved = resolve_account_hash(client, last4)
    if not resolved:
        return None
    account_hash, _ = resolved
    try:
        resp = client.get_order(order_id, account_hash)
        if resp.status_code != 200:
            return None
        d = resp.json()
    except Exception:
        return None
    status = d.get("status")
    # Fill time: Schwab's closeTime is when the order reached its terminal
    # state (= the fill, for a FILLED order). Parsed to local time, or None.
    filled_at = None
    _ct = d.get("closeTime")
    if status == "FILLED" and _ct:
        try:
            filled_at = datetime.fromisoformat(
                _ct.replace("Z", "+00:00")).astimezone()
        except Exception:
            filled_at = None
    return {
        "status": status,
        "filled": d.get("filledQuantity"),
        "quantity": d.get("quantity"),
        "remaining": d.get("remainingQuantity"),
        "cancelable": status in CANCELLABLE_STATUSES,
        "filled_at": filled_at,
        "fill_price": _avg_fill_price(d),
    }


def cancel_order(client, order_id, last4: str | None = None) -> dict:
    """Cancel a working order. Returns {ok, error}.

    Canceling an unfilled order changes no position (no money moves); still
    routed through the same account resolution as placement.
    """
    if not order_id:
        return {"ok": False, "error": "no order id"}
    resolved = resolve_account_hash(client, last4)
    if not resolved:
        return {"ok": False, "error": "account not resolved"}
    account_hash, _ = resolved
    try:
        resp = client.cancel_order(order_id, account_hash)
    except Exception as exc:  # noqa: BLE001 — surface any transport error
        return {"ok": False, "error": str(exc)}
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("errors", [{}])[0].get("detail")
        except Exception:
            detail = None
        return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
    return {"ok": True, "error": None}


# ── Live re-quote (read-only) ────────────────────────────────────────────────

def requote_option(client, ticker: str, expiration: str, strike: float,
                   option_type: str = "P") -> dict | None:
    """Fresh bid/ask/mid/last for one option leg via the existing chain fetch.

    ``option_type`` ("P"/"C", or put/call) picks the calls or puts table — a
    covered call must re-quote the CALL, not the put at the same strike. Read-
    only; reuses ``schwab_live.fetch_option_chain_schwab``. Returns {bid, ask,
    mid, last, volume, open_interest, iv, delta, last_trade_ms} or None when
    unavailable (iv as a fraction; iv/delta are None when Schwab omits them;
    last_trade_ms is the last print's epoch-ms, 0 when Schwab omits it).
    """
    from stocks_shared.schwab_live import fetch_option_chain_schwab
    try:
        chain = fetch_option_chain_schwab(client, ticker, expiration)
    except Exception:
        return None
    if chain is None:
        return None
    is_call = str(option_type).upper() in ("C", "CALL")
    table = chain.calls if is_call else chain.puts
    if table is None or table.empty:
        return None
    row = table[table["strike"] == float(strike)]
    if row.empty:
        return None
    r = row.iloc[0]
    bid = float(r.get("bid", 0) or 0)
    ask = float(r.get("ask", 0) or 0)
    last = float(r.get("lastPrice", 0) or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    _vol_pct = float(r.get("volatility", 0) or 0)   # Schwab IV is a percent
    _delta = float(r.get("delta", 0) or 0)
    # Epoch-ms of the last print (Schwab tradeTimeInLong, kept by the chain
    # fetcher). 0 when absent — the display treats that as "no time".
    _last_ms = int(r.get("last_trade_ms", 0) or 0)
    return {"bid": bid, "ask": ask, "mid": mid, "last": last,
            "volume": int(r.get("volume", 0) or 0),
            "open_interest": int(r.get("openInterest", 0) or 0),
            "iv": (_vol_pct / 100.0) if _vol_pct else None,
            "delta": _delta if _delta else None,
            "last_trade_ms": _last_ms}


def requote_put(client, ticker: str, expiration: str,
                strike: float) -> dict | None:
    """Back-compat alias — re-quote a put. See ``requote_option``."""
    return requote_option(client, ticker, expiration, strike, option_type="P")


# Rate for the implied-vol/delta back-out below. Delta is only weakly
# sensitive to it at the short tenors put-sellers trade.
_FILL_SNAPSHOT_RISK_FREE = 0.045


def fill_snapshot(client, ticker: str, expiration: str, strike: float,
                  fill_price: float, filled_at,
                  risk_free: float = _FILL_SNAPSHOT_RISK_FREE) -> dict | None:
    """Reconstruct the underlying spot and option delta at an order's fill.

    The underlying's 1-minute bar nearest ``filled_at`` (a datetime) gives the
    spot; the implied vol backed out of the actual ``fill_price`` (premium per
    share) at that spot yields a delta consistent with both. Returns
    ``{fill_spot, fill_delta, fill_iv}`` (fill_delta/fill_iv None when no sane
    IV solves, e.g. a print at/below intrinsic), or None when the fill bar
    can't be located — the fill predates available intraday history or fell
    outside the minute-bar session.
    """
    from datetime import time as _dtime
    from stocks_shared.schwab_live import fetch_price_history_schwab
    from stocks_shared.black_scholes import bs_delta, implied_vol
    if not filled_at or not fill_price or fill_price <= 0:
        return None
    try:
        target = filled_at.timestamp()            # UTC epoch, tz-correct
        candles = fetch_price_history_schwab(client, ticker, "1m", limit=5000)
    except Exception:
        return None
    if not candles:
        return None
    bar = min(candles, key=lambda c: abs(c["time"] - target))
    if abs(bar["time"] - target) > 30 * 60:       # no bar within 30m of fill
        return None
    spot = float(bar["close"])
    if spot <= 0:
        return None
    try:
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        fa_local = filled_at.astimezone().replace(tzinfo=None)
        exp_dt = datetime.combine(exp_date, _dtime(16, 0))  # expiry at close
        T = max((exp_dt - fa_local).total_seconds(), 0.0) / (365.0 * 86400.0)
    except Exception:
        return None
    iv = implied_vol(float(fill_price), spot, float(strike), T, risk_free,
                     "put")
    delta = (bs_delta(spot, float(strike), T, risk_free, iv, "put")
             if iv is not None else None)
    return {"fill_spot": round(spot, 4),
            "fill_delta": round(delta, 4) if delta is not None else None,
            "fill_iv": round(iv, 6) if iv is not None else None}
