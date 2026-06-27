"""Persist placed put-sell trades — a single JSON log under
``options-scanner/trades/``.

One file, a list of trade records, so the Trades tab can show what was
placed, estimate P/L, and (later) close positions. Gitignored — this is
personal trade data, not shipped state.

Each record:
  id          unique short id
  ticker, strike, expiration (YYYY-MM-DD), quantity
  option_type "P" (cash-secured put) or "C" (covered call); default "P"
  credit      credit per share received at open
  status      "open" | "closing" | "closed" | "expired" | "assigned"
              ("closing" = a live buy-to-close order is working but unfilled)
  paper       bool — placed in Schwab paper/sandbox
  order_id    Schwab order id (None until placement is wired)
  opened_at   ISO-8601 timestamp
  close_order_id  Schwab id of the buy-to-close order (set while "closing")
  close_limit_px  per-share limit on that closing order
  close_qty   contracts the working closing order is buying back (≤ quantity)
  close_cost  per-share cost paid to close (None while open)
  closed_at   ISO-8601 (None while open)
  fill_spot   underlying spot captured at fill (paper: at placement)
  fill_delta  option delta captured at that same moment
  fill_iv     option implied vol captured at that same moment
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).parents[1] / "trades"
_FILE = _DIR / "trades.json"


def load() -> list[dict]:
    """All recorded trades, newest first. [] when none/corrupt."""
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    data.sort(key=lambda t: t.get("opened_at", ""), reverse=True)
    return data


def _write(trades: list[dict]) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(trades, indent=2), encoding="utf-8")


def add(trade: dict) -> dict:
    """Append a trade, filling id/opened_at/status defaults. Returns it.

    Callers supply ticker/strike/expiration/quantity/credit (and optionally
    order_id, paper); everything else is defaulted here.
    """
    rec = {
        "id": uuid.uuid4().hex[:12],
        "opened_at": datetime.now().isoformat(timespec="seconds"),
        "status": "open",
        "paper": True,
        "order_id": None,
        "close_cost": None,
        "closed_at": None,
        **trade,
    }
    trades = load()
    trades.append(rec)
    _write(trades)
    return rec


def update(trade_id: str, **fields) -> None:
    """Patch fields on the trade with matching id. No-op if absent."""
    trades = load()
    for t in trades:
        if t.get("id") == trade_id:
            t.update(fields)
            break
    _write(trades)


def remove(trade_id: str) -> None:
    """Delete the trade with matching id. No-op if absent."""
    _write([t for t in load() if t.get("id") != trade_id])
