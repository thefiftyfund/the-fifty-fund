"""
ledger.py — Append-only event ledger for The Fifty Fund
========================================================
Events are written as single JSON lines to data/ledger.jsonl.
Each line is a complete, self-contained event record.

Usage:
    from ledger import log_event, generate_cycle_id

    cycle_id = generate_cycle_id()
    log_event(cycle_id, "CYCLE_START", {"tickers": ["AAPL", "TSLA"]})
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT   = Path(__file__).resolve().parent.parent
_DATA_DIR    = _REPO_ROOT / "data"
_LEDGER_PATH = _DATA_DIR / "ledger.jsonl"

# ── Event type constants ───────────────────────────────────────────────────────

CYCLE_START         = "CYCLE_START"
CYCLE_END           = "CYCLE_END"
DECISION_PROPOSED   = "DECISION_PROPOSED"
DECISION_VALIDATED  = "DECISION_VALIDATED"
ORDER_SUBMITTED     = "ORDER_SUBMITTED"
ORDER_FILLED        = "ORDER_FILLED"
ORDER_REJECTED      = "ORDER_REJECTED"
POST_X              = "POST_X"
POST_TELEGRAM       = "POST_TELEGRAM"
DASHBOARD_UPDATED   = "DASHBOARD_UPDATED"
ERROR               = "ERROR"
MILESTONE           = "MILESTONE"
RECONCILIATION      = "RECONCILIATION"


# ── Core helpers ──────────────────────────────────────────────────────────────

def generate_cycle_id() -> str:
    """Return a new UUID4 string to identify one trading cycle."""
    return str(uuid.uuid4())


def _now_utc() -> str:
    """Return the current UTC time as an ISO 8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Write ─────────────────────────────────────────────────────────────────────

def log_event(cycle_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """
    Append one event to data/ledger.jsonl.

    Each call opens the file in append mode, writes one JSON line, and
    closes immediately — so a mid-cycle crash never corrupts earlier lines.
    """
    _ensure_data_dir()
    record = {
        "timestamp": _now_utc(),
        "cycle_id":  cycle_id,
        "event_type": event_type,
        "payload":   payload,
    }
    with open(_LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


# ── Read ──────────────────────────────────────────────────────────────────────

def _iter_events():
    """Yield parsed event dicts from the ledger, oldest first."""
    if not _LEDGER_PATH.exists():
        return
    with open(_LEDGER_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def get_last_cycle() -> dict | None:
    """
    Return the most-recent CYCLE_START event, or None if the ledger is empty.
    Used on startup to detect whether a cycle was in progress before a restart.
    """
    last = None
    for event in _iter_events():
        if event.get("event_type") == CYCLE_START:
            last = event
    return last


def get_events_since(timestamp: str) -> list[dict]:
    """
    Return all events whose timestamp is strictly after `timestamp`.

    Args:
        timestamp: UTC ISO 8601 string, e.g. "2026-04-16T14:00:00.000Z"
    """
    cutoff = timestamp.rstrip("Z")
    result = []
    for event in _iter_events():
        ts = event.get("timestamp", "").rstrip("Z")
        if ts > cutoff:
            result.append(event)
    return result


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("Running ledger self-test …\n")

    cid = generate_cycle_id()
    print(f"cycle_id: {cid}")

    log_event(cid, CYCLE_START,        {"tickers": ["AAPL", "TSLA", "NVDA"]})
    log_event(cid, DECISION_PROPOSED,  {"action": "BUY", "ticker": "NVDA", "confidence": 8})
    log_event(cid, DECISION_VALIDATED, {"action": "BUY", "ticker": "NVDA", "passed": True})
    log_event(cid, ORDER_SUBMITTED,    {"ticker": "NVDA", "qty": 1, "side": "buy"})
    log_event(cid, ORDER_FILLED,       {"ticker": "NVDA", "filled_qty": 1, "filled_avg_price": 875.50})
    log_event(cid, POST_X,             {"tweet": "BUY NVDA 🟢 …"})
    log_event(cid, DASHBOARD_UPDATED,  {"portfolio_value": 51.25})
    log_event(cid, CYCLE_END,          {"result": "BUY NVDA filled", "duration_s": 4.2})

    print(f"\nWrote 8 events to {_LEDGER_PATH}\n")

    last = get_last_cycle()
    print(f"get_last_cycle() → cycle_id={last['cycle_id']}  ts={last['timestamp']}")

    # Events since 10 minutes before the epoch of the first event
    first_ts = "2000-01-01T00:00:00.000Z"
    events = get_events_since(first_ts)
    print(f"\nget_events_since('2000-…') → {len(events)} events:")
    for e in events:
        print(f"  [{e['timestamp']}] {e['event_type']}")

    print("\nSelf-test passed.")
    sys.exit(0)
