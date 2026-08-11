# Glossary

Common options and quantitative-finance terms used throughout this
repo's docs, scripts, and code. Definitions are brief — for a deeper
look at the scanner's IV+pp metric, see
[options-scanner/INTERPRETING_IV.md](../options-scanner/INTERPRETING_IV.md);
for spread strategies, see
[options-scanner/SPREADS.md](../options-scanner/SPREADS.md).

## Single-leg basics

**ATM (At the Money)** — Strike price equal to or very near the
current price of the underlying. Highest gamma here.

**Call** — Option contract giving the holder the right to *buy* the
underlying at the strike price by expiration.

**Cash-secured put (CSP)** — Selling a put while setting aside enough
cash to buy 100 shares per contract at the strike, so assignment is
always affordable. You keep the premium; if the stock falls below the
strike you may be assigned the shares.

**Covered call** — Selling a call against 100 shares per contract that
you already own. The shares "cover" the obligation to deliver, capping
your upside at the strike in exchange for the premium.

**DTE** — Days to expiration. Calendar days remaining until the
option contract expires.

**Exercise / Assignment** — *Exercise* is when an option holder
chooses to use their right (buy if call, sell if put).
*Assignment* is when the seller of that option is matched to
deliver the other side. US equity options are American-style — can
be exercised any day before expiration.

**Expiration** — Date the option contract terminates. After
expiration, the option is exercised, assigned, or expires worthless.

**Intrinsic value / Time value** — The two halves of an option's price.
*Intrinsic* is what it would be worth exercised right now (zero for an
OTM option); *time value* (extrinsic) is the rest — what buyers pay for
the chance of further movement. Time value is what decays to zero by
expiration, and what a seller closing early hands back.

**ITM (In the Money)** — Option with intrinsic value. Calls with
strike below spot; puts with strike above spot.

**LEAPS** — Long-term Equity AnticiPation Securities. Options with
expirations roughly 9+ months out. Often have thin volume, which
makes last-trade-based data sources (like Yahoo Finance) particularly
unreliable for them.

**Moneyness** — How far in or out of the money an option is, usually
as a percentage of spot rather than in dollars, so strikes on a $40
stock and a $400 stock are comparable. The scanner's Positions table
shades and sorts by ITM%.

**OTM (Out of the Money)** — Option with no intrinsic value, only
extrinsic (time + IV). Calls with strike above spot; puts with
strike below spot.

**Premium** — The market price of an option. The seller (writer)
receives premium up front; the buyer pays it.

**Put** — Option contract giving the holder the right to *sell* the
underlying at the strike price by expiration.

**Roll** — Closing an existing option position and simultaneously
opening a new one at a later expiration (and/or different strike).
Commonly done to extend a winning trade or repair a losing one.

**Strike price** — The price at which the option holder can buy
(call) or sell (put) the underlying if they exercise.

**Unwind** — Exiting both halves of a covered position at once: buying
back the short call *and* selling the shares behind it. Done as a single
net-price order, both legs fill together, so there's never a window
where the call is closed but you still hold the stock (or the shares are
gone and the call is left naked).

## Implied volatility & pricing

**Black-Scholes** — Classic options pricing model. The scanner uses
it to compute Greeks (delta especially) when the data source doesn't
provide them directly — Yahoo Finance, for example.

**Implied volatility (IV)** — The volatility number implied by an
option's current market price, working backward through
Black-Scholes or a similar model. Higher IV means a pricier option.

**IV+pp** — The scanner's headline metric. How far above or below
the fitted volatility surface a given option's IV sits, in
percentage points. Positive means richer than the surface predicts;
negative means cheaper.

**Surface fit / volatility surface** — A smooth surface fitted
across an option chain's IVs at every strike and expiration. The
scanner's IV+pp metric measures each option's distance from this
fit.

## The Greeks

**Greeks** — Partial derivatives describing how an option's price
responds to changes in its inputs. The common four below.

**Delta** — Rate of change of an option's price with respect to the
underlying's price. Roughly 0.5 at the money, approaches 1 deep in
the money, 0 deep out of the money.

**Gamma** — Rate of change of delta with respect to the underlying's
price. Highest near the money.

**Theta** — Rate of change of an option's price with respect to
time — almost always negative for option holders (time decay).

**Vega** — Rate of change of an option's price with respect to
volatility. Positive for both calls and puts.

**GEX (Gamma Exposure)** — Aggregate dealer gamma positioning across
an underlying's option chain. Positive GEX (dealers net long gamma)
tends to dampen price moves; negative GEX (dealers net short gamma)
tends to amplify them. The scanner plots GEX by strike.

## Spread strategies

**Spread** — Any multi-leg option position — combinations of long
and short calls/puts across strikes or expirations. Designed to
shape P&L (cap losses, define risk, target specific outcomes).

**Credit spread** — Multi-leg position opened for a net credit
(premium received). Defined-risk; max profit is the credit
received.

**Debit spread** — Multi-leg position opened for a net debit
(premium paid). Defined-risk; max loss is the debit paid.

**Vertical spread** — Two options of the same type (both calls or
both puts), same expiration, different strikes — one long, one
short. Examples: Bull Put (sell higher put, buy lower put —
credit, bullish), Bear Call (sell lower call, buy higher call —
credit, bearish), Bull Call (long lower call, short higher call —
debit, bullish), Bear Put (long higher put, short lower put —
debit, bearish).

**Iron condor** — Neutral credit spread: short a put spread below
spot plus short a call spread above spot, with no overlap. Profits
if the underlying stays in the middle range through expiration.

**Iron butterfly** — Neutral credit spread: short an ATM straddle
(short call + short put at same strike) with bought "wings" (long
OTM call + long OTM put) for defined risk. Profits if the
underlying pins the strike.

**Calendar spread / Diagonal spread** — Long a longer-dated option
and short a shorter-dated option (calendar uses the same strike;
diagonal uses different strikes). Profits from the front-leg time
decay running faster than the back-leg's.

**Straddle** — Long a call and a put at the same strike and
expiration. Profits if the underlying moves *enough* in either
direction. Long volatility position.

**Strangle** — Like a straddle but with the call and put at
different (typically OTM) strikes. Cheaper than a straddle but
needs a bigger move to profit. Long volatility position.

## Strategy metrics

**Breakeven (BE)** — Underlying price at expiration where a
position has zero profit and zero loss. Spreads have two
breakevens; single-leg options have one.

**POP (Probability of Profit)** — Estimated probability that a
position expires profitable, computed from Black-Scholes
assumptions about the underlying's distribution at expiration.

**EV (Expected Value)** — Probability-weighted average outcome.
For a spread: `POP × max profit − (1 − POP) × max loss`. Positive
EV = statistically favorable.

**R/R (Risk/Reward)** — Ratio of maximum profit to maximum loss.
Higher is better — a spread with R/R = 0.5 risks $2 to make $1.

## Market structure & data

**Market maker** — Entity that continuously quotes bid and ask
prices on a security to provide liquidity. Live NBBO bid/ask quotes
come from market makers; last-trade prices come from whoever last
actually traded.

**NBBO** — National Best Bid and Offer. The highest bid and lowest
ask across all US exchanges for a given security, consolidated in
real time. Schwab's developer API serves NBBO quotes; Yahoo Finance
returns last-refresh snapshots that can be stale.

**Open interest (OI)** — Total number of option contracts of a
specific strike/expiration currently outstanding (not yet closed or
expired). Higher OI generally means easier to trade in and out.

**Reg NMS** — Regulation National Market System. US SEC rules
requiring brokers to route orders to the venue offering the best
public price, creating the consolidated NBBO.

**Volume (Vol)** — Number of option contracts traded in a given
day for a specific strike/expiration. Resets daily. Distinct from
open interest, which is cumulative across all open positions.
