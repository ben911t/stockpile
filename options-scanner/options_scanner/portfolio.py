"""Portfolio scan: read a brokerage CSV and scan every open stock position."""

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Yahoo rate-limit handling for the scan loop — keep in sync with
# options_scanner.tabs.portfolio (the Streamlit path). When a fetch raises
# RateLimitError the scan waits and retries that ticker, up to _RL_MAX_RETRIES
# times, _RL_WAIT_SECONDS apart, rather than failing (or, in the CLI, crashing).
# Yahoo's throttle window often runs longer than a minute, so one short wait
# isn't enough. Because Yahoo throttles per-IP, once the wait clears every
# remaining ticker proceeds — but if one ticker burns its WHOLE budget and is
# still throttled, further waiting is futile: give up and fail the remaining
# throttled tickers fast. Worst case ≈ one ticker's full budget (4×30s), not
# minutes per ticker.
_RL_WAIT_SECONDS = 30   # seconds between rate-limit retries
_RL_MAX_RETRIES = 4     # per-ticker retries before concluding the IP is throttled


def detect_brokerage(content: bytes) -> str | None:
    """Identify brokerage format from the first 8 KB of a CSV.

    Returns a brokerage key or None if the format is not recognized.
    Fingerprints: Schwab = "Fees & Comm", Fidelity = "Run Date",
    Robinhood = "Trans Code" + "Activity Date", Merrill = "Symbol/ CUSIP",
    Stockpile = lowercase header row starting with "symbol".
    """
    try:
        text = content.decode("utf-8-sig", errors="replace")[:8192]
    except Exception:
        return None
    if "Fees & Comm" in text:
        return "schwab"
    if "Run Date" in text:
        return "fidelity"
    if "Trans Code" in text and "Activity Date" in text:
        return "robinhood"
    if "Symbol/ CUSIP" in text:
        return "merrill"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("symbol") and "action" in stripped.lower():
            return "stockpile"
        break
    return None


def _get_parser(brokerage: str):
    b = brokerage.lower()
    if b == "schwab":
        from stocks_shared.parsers.schwab import parse_all_transactions
    elif b == "robinhood":
        from stocks_shared.parsers.robinhood import parse_all_transactions
    elif b == "fidelity":
        from stocks_shared.parsers.fidelity import parse_all_transactions
    elif b == "merrill":
        from stocks_shared.parsers.merrill import parse_all_transactions
    elif b == "stockpile":
        from stocks_shared.parsers.stockpile import parse_all_transactions
    else:
        sys.exit(f"Unknown brokerage '{brokerage}'. "
                 "Supported: schwab, robinhood, fidelity, merrill, stockpile")
    return parse_all_transactions


def _exp_to_yf(exp_str: str) -> str:
    """Convert MM/DD/YYYY to YYYY-MM-DD."""
    m, d, y = exp_str.split("/")
    return f"{y}-{m}-{d}"


def _fetch_roll_chain(ticker: str, exp_yf: str, provider: str,
                      schwab_config: dict | None, schwab_client=None):
    """Fetch single-expiration chain for roll close-cost lookup."""
    if provider == "schwab":
        from stocks_shared.schwab_live import fetch_option_chain_schwab
        return fetch_option_chain_schwab(schwab_client, ticker, exp_yf)
    from stocks_shared.yahoo import fetch_option_chain
    return fetch_option_chain(ticker, exp_yf)


def get_portfolio(csv_path: str, brokerage: str,
                  include_closed: bool = False) -> list[dict]:
    """Return stock positions with associated open short options.

    With include_closed=False (default) only tickers with shares > 0 are
    returned. With include_closed=True every ticker that ever appeared in the
    CSV is returned (shares may be 0 for fully-exited positions).

    Each entry: {ticker, shares, open_calls, open_puts}
    """
    from stocks_shared.analysis import detect_open_positions

    parse = _get_parser(brokerage)
    ticker_txns, _ = parse(csv_path)

    positions = []
    for ticker, txns in sorted(ticker_txns.items()):
        # txns is sorted oldest-first by the parser. Walk in order so a
        # "Transfer In" (TDA->Schwab broker migration) acts as an authoritative
        # balance snapshot that resets the running total — any prior Buys/Sells
        # in the CSV are already baked into that transferred quantity.
        shares = 0.0
        for row in txns:
            _, action, opt_type, _, _, _, qty, *_ = row
            if opt_type != "Stock" or qty == "":
                continue
            q = float(qty)
            if action == "Transfer In":
                shares = q                      # snapshot reset, not delta
            elif action in ("Buy", "Reinvest Shares"):
                shares += q
            elif action == "Sell":
                shares -= q

        if not include_closed and shares <= 0.001:   # float-safe zero check
            continue

        open_opts = detect_open_positions(txns)
        positions.append({
            "ticker": ticker,
            "shares": max(0.0, shares),
            "open_calls": [o for o in open_opts if o["type"] == "Call"],
            "open_puts":  [o for o in open_opts if o["type"] == "Put"],
        })

    return positions


def scan_position(pos: dict, min_dte: int = 365, min_oi: int = 25,
                  max_delta: float = 0.70, provider: str = "yahoo",
                  schwab_config: dict | None = None) -> dict:
    """Fetch call chain and return scan results for one position.

    Returns dict: {position, error, df, spot, earnings_dates, roll_close_costs}
    roll_close_costs: {option_symbol: mid_price}
    """
    from options_scanner.chain import fetch_chain
    from options_scanner.iv_surface import compute_iv_excess
    from options_scanner.earnings import fetch_earnings_dates, annotate_earnings

    ticker = pos["ticker"]
    empty = {"position": pos, "error": None, "df": pd.DataFrame(),
             "spot": None, "earnings_dates": [], "roll_close_costs": {}}

    try:
        df = fetch_chain(ticker, opt_type="calls", min_dte=min_dte,
                         provider=provider, schwab_config=schwab_config)
    except ValueError as exc:
        return {**empty, "error": str(exc)}

    if df.empty:
        return {**empty, "error": f"No LEAPS calls found (min DTE {min_dte})"}

    df = compute_iv_excess(df)
    earnings_dates = fetch_earnings_dates(ticker)
    df = annotate_earnings(df, earnings_dates)

    spot = float(df["spot"].iloc[0])
    df = df[df["delta"].abs() <= max_delta].copy()

    # Build Schwab client once for all roll lookups (reuses cached instance)
    schwab_client = None
    if provider == "schwab" and pos["open_calls"]:
        from stocks_shared.schwab_live import get_client
        cfg = schwab_config or {}
        try:
            schwab_client = get_client(
                cfg.get("app_key", ""),
                cfg.get("app_secret", ""),
                cfg.get("callback_url", "https://127.0.0.1:8182/"),
                cfg.get("token_file", "~/.config/schwab-token.json"),
            )
        except ValueError as exc:
            log.warning("Schwab auth failed for roll lookup: %s", exc)

    # Look up close cost (mid) for each open short call
    roll_close_costs = {}
    for opt in pos["open_calls"]:
        exp_yf = _exp_to_yf(opt["expiration"])
        chain = _fetch_roll_chain(ticker, exp_yf, provider,
                                  schwab_config, schwab_client)
        if chain is None:
            continue
        row = chain.calls[chain.calls["strike"] == float(opt["strike"])]
        if row.empty:
            continue
        bid = float(row["bid"].iloc[0] or 0)
        ask = float(row["ask"].iloc[0] or 0)
        last = float(row["lastPrice"].iloc[0] or 0)
        roll_close_costs[opt["symbol"]] = (
            (bid + ask) / 2 if bid > 0 and ask > 0 else last
        )

    return {
        "position": pos,
        "error": None,
        "df": df,
        "spot": spot,
        "earnings_dates": earnings_dates,
        "roll_close_costs": roll_close_costs,
    }


def _scan_positions(positions: list[dict], min_dte: int, min_oi: int,
                    max_delta: float, provider: str,
                    schwab_config: dict | None):
    """Yield (position, result) for each position, with Yahoo rate-limit
    retry/backoff.

    Mirrors the Streamlit path (options_scanner.tabs.portfolio): a throttled
    ticker retries up to ``_RL_MAX_RETRIES`` times, ``_RL_WAIT_SECONDS`` apart;
    once any ticker exhausts its budget the IP is treated as persistently
    throttled and every remaining ticker fails fast. A generator so the caller
    can print each ticker's results as the scan streams (rather than blocking
    until the whole basket is done). ``RateLimitError`` is imported lazily so a
    Schwab-only run never pulls in the Yahoo/yfinance stack.
    """
    from stocks_shared.yahoo import RateLimitError

    rl_give_up = False  # a full retry budget didn't clear the throttle → stop
    for i, pos in enumerate(positions):
        ticker = pos["ticker"]
        log.info("Scanning %s (%d/%d)...", ticker, i + 1, len(positions))
        attempts = 0  # rate-limit retries used on THIS ticker
        while True:
            try:
                result = scan_position(pos, min_dte, min_oi, max_delta,
                                       provider=provider,
                                       schwab_config=schwab_config)
                break
            except RateLimitError as exc:
                if rl_give_up or attempts >= _RL_MAX_RETRIES:
                    # Retry budget exhausted (or a prior ticker already
                    # exhausted it). Yahoo throttles per-IP, so more waiting is
                    # futile — fail this ticker and the rest fast.
                    rl_give_up = True
                    result = {"position": pos,
                              "error": (f"{exc}. Yahoo is still throttling — "
                                        "rescan in a few minutes, or use "
                                        "--data-source schwab."),
                              "df": pd.DataFrame(), "spot": None,
                              "earnings_dates": [], "roll_close_costs": {}}
                    break
                attempts += 1
                log.warning("Yahoo rate limit — retry %d/%d for %s in %ds "
                            "(the scan will finish, just slower)...",
                            attempts, _RL_MAX_RETRIES, ticker, _RL_WAIT_SECONDS)
                time.sleep(_RL_WAIT_SECONDS)
        yield pos, result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )

    parser = argparse.ArgumentParser(
        description="Scan all open stock positions in a brokerage CSV."
    )
    parser.add_argument("--csv", required=True, metavar="FILE",
                        help="Path to brokerage CSV export")
    parser.add_argument("--brokerage", default="schwab",
                        choices=["schwab", "robinhood", "fidelity", "merrill"])
    parser.add_argument("--tickers", nargs="*", metavar="TICKER",
                        help="Only scan these tickers (default: all)")
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--min-oi",  type=int, default=25)
    parser.add_argument("--max-delta", type=float, default=0.70)
    parser.add_argument("--top",     type=int, default=5,
                        help="Max rows per position in terminal (default: 5)")
    parser.add_argument("--html", action="store_true",
                        help="Save a combined HTML portfolio report")
    parser.add_argument("--output-dir", default=None, metavar="DIR")
    parser.add_argument(
        "--data-source",
        dest="data_source",
        choices=["yahoo", "schwab"],
        default=None,
        help="Data source override (default: from config.toml or 'yahoo')",
    )
    args = parser.parse_args()

    from options_scanner.config import load_config, get_provider, get_schwab_config
    cfg = load_config()
    provider = args.data_source or get_provider(cfg)
    schwab_config = get_schwab_config(cfg)

    log.info("Parsing %s (%s)...", args.csv, args.brokerage)
    positions = get_portfolio(args.csv, args.brokerage)

    if args.tickers:
        want = {t.upper() for t in args.tickers}
        positions = [p for p in positions if p["ticker"] in want]

    if not positions:
        sys.exit("No open stock positions found.")

    print(f"\nFound {len(positions)} position(s): "
          f"{', '.join(p['ticker'] for p in positions)}")
    if provider == "schwab":
        print("  Data source: Schwab (real-time)")
    print()

    from options_scanner.display.cli import print_results

    results = []
    for pos, result in _scan_positions(positions, args.min_dte, args.min_oi,
                                       args.max_delta, provider, schwab_config):
        results.append(result)
        ticker = pos["ticker"]

        if result["error"]:
            print(f"  {ticker}: {result['error']}\n")
            continue

        for opt in pos["open_calls"]:
            close = result["roll_close_costs"].get(opt["symbol"])
            close_str = f"  close mid: ${close:.2f}" if close else ""
            print(f"  Open call: {opt['symbol']} "
                  f"({opt['contracts']} contract(s)){close_str}")

        roll_close = None
        if pos["open_calls"]:
            first = pos["open_calls"][0]
            roll_close = result["roll_close_costs"].get(first["symbol"])

        print_results(
            result["df"],
            ticker,
            result["spot"],
            result["earnings_dates"],
            "call",
            roll_close_cost=roll_close,
            min_oi=args.min_oi,
            top_n=args.top,
            buy=False,
        )

    if args.html:
        from options_scanner.report import save_portfolio_html
        output_dir = (Path(args.output_dir) if args.output_dir
                      else Path(__file__).parents[1] / "output")
        filename = f"portfolio_{date.today().strftime('%Y%m%d')}.html"
        output_path = output_dir / filename
        save_portfolio_html(results, args.csv, output_path, args.min_oi, args.top)
        print(f"\n  HTML report: {output_path}")


if __name__ == "__main__":
    main()
