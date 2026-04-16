"""
reconciliation.py — Live portfolio reconciliation for The Fifty Fund
====================================================================
Fetches the authoritative portfolio state directly from Alpaca and
compares it against the last snapshot stored in the event ledger.

This is the ONLY place that should talk to Alpaca for portfolio/account
state. All other modules should call get_reconciled_portfolio() at the
start of each cycle instead of querying Alpaca independently.

Portfolio schema returned:
    {
      "cash":            float,
      "portfolio_value": float,
      "positions": [
        {
          "symbol":       str,
          "qty":          float,
          "market_value": float,
          "avg_entry":    float,
        },
        ...
      ],
    }
"""

import json
import logging
import os
from pathlib import Path

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

import ledger as _ledger

load_dotenv()

logger = logging.getLogger(__name__)

# ── Alpaca client ──────────────────────────────────────────────────────────────

_alpaca = tradeapi.REST(
    os.getenv("ALPACA_API_KEY", ""),
    os.getenv("ALPACA_SECRET_KEY", ""),
    os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
    api_version="v2",
)

_LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.jsonl"


# ── Alpaca fetch ───────────────────────────────────────────────────────────────

def _fetch_from_alpaca() -> tuple[dict, object]:
    """
    Pull account + open positions from Alpaca.

    Returns:
        (portfolio_dict, account_object)
        portfolio_dict uses the canonical reconciled schema.
    """
    account   = _alpaca.get_account()
    positions = _alpaca.list_positions()

    pos_list = [
        {
            "symbol":       p.symbol,
            "qty":          float(p.qty),
            "market_value": round(float(p.market_value), 2),
            "avg_entry":    round(float(p.avg_entry_price), 4),
        }
        for p in positions
    ]

    portfolio = {
        "cash":            round(float(account.cash), 2),
        "portfolio_value": round(float(account.portfolio_value), 2),
        "positions":       pos_list,
    }
    return portfolio, account


# ── Drift detection ────────────────────────────────────────────────────────────

def _last_snapshot() -> dict | None:
    """Return the portfolio payload from the most-recent RECONCILIATION ledger event."""
    if not _LEDGER_PATH.exists():
        return None
    last = None
    with open(_LEDGER_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("event_type") == _ledger.RECONCILIATION:
                    last = event.get("payload", {}).get("current")
            except json.JSONDecodeError:
                continue
    return last


def _compute_drift(prev: dict, curr: dict) -> dict:
    """
    Compare two portfolio snapshots and return a human-readable diff dict.
    Returns {} when nothing meaningful has changed.
    """
    drift: dict = {}

    pv_delta = round(curr["portfolio_value"] - prev["portfolio_value"], 4)
    if abs(pv_delta) >= 0.01:
        drift["portfolio_value_delta"] = pv_delta

    cash_delta = round(curr["cash"] - prev["cash"], 4)
    if abs(cash_delta) >= 0.01:
        drift["cash_delta"] = cash_delta

    prev_map = {p["symbol"]: p for p in prev.get("positions", [])}
    curr_map = {p["symbol"]: p for p in curr.get("positions", [])}
    prev_syms = set(prev_map)
    curr_syms = set(curr_map)

    added   = sorted(curr_syms - prev_syms)
    removed = sorted(prev_syms - curr_syms)
    if added:
        drift["positions_added"] = added
    if removed:
        drift["positions_removed"] = removed

    qty_changes: dict = {}
    for sym in prev_syms & curr_syms:
        delta = round(curr_map[sym]["qty"] - prev_map[sym]["qty"], 6)
        if abs(delta) >= 0.000001:
            qty_changes[sym] = delta
    if qty_changes:
        drift["qty_changes"] = qty_changes

    return drift


# ── Main entry point ───────────────────────────────────────────────────────────

def get_reconciled_portfolio() -> dict:
    """
    Fetch the authoritative live portfolio state from Alpaca, compare it
    against the last recorded reconciliation snapshot, and log a
    RECONCILIATION event noting any drift.

    Returns the canonical portfolio dict on success, or a zero-value
    fallback on Alpaca error (so callers never crash).
    """
    try:
        current, _ = _fetch_from_alpaca()
    except Exception as exc:
        logger.error("Reconciliation: Alpaca fetch failed — %s", exc)
        return {"cash": 0.0, "portfolio_value": 0.0, "positions": []}

    prev      = _last_snapshot()
    drift     = _compute_drift(prev, current) if prev else {}
    has_drift = bool(drift)

    if prev and has_drift:
        logger.info("Portfolio drift detected: %s", drift)
    elif prev:
        logger.debug("Reconciliation: no drift from last snapshot.")
    else:
        logger.info("Reconciliation: no prior snapshot — first run.")

    try:
        _ledger.log_event(
            _ledger.generate_cycle_id(),
            _ledger.RECONCILIATION,
            {
                "current":   current,
                "drift":     drift,
                "has_drift": has_drift,
            },
        )
    except Exception as exc:
        logger.warning("Could not write RECONCILIATION event to ledger: %s", exc)

    return current


def get_alpaca_account():
    """
    Return the raw Alpaca account object.
    Used by the risk engine for PDT (daytrade_count) checks.
    Returns None on error.
    """
    try:
        return _alpaca.get_account()
    except Exception as exc:
        logger.error("Could not fetch Alpaca account: %s", exc)
        return None


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print("Running reconciliation self-test…")
    print("Requires ALPACA_API_KEY + ALPACA_SECRET_KEY in environment.\n")

    portfolio = get_reconciled_portfolio()

    if portfolio["portfolio_value"] == 0.0 and not portfolio["positions"]:
        print("WARNING: got zero-value fallback — Alpaca credentials may be missing.")
        print("Set ALPACA_API_KEY and ALPACA_SECRET_KEY to test against a real account.")
    else:
        print(f"Portfolio value : ${portfolio['portfolio_value']:.2f}")
        print(f"Cash available  : ${portfolio['cash']:.2f}")
        positions = portfolio["positions"]
        print(f"Open positions  : {len(positions)}")
        for p in positions:
            print(
                f"  {p['symbol']:6s}  qty={p['qty']:.6f}  "
                f"avg=${p['avg_entry']:.4f}  MV=${p['market_value']:.2f}"
            )
        if not positions:
            print("  (none)")

        account = get_alpaca_account()
        if account:
            print(f"\nDay trade count : {getattr(account, 'daytrade_count', 'n/a')}")
            print(f"Account status  : {getattr(account, 'status', 'n/a')}")

        print(f"\nRECONCILIATION event logged to {_LEDGER_PATH}")

    print("\nSelf-test complete.")
    sys.exit(0)
