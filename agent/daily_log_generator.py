"""
daily_log_generator.py — Auto-generate daily build logs from the event ledger
==============================================================================
Reads data/ledger.jsonl, groups events by UTC date, and writes a markdown
summary to docs/build_log/DAY_XXX.md for each day that does not already
have a log file.  Existing files (including hand-written DAY_001, DAY_002)
are never overwritten.

Entry point:
    from daily_log_generator import generate_daily_logs
    generate_daily_logs()               # all completed days with no existing file
    generate_daily_logs(include_today=True)  # also write today's in-progress log
"""

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT     = Path(__file__).resolve().parent.parent
_LEDGER_PATH   = _REPO_ROOT / "data" / "ledger.jsonl"
_BUILD_LOG_DIR = _REPO_ROOT / "docs" / "build_log"
_LAUNCH_DATE   = date(2026, 4, 15)   # Day 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _day_number(d: date) -> int:
    return (d - _LAUNCH_DATE).days + 1


def _log_path(d: date) -> Path:
    return _BUILD_LOG_DIR / f"DAY_{_day_number(d):03d}.md"


def _read_ledger() -> list[dict]:
    if not _LEDGER_PATH.exists():
        return []
    events: list[dict] = []
    with open(_LEDGER_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _group_by_date(events: list[dict]) -> dict[date, list[dict]]:
    groups: dict[date, list[dict]] = defaultdict(list)
    for event in events:
        ts = event.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)
            groups[dt.date()].append(event)
        except (ValueError, TypeError):
            continue
    return dict(groups)


def _ts(event: dict) -> str:
    """Return a short UTC timestamp string like '14:30 UTC'."""
    raw = event.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(raw.rstrip("Z")).replace(tzinfo=timezone.utc)
        return dt.strftime("%H:%M UTC")
    except (ValueError, TypeError):
        return raw[:16]


# ── Markdown builder ──────────────────────────────────────────────────────────

def _build_markdown(d: date, events: list[dict]) -> str:
    day_num  = _day_number(d)
    date_str = d.strftime("%B %d, %Y")

    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_type[e.get("event_type", "UNKNOWN")].append(e)

    cycles     = by_type.get("CYCLE_START", [])
    filled     = by_type.get("ORDER_FILLED", [])
    submitted  = by_type.get("ORDER_SUBMITTED", [])
    rejected   = by_type.get("ORDER_REJECTED", [])
    errors     = by_type.get("ERROR", [])
    x_posts    = by_type.get("POST_X", [])
    tg_posts   = by_type.get("POST_TELEGRAM", [])
    milestones = by_type.get("MILESTONE", [])
    recon      = by_type.get("RECONCILIATION", [])

    # End-of-day portfolio value from last RECONCILIATION event
    end_pv: float | None = None
    if recon:
        end_pv = recon[-1].get("payload", {}).get("current", {}).get("portfolio_value")

    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines += [
        f"# Day {day_num} — {date_str}",
        f"**Date:** {date_str}  ",
        "*Auto-generated from event ledger.*",
        "",
    ]

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("## Summary")
    lines.append(f"- **Cycles run:** {len(cycles)}")
    lines.append(f"- **Orders submitted:** {len(submitted)}"
                 + (f" ({len(filled)} filled, {len(rejected)} rejected)"
                    if submitted else ""))
    lines.append(f"- **Errors:** {len(errors)}")
    lines.append(f"- **X posts:** {len(x_posts)}")
    lines.append(f"- **Telegram messages:** {len(tg_posts)}")
    if end_pv is not None:
        lines.append(f"- **Portfolio value (EOD):** ${end_pv:.2f}")
    lines.append("")

    # ── Trades ────────────────────────────────────────────────────────────────
    if submitted:
        lines.append("## Trades")
        for e in submitted:
            p      = e.get("payload", {})
            action = p.get("action", "?")
            ticker = p.get("ticker", "?")
            dollar = p.get("dollar_amount")
            qty    = p.get("qty")
            if action == "BUY" and dollar is not None:
                detail = f"BUY ${float(dollar):.2f} {ticker}"
            elif action == "SELL" and qty is not None:
                detail = f"SELL {float(qty)} shares {ticker}"
            else:
                detail = f"{action} {ticker}"
            lines.append(f"- `{_ts(e)}`  {detail}")
        lines.append("")

    # ── Errors ────────────────────────────────────────────────────────────────
    if errors:
        lines.append("## Errors")
        for e in errors:
            msg = e.get("payload", {}).get("message", "(no message)")
            lines.append(f"- `{_ts(e)}`  {msg[:140]}")
        lines.append("")

    # ── X Posts ───────────────────────────────────────────────────────────────
    if x_posts:
        lines.append("## X Posts")
        for e in x_posts:
            p     = e.get("payload", {})
            ctx   = p.get("context", "trade")
            tweet = (p.get("tweet") or "")[:120]
            line  = f"- `{_ts(e)}`  [{ctx}]"
            if tweet:
                line += f"  {tweet}"
            lines.append(line)
        lines.append("")

    # ── Milestones ────────────────────────────────────────────────────────────
    if milestones:
        lines.append("## Milestones")
        for e in milestones:
            p   = e.get("payload", {})
            key = p.get("key", "?")
            pv  = p.get("portfolio_value")
            pv_str = f"  (${pv:.2f})" if pv else ""
            lines.append(f"- `{_ts(e)}`  {key}{pv_str}")
        lines.append("")

    return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

def generate_daily_logs(include_today: bool = False) -> list[date]:
    """
    Generate missing build logs for every date found in the ledger.

    Args:
        include_today: When True, also generate a log for the current UTC date
                       even though the day is still in progress.

    Returns:
        List of dates for which a new file was written.
    """
    _BUILD_LOG_DIR.mkdir(parents=True, exist_ok=True)

    events = _read_ledger()
    if not events:
        logger.info("daily_log_generator: ledger is empty — nothing to generate.")
        return []

    by_date  = _group_by_date(events)
    today    = datetime.now(timezone.utc).date()
    written: list[date] = []

    for d in sorted(by_date):
        if d == today and not include_today:
            continue
        if _day_number(d) < 1:
            continue   # before launch date
        out_path = _log_path(d)
        if out_path.exists():
            continue   # never overwrite — preserves manual logs

        content = _build_markdown(d, by_date[d])
        out_path.write_text(content, encoding="utf-8")
        logger.info("daily_log_generator: wrote %s", out_path.name)
        written.append(d)

    return written


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    written = generate_daily_logs(include_today=True)
    if written:
        print(f"Generated {len(written)} log(s): {[d.isoformat() for d in written]}")
    else:
        print("No new logs generated (all days already have files, or ledger is empty).")
    sys.exit(0)
