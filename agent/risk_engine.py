"""
risk_engine.py — Pre-trade risk guardrails for The Fifty Fund
=============================================================
Pure deterministic validation — no AI calls, no broker I/O.
The only side-effect is one DECISION_VALIDATED ledger event per call.

Entry point:
    from risk_engine import validate_trade
    ok, reason = validate_trade(decision, portfolio, account, ledger_path)
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import ledger as _ledger

logger = logging.getLogger(__name__)

# ── Rule constants ─────────────────────────────────────────────────────────────

MAX_POSITION_PCT    = 0.30       # no single position > 30% of portfolio value
CASH_BUFFER         = 2.00       # always keep at least $2 cash
MAX_TRADES_PER_DAY  = 3          # max BUY/SELL orders per UTC calendar day
DUPLICATE_WINDOW_S  = 15 * 60    # block same-ticker re-order within 15 minutes
MIN_ORDER_VALUE     = 1.00       # minimum dollar amount for any order


# ── Ledger reader ──────────────────────────────────────────────────────────────

def _read_events(ledger_path: Path) -> list[dict]:
    """Yield all events from ledger_path, oldest-first. Returns [] on any error."""
    if ledger_path is None or not ledger_path.exists():
        return []
    events = []
    with open(ledger_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _orders_today(ledger_path: Path) -> list[dict]:
    """Return ORDER_SUBMITTED events from the current UTC calendar day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [
        e for e in _read_events(ledger_path)
        if e.get("event_type") == _ledger.ORDER_SUBMITTED
        and e.get("timestamp", "").startswith(today)
    ]


def _recent_ticker_orders(ticker: str, ledger_path: Path) -> list[dict]:
    """Return ORDER_SUBMITTED events for `ticker` within DUPLICATE_WINDOW_S seconds."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_WINDOW_S)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    return [
        e for e in _read_events(ledger_path)
        if e.get("event_type") == _ledger.ORDER_SUBMITTED
        and e.get("payload", {}).get("ticker") == ticker
        and e.get("timestamp", "").rstrip("Z") >= cutoff
    ]


# ── Individual rule checkers ───────────────────────────────────────────────────

def _rule_min_order_value(decision: dict) -> tuple[bool, str]:
    if decision["action"] != "BUY":
        return True, "ok"
    amt = float(decision.get("dollar_amount") or 0)
    if amt < MIN_ORDER_VALUE:
        return False, (
            f"MIN_ORDER_VALUE: ${amt:.2f} is below the ${MIN_ORDER_VALUE:.2f} minimum"
        )
    return True, "ok"


def _rule_cash_buffer(decision: dict, portfolio: dict) -> tuple[bool, str]:
    if decision["action"] != "BUY":
        return True, "ok"
    amt  = float(decision.get("dollar_amount") or 0)
    cash = float(portfolio.get("cash", 0))
    remaining = cash - amt
    if remaining < CASH_BUFFER:
        return False, (
            f"CASH_BUFFER: ${cash:.2f} − ${amt:.2f} = ${remaining:.2f} "
            f"< required ${CASH_BUFFER:.2f} minimum"
        )
    return True, "ok"


def _rule_max_position(decision: dict, portfolio: dict) -> tuple[bool, str]:
    if decision["action"] != "BUY":
        return True, "ok"
    ticker = decision.get("ticker", "")
    amt    = float(decision.get("dollar_amount") or 0)
    pv     = float(portfolio.get("portfolio_value", 0))
    limit  = pv * MAX_POSITION_PCT

    # Support both list-of-dicts (reconciliation format) and symbol-keyed dict (legacy)
    positions = portfolio.get("positions", {})
    if isinstance(positions, list):
        existing_mv = next(
            (float(p.get("market_value", 0)) for p in positions if p.get("symbol") == ticker),
            0.0,
        )
    else:
        existing_mv = float(positions.get(ticker, {}).get("market_value", 0))

    if existing_mv + amt > limit:
        return False, (
            f"MAX_POSITION_PCT: existing ${existing_mv:.2f} + ${amt:.2f} "
            f"= ${existing_mv + amt:.2f} exceeds "
            f"{int(MAX_POSITION_PCT * 100)}% cap (${limit:.2f}) of ${pv:.2f} portfolio"
        )
    return True, "ok"


def _rule_position_exists(decision: dict, portfolio: dict) -> tuple[bool, str]:
    if decision["action"] != "SELL":
        return True, "ok"
    ticker   = decision.get("ticker", "")
    sell_qty = float(decision.get("qty") or 0)

    positions = portfolio.get("positions", {})
    if isinstance(positions, list):
        held_qty = next(
            (float(p.get("qty", 0)) for p in positions if p.get("symbol") == ticker),
            0.0,
        )
    else:
        held_qty = float(positions.get(ticker, {}).get("qty", 0))

    if held_qty <= 0:
        return False, f"POSITION_EXISTS: no open position in {ticker}"
    if sell_qty <= 0:
        return False, f"POSITION_EXISTS: sell qty must be > 0 (got {sell_qty})"
    if sell_qty > held_qty:
        return False, (
            f"POSITION_EXISTS: sell qty {sell_qty:.6f} exceeds "
            f"held {held_qty:.6f} {ticker}"
        )
    return True, "ok"


def _rule_pdt_safe(account: Any) -> tuple[bool, str]:
    """account may be an Alpaca REST object or a dict-like with daytrade_count."""
    if account is None:
        return True, "ok"
    try:
        count = int(
            getattr(account, "daytrade_count", None)
            or (account.get("daytrade_count", 0) if hasattr(account, "get") else 0)
        )
    except Exception:
        return True, "ok"
    if count >= 3:
        return False, f"PDT_SAFE: {count} day trades in rolling 5-day window (limit 3)"
    return True, "ok"


def _rule_max_trades_per_day(ledger_path: Path) -> tuple[bool, str]:
    if ledger_path is None:
        return True, "ok"
    count = len(_orders_today(ledger_path))
    if count >= MAX_TRADES_PER_DAY:
        return False, (
            f"MAX_TRADES_PER_DAY: {count} orders already placed today "
            f"(limit {MAX_TRADES_PER_DAY})"
        )
    return True, "ok"


def _rule_no_duplicate_order(decision: dict, ledger_path: Path) -> tuple[bool, str]:
    if ledger_path is None:
        return True, "ok"
    ticker = decision.get("ticker", "")
    if not ticker:
        return True, "ok"
    recent = _recent_ticker_orders(ticker, ledger_path)
    if recent:
        last_ts = recent[-1].get("timestamp", "?")
        return False, (
            f"NO_DUPLICATE_ORDER: {ticker} order already submitted at {last_ts} "
            f"(within {DUPLICATE_WINDOW_S // 60}-min window)"
        )
    return True, "ok"


# ── Main entry point ───────────────────────────────────────────────────────────

def validate_trade(
    decision: dict,
    portfolio: dict,
    account: Any,
    ledger_path: Path,
) -> tuple[bool, str]:
    """
    Run all pre-trade risk checks and log the outcome to the ledger.

    Args:
        decision:    Claude's decision dict (action, ticker, dollar_amount, qty, …)
        portfolio:   Reconciled portfolio dict (cash, portfolio_value, positions)
        account:     Alpaca account object or dict-like with daytrade_count.
                     Pass None to skip the PDT check (e.g. in tests).
        ledger_path: Path to data/ledger.jsonl.
                     Pass None to skip ledger-dependent checks (trades-per-day,
                     duplicate-order).

    Returns:
        (True, "ok")               — all rules passed
        (False, "<rule>: <msg>")   — first failing rule and its reason
    """
    action = (decision.get("action") or "HOLD").upper()
    ticker = decision.get("ticker") or ""

    if action == "HOLD":
        _log_validated(decision, True, "hold")
        return True, "hold"

    checks = [
        lambda: _rule_min_order_value(decision),
        lambda: _rule_cash_buffer(decision, portfolio),
        lambda: _rule_max_position(decision, portfolio),
        lambda: _rule_position_exists(decision, portfolio),
        lambda: _rule_pdt_safe(account),
        lambda: _rule_max_trades_per_day(ledger_path),
        lambda: _rule_no_duplicate_order(decision, ledger_path),
    ]

    for check in checks:
        passed, reason = check()
        if not passed:
            logger.warning("Risk check FAILED [%s %s]: %s", action, ticker, reason)
            _log_validated(decision, False, reason)
            return False, reason

    _log_validated(decision, True, "ok")
    return True, "ok"


def _log_validated(decision: dict, passed: bool, reason: str) -> None:
    """Write a DECISION_VALIDATED event, using the decision's cycle_id if present."""
    cycle_id = decision.get("cycle_id") or _ledger.generate_cycle_id()
    try:
        _ledger.log_event(
            cycle_id,
            _ledger.DECISION_VALIDATED,
            {
                "action": (decision.get("action") or "HOLD").upper(),
                "ticker": decision.get("ticker") or "",
                "passed": passed,
                "reason": reason,
            },
        )
    except Exception as exc:
        logger.warning("Could not write DECISION_VALIDATED to ledger: %s", exc)


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.WARNING)
    print("Running risk_engine self-test…\n")

    portfolio = {
        "cash":            45.00,
        "portfolio_value": 50.00,
        "positions": [
            {"symbol": "NVDA", "qty": 0.05, "market_value": 5.00, "avg_entry": 100.0}
        ],
    }

    # ── Write fake ORDER_SUBMITTED events directly to a temp ledger ────────────
    def _write_order(path: Path, ticker: str) -> None:
        record = {
            "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "cycle_id":   "test",
            "event_type": _ledger.ORDER_SUBMITTED,
            "payload":    {"ticker": ticker, "qty": 1, "side": "buy"},
        }
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    cases: list[tuple[str, dict, bool]] = [
        ("HOLD always passes",
         {"action": "HOLD"},
         True),
        ("BUY below min ($0.50)",
         {"action": "BUY", "ticker": "AAPL", "dollar_amount": 0.50},
         False),
        ("BUY would breach cash buffer",
         {"action": "BUY", "ticker": "AAPL", "dollar_amount": 44.00},
         False),
        ("BUY would exceed 30% position cap",
         {"action": "BUY", "ticker": "NVDA", "dollar_amount": 13.00},
         False),
        ("SELL ticker not held",
         {"action": "SELL", "ticker": "TSLA", "qty": 0.10},
         False),
        ("SELL qty exceeds held",
         {"action": "SELL", "ticker": "NVDA", "qty": 1.00},
         False),
        ("BUY valid",
         {"action": "BUY", "ticker": "AAPL", "dollar_amount": 5.00},
         True),
        ("SELL valid",
         {"action": "SELL", "ticker": "NVDA", "qty": 0.05},
         True),
    ]

    failures = 0
    for desc, decision, expected in cases:
        ok, reason = validate_trade(decision, portfolio, account=None, ledger_path=tmp_path)
        status = "PASS" if ok == expected else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"  [{status}] {desc}")
        if status == "FAIL" or not ok:
            print(f"          → valid={ok}, reason={reason}")

    # ── MAX_TRADES_PER_DAY: write 3 orders, 4th should be blocked ─────────────
    for _ in range(3):
        _write_order(tmp_path, "SPY")

    ok, reason = validate_trade(
        {"action": "BUY", "ticker": "MSFT", "dollar_amount": 5.00},
        portfolio, account=None, ledger_path=tmp_path,
    )
    status = "PASS" if not ok else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"  [{status}] MAX_TRADES_PER_DAY (3 already placed)")
    if status == "FAIL":
        print(f"          → valid={ok}, reason={reason}")

    # ── NO_DUPLICATE_ORDER: AAPL order just placed ────────────────────────────
    _write_order(tmp_path, "AAPL")
    ok, reason = validate_trade(
        {"action": "BUY", "ticker": "AAPL", "dollar_amount": 5.00},
        portfolio, account=None, ledger_path=tmp_path,
    )
    status = "PASS" if not ok else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"  [{status}] NO_DUPLICATE_ORDER (AAPL within 15 min)")
    if status == "FAIL":
        print(f"          → valid={ok}, reason={reason}")

    # ── PDT_SAFE: account with 3 day trades ───────────────────────────────────
    class _MockAccount:
        daytrade_count = 3

    ok, reason = validate_trade(
        {"action": "BUY", "ticker": "GOOGL", "dollar_amount": 5.00},
        portfolio, account=_MockAccount(), ledger_path=None,
    )
    status = "PASS" if not ok else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"  [{status}] PDT_SAFE (3 day trades)")
    if status == "FAIL":
        print(f"          → valid={ok}, reason={reason}")

    tmp_path.unlink(missing_ok=True)

    print(f"\n{'All tests passed ✓' if failures == 0 else f'{failures} test(s) FAILED'}")
    sys.exit(0 if failures == 0 else 1)
