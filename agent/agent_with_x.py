"""
agent_with_x.py — Unified runner for The Fifty Fund
====================================================
Wires together:
  - algomind_agent  →  market data, Claude decisions, Alpaca execution
  - reconciliation  →  authoritative broker-side portfolio state each cycle
  - risk_engine     →  deterministic pre-trade guardrails
  - ledger          →  append-only event log (one cycle_id per run_cycle call)
  - x_poster        →  X/Twitter auto-posting
  - substack_engine →  AI-authored Substack content

Entry point: python agent/agent_with_x.py

Scheduler (all times ET):
  - Every 30 minutes during NYSE hours → run_cycle()
  - 9:30am ET weekdays                 → morning market outlook
  - 4:05pm ET weekdays                 → EOD summary + daily Telegram summary
  - Every Friday 4:05pm                → weekly recap tweet + Substack review
  - 1st of each month                  → Substack monthly deep dive

Cycle flow (run_cycle):
  1. Generate cycle_id → log CYCLE_START
  2. reconciliation.get_reconciled_portfolio() → authoritative portfolio
  3. Fetch market data (yfinance/Alpaca)
  4. ask_claude() → log DECISION_PROPOSED
  5. risk_engine.validate_trade() → log DECISION_VALIDATED
     ↳ if blocked: log CYCLE_END, return
  6. BUY/SELL: log ORDER_SUBMITTED → execute_trade() → log ORDER_FILLED/REJECTED/ERROR
     HOLD: log to ai_log, continue
  7. Telegram + email → log POST_TELEGRAM
  8. X post (ORDER_FILLED + HOLD only) → log POST_X
  9. Dashboard update (ORDER_FILLED only) → log DASHBOARD_UPDATED
 10. Milestone check → log MILESTONE for each hit
 11. log CYCLE_END → push data.json to GitHub
"""

import json
import logging
import os
import time
from datetime import datetime, date, timezone
from pathlib import Path

import pytz
from dotenv import load_dotenv

# ── Internal modules ──────────────────────────────────────────────────────────
import algomind_agent as agent
from algomind_agent import append_ai_log
import daily_log_generator
import ledger         as ldr
import reconciliation
import risk_engine
import x_poster       as xp
import substack_engine as sub

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ET_ZONE     = pytz.timezone("America/New_York")
LEDGER_PATH = ldr.LEDGER_PATH

# ── State tracking (in-memory, reset on restart) ──────────────────────────────

_state = {
    "last_cycle_dt":            None,    # datetime of last trade cycle
    "morning_outlook_posted":   set(),   # set of dates (date objects)
    "eod_summary_posted":       set(),   # set of dates
    "weekly_recap_posted":      set(),   # set of dates (Friday dates)
    "monthly_deep_dive_posted": set(),   # set of (year, month) tuples
    "daily_summary_sent":       set(),   # set of dates
    "daily_log_generated":      set(),   # set of dates
    "trades_this_week":         [],      # list of decision dicts since Monday
    "first_trade_done":         False,   # True after first ever execution
}

CYCLE_INTERVAL_SECONDS = 30 * 60   # 30 minutes
RESTART_GUARD_SECONDS  = 15 * 60   # skip immediate re-run if last cycle < 15 min ago


# ── Portfolio format adapter ──────────────────────────────────────────────────

def _to_legacy_portfolio(reconciled: dict) -> dict:
    """
    Convert the reconciled portfolio (positions as list-of-dicts) to the
    symbol-keyed dict format expected by ask_claude() and update_dashboard_data().

    unrealized_pl is approximated as market_value − (avg_entry × qty).
    """
    positions = {}
    for p in reconciled.get("positions", []):
        cost_basis    = p["avg_entry"] * p["qty"]
        unrealized_pl = round(p["market_value"] - cost_basis, 2)
        positions[p["symbol"]] = {
            "qty":           p["qty"],
            "market_value":  p["market_value"],
            "unrealized_pl": unrealized_pl,
        }
    return {
        "cash":            reconciled["cash"],
        "portfolio_value": reconciled["portfolio_value"],
        "positions":       positions,
    }


# ── Persistent State Helpers ──────────────────────────────────────────────────

def _load_persistent_state() -> dict:
    """
    Read last_cycle_utc and last_outlook_date from data/state.json.
    Used as a fallback when the ledger has no CYCLE_START events yet
    (e.g. fresh deploy before any cycle has run).
    """
    result: dict = {"last_cycle_utc": None, "last_outlook_date": None}
    try:
        with open(agent._STATE_JSON_PATH) as fh:
            state = json.load(fh)
        result["last_cycle_utc"]    = state.get("last_cycle_utc")
        result["last_outlook_date"] = state.get("last_outlook_date")
    except Exception as exc:
        logger.warning("Could not load persistent state from state.json: %s", exc)
    return result


# ── Unified Run Cycle ─────────────────────────────────────────────────────────

def run_cycle() -> None:
    """
    Execute one complete trading + social cycle.

    Every meaningful step is bracketed by a ledger event so the full
    history is audit-able even if a crash occurs mid-cycle.
    """
    cycle_id        = ldr.generate_cycle_id()
    cycle_start_ts  = datetime.now(ET_ZONE)
    _cycle_action   = ""
    _cycle_ticker   = ""

    # ── 1. CYCLE_START ────────────────────────────────────────────────────────
    ldr.log_event(cycle_id, ldr.CYCLE_START, {
        "tickers":    agent.TICKERS,
        "started_at": cycle_start_ts.isoformat(),
    })
    logger.info("=== run_cycle() start  cycle_id=%s ===", cycle_id)

    try:
        # ── 2. Reconcile portfolio (authoritative broker-side state) ──────────
        reconciled, account = reconciliation.get_portfolio_and_account(cycle_id)
        portfolio = _to_legacy_portfolio(reconciled)   # for Claude + dashboard compat

        # ── 3. Market data ────────────────────────────────────────────────────
        market_data = agent.fetch_market_data(agent.TICKERS)
        if not market_data:
            logger.warning("No market data — skipping cycle.")
            ldr.log_event(cycle_id, ldr.CYCLE_END, {"result": "skipped: no market data"})
            return

        # ── 4. Claude decision ────────────────────────────────────────────────
        decision = agent.ask_claude(market_data, portfolio)

        ldr.log_event(cycle_id, ldr.DECISION_PROPOSED, {
            "action":         decision.get("action"),
            "ticker":         decision.get("ticker"),
            "dollar_amount":  decision.get("dollar_amount"),
            "qty":            decision.get("qty"),
            "confidence":     decision.get("confidence"),
            "reasoning":      (decision.get("reasoning") or "")[:300],
            "market_summary": decision.get("market_summary"),
        })

        action = (decision.get("action") or "HOLD").upper()
        ticker = decision.get("ticker") or ""
        _cycle_action = action
        _cycle_ticker  = ticker

        # ── 5. Risk validation (logs DECISION_VALIDATED internally) ──────────
        is_valid, validation_reason = risk_engine.validate_trade(
            decision, reconciled, account, LEDGER_PATH, cycle_id=cycle_id
        )

        if not is_valid:
            logger.warning("Trade blocked by risk engine: %s", validation_reason)
            try:
                append_ai_log(
                    f"BLOCKED {action} {ticker} — {validation_reason}",
                    ["risk-blocked", ticker.lower() if ticker else "hold"],
                )
            except Exception:
                pass
            # No trade, no social posts — end cycle cleanly
            ldr.log_event(cycle_id, ldr.CYCLE_END, {
                "result": "blocked",
                "reason": validation_reason,
            })
            return

        # ── 6. Execute trade ──────────────────────────────────────────────────
        result = None
        if action in ("BUY", "SELL"):
            # Log intent before hitting the broker
            ldr.log_event(cycle_id, ldr.ORDER_SUBMITTED, {
                "action":        action,
                "ticker":        ticker,
                "dollar_amount": decision.get("dollar_amount"),
                "qty":           decision.get("qty"),
            })

            result = agent.execute_trade(decision)
            decision["result"] = result

            if result.startswith("ERROR"):
                ldr.log_event(cycle_id, ldr.ERROR, {
                    "message": result,
                    "action":  action,
                    "ticker":  ticker,
                })
                try:
                    append_ai_log(f"ERROR — {result}", ["error"])
                except Exception:
                    pass
            elif result.startswith("REJECTED"):
                ldr.log_event(cycle_id, ldr.ORDER_REJECTED, {
                    "reason": result,
                    "ticker": ticker,
                })
                try:
                    append_ai_log(
                        f"REJECTED {action} {ticker} — {result}",
                        ["rejected", ticker.lower()],
                    )
                except Exception:
                    pass
            else:
                ldr.log_event(cycle_id, ldr.ORDER_FILLED, {
                    "result": result,
                    "ticker": ticker,
                    "action": action,
                })

        else:
            # HOLD — no order, just log
            result = f"HOLD — {decision.get('reasoning', '')}"
            decision["result"] = result
            try:
                append_ai_log(result[:200], ["hold", "market-analysis"])
            except Exception:
                pass

        # Timestamp is the same regardless of branch — set once here
        decision["timestamp"] = datetime.now(ET_ZONE).isoformat()

        # Social posting: HOLD decisions OR successfully filled BUY/SELL orders
        order_filled = action not in ("BUY", "SELL") or (
            result and not result.startswith(("REJECTED", "ERROR"))
        )

        # ── 7. Telegram + email ───────────────────────────────────────────────
        pv      = reconciled["portfolio_value"]
        pnl_pct = (pv - agent.STARTING_CASH) / agent.STARTING_CASH * 100
        tg_msg  = (
            f"🤖 *AlgoMind* | {datetime.now(ET_ZONE).strftime('%H:%M ET')}\n"
            f"Action    : *{decision.get('action', '?')}* {ticker}\n"
            f"Result    : {result}\n"
            f"Reasoning : {decision.get('reasoning', '')}\n"
            f"Confidence: {decision.get('confidence', '?')}/10\n"
            f"Portfolio : ${pv:.2f} ({pnl_pct:+.1f}% vs start)"
        )
        try:
            tg_sent = agent.send_telegram(tg_msg)
            agent.send_email(
                f"[TheFiftyFund] {decision.get('action', '?')} {ticker}",
                tg_msg,
            )
            ldr.log_event(cycle_id, ldr.POST_TELEGRAM, {
                "sent":    tg_sent,
                "message": tg_msg[:200],
            })
            try:
                if tg_sent:
                    append_ai_log("Telegram alert sent", ["telegram"])
                else:
                    append_ai_log("Telegram alert failed", ["telegram", "error"])
            except Exception:
                pass
        except Exception as exc:
            ldr.log_event(cycle_id, ldr.ERROR, {"message": f"Telegram failed: {exc}"})
            logger.error("Telegram send failed: %s", exc)

        # ── 8. X post (ORDER_FILLED + HOLD only) ──────────────────────────────
        tweet_text = None
        if order_filled:
            try:
                tweet_text = xp.post_trade_decision(decision)
                ldr.log_event(cycle_id, ldr.POST_X, {
                    "tweet": (tweet_text or "")[:280],
                })
                try:
                    append_ai_log(
                        f"Trade posted to X: {(tweet_text or '')[:100]}",
                        ["x-post", "trade"],
                    )
                except Exception:
                    pass
            except Exception as exc:
                ldr.log_event(cycle_id, ldr.ERROR, {"message": f"X post failed: {exc}"})
                logger.error("X post failed: %s", exc)
        else:
            logger.info("X post skipped — result was: %s", (result or "")[:80])

        # ── 9. Dashboard update (ORDER_FILLED BUY/SELL only) ──────────────────
        if action in ("BUY", "SELL") and order_filled:
            try:
                agent.update_dashboard_data(
                    decision, result, portfolio, x_post_text=tweet_text
                )
                ldr.log_event(cycle_id, ldr.DASHBOARD_UPDATED, {
                    "portfolio_value": pv,
                    "action":          action,
                    "ticker":          ticker,
                })
            except Exception as exc:
                ldr.log_event(cycle_id, ldr.ERROR, {
                    "message": f"Dashboard update failed: {exc}",
                })
                logger.error("Dashboard update failed (trade was still executed): %s", exc)

        # ── 10. First trade flag ──────────────────────────────────────────────
        is_first_trade = False
        if action in ("BUY", "SELL") and order_filled and not _state["first_trade_done"]:
            _state["first_trade_done"] = True
            is_first_trade = True

        # ── 11. Milestone check ───────────────────────────────────────────────
        try:
            # Only fetch a fresh snapshot when a trade changed the portfolio.
            # For HOLD (or rejected orders) the pre-cycle snapshot is still current.
            if action in ("BUY", "SELL") and order_filled:
                fresh = reconciliation.get_reconciled_portfolio(cycle_id)
            else:
                fresh = reconciled
            fresh_pv = fresh["portfolio_value"]

            first_hit = xp.check_and_post_milestones(
                portfolio_value=fresh_pv,
                first_trade=is_first_trade,
            )
            for key in first_hit:
                ldr.log_event(cycle_id, ldr.MILESTONE, {"key": key, "portfolio_value": fresh_pv})
                try:
                    append_ai_log(f"Milestone posted to X: {key}", ["x-post", "milestone"])
                except Exception:
                    pass

            # Substack milestone posts (second pass, first_trade=False avoids double-fire)
            newly_hit = xp.check_and_post_milestones(
                portfolio_value=fresh_pv,
                first_trade=False,
            )
            for key in newly_hit:
                try:
                    sub.generate_milestone_post(key, fresh)
                except Exception as exc:
                    ldr.log_event(cycle_id, ldr.ERROR, {
                        "message": f"Substack milestone post failed: {exc}",
                    })

        except Exception as exc:
            ldr.log_event(cycle_id, ldr.ERROR, {"message": f"Milestone check failed: {exc}"})
            logger.warning("Milestone check failed: %s", exc)

        # ── 12. Accumulate weekly trades ──────────────────────────────────────
        if action in ("BUY", "SELL") and order_filled:
            _state["trades_this_week"].append(decision)

        logger.info("run_cycle() complete: %s", result)

    except Exception as exc:
        ldr.log_event(cycle_id, ldr.ERROR, {"message": str(exc), "fatal": True})
        logger.error("run_cycle() error: %s", exc, exc_info=True)
        agent.send_telegram(f"⚠️ AlgoMind run_cycle error:\n{exc}")

    finally:
        # ── 13. CYCLE_END ─────────────────────────────────────────────────────
        duration_s = (datetime.now(ET_ZONE) - cycle_start_ts).total_seconds()
        ldr.log_event(cycle_id, ldr.CYCLE_END, {
            "action":     _cycle_action,
            "ticker":     _cycle_ticker,
            "duration_s": round(duration_s, 1),
        })

        # ── 14. Persist cycle timestamp → push if dirty ───────────────────────
        try:
            agent._update_agent_state("last_cycle_utc", datetime.now(ET_ZONE).isoformat())
        except Exception:
            pass

        if agent._dashboard_dirty:
            try:
                agent.push_dashboard_to_github(_cycle_action or "ai-log", _cycle_ticker)
            except Exception as exc:
                logger.error("End-of-cycle dashboard push failed: %s", exc)
            agent._dashboard_dirty = False


# ── Scheduled Event Handlers ──────────────────────────────────────────────────

def _handle_morning_outlook(market_data: dict) -> None:
    """Post morning market outlook once per trading day at open."""
    today = date.today()
    if today in _state["morning_outlook_posted"]:
        return
    cid = ldr.generate_cycle_id()
    try:
        tweet = xp.post_morning_outlook(market_data)
        ldr.log_event(cid, ldr.POST_X, {"context": "morning-outlook", "tweet": (tweet or "")[:280]})
        _state["morning_outlook_posted"].add(today)
        logger.info("Morning outlook posted for %s.", today)
        try:
            append_ai_log(
                f"Morning outlook posted to X: {(tweet or '')[:100]}",
                ["x-post", "morning-outlook"],
            )
        except Exception:
            pass
    except Exception as exc:
        ldr.log_event(cid, ldr.ERROR, {"message": f"Morning outlook failed: {exc}"})
        logger.error("Morning outlook post failed: %s", exc)
        return
    # Persist so redeploys don't re-post the outlook today
    try:
        agent._update_agent_state("last_outlook_date", today.isoformat())
    except Exception:
        pass


def _handle_eod(portfolio: dict) -> None:
    """Post EOD tweet, Telegram daily summary, and (Fridays) weekly recap + Substack."""
    today  = date.today()
    now_et = datetime.now(ET_ZONE)
    cid    = ldr.generate_cycle_id()

    # EOD tweet
    if today not in _state["eod_summary_posted"]:
        try:
            xp.post_eod_summary(portfolio)
            ldr.log_event(cid, ldr.POST_X, {"context": "eod-summary"})
            _state["eod_summary_posted"].add(today)
        except Exception as exc:
            ldr.log_event(cid, ldr.ERROR, {"message": f"EOD summary post failed: {exc}"})
            logger.error("EOD summary post failed: %s", exc)

    # Telegram daily summary
    if today not in _state["daily_summary_sent"]:
        try:
            agent.send_daily_summary()
            ldr.log_event(cid, ldr.POST_TELEGRAM, {"context": "daily-summary"})
            _state["daily_summary_sent"].add(today)
        except Exception as exc:
            ldr.log_event(cid, ldr.ERROR, {"message": f"Daily summary failed: {exc}"})
            logger.error("Daily summary failed: %s", exc)

    # Daily build log (after all other EOD work is done)
    if today not in _state["daily_log_generated"]:
        try:
            written = daily_log_generator.generate_daily_logs(include_today=True)
            _state["daily_log_generated"].add(today)
            if written:
                logger.info("Daily build log(s) generated: %s", [d.isoformat() for d in written])
        except Exception as exc:
            logger.error("Daily log generation failed: %s", exc)

    # Friday: weekly recap tweet + Substack review
    if now_et.weekday() == 4 and today not in _state["weekly_recap_posted"]:
        try:
            xp.post_weekly_recap(portfolio)
            sub.generate_weekly_review(portfolio, _state["trades_this_week"])
            ldr.log_event(cid, ldr.POST_X, {"context": "weekly-recap"})
            _state["weekly_recap_posted"].add(today)
            _state["trades_this_week"] = []
            logger.info("Weekly recap + Substack review posted.")
        except Exception as exc:
            ldr.log_event(cid, ldr.ERROR, {"message": f"Weekly recap failed: {exc}"})
            logger.error("Weekly recap failed: %s", exc)


def _handle_monthly_deep_dive(portfolio: dict) -> None:
    """Generate Substack monthly deep dive on the 1st of each month."""
    now_et = datetime.now(ET_ZONE)
    key    = (now_et.year, now_et.month)
    if now_et.day == 1 and key not in _state["monthly_deep_dive_posted"]:
        cid = ldr.generate_cycle_id()
        try:
            sub.generate_monthly_deep_dive(portfolio)
            ldr.log_event(cid, ldr.POST_X, {"context": "monthly-deep-dive", "month": f"{key[0]}-{key[1]:02d}"})
            _state["monthly_deep_dive_posted"].add(key)
            logger.info("Monthly deep dive posted: %s/%s", *key)
        except Exception as exc:
            ldr.log_event(cid, ldr.ERROR, {"message": f"Monthly deep dive failed: {exc}"})
            logger.error("Monthly deep dive failed: %s", exc)


# ── Main Scheduler Loop ───────────────────────────────────────────────────────

def start() -> None:
    """
    Start the unified scheduler loop.  Polls every 60 seconds and dispatches
    events based on ET market time.
    """
    logger.info("The Fifty Fund — AlgoMind with X + Substack started.")
    agent.send_telegram(
        "🤖 *The Fifty Fund* is online.\n"
        "AlgoMind is scanning the market. First cycle begins at next 30-min mark."
    )

    # ── Seed cycle guard from ledger (primary) ────────────────────────────────
    # If the ledger has a recent CYCLE_START, honour the 30-min interval to
    # prevent duplicate runs immediately after a Railway redeploy.
    last_ledger_cycle = ldr.get_last_cycle()
    if last_ledger_cycle:
        try:
            ts_str  = last_ledger_cycle["timestamp"].rstrip("Z")
            last_dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            last_dt = last_dt.astimezone(ET_ZONE)
            elapsed = (datetime.now(ET_ZONE) - last_dt).total_seconds()
            if elapsed < RESTART_GUARD_SECONDS:
                _state["last_cycle_dt"] = last_dt
                logger.info(
                    "Startup (ledger): last cycle was %.0fs ago — "
                    "restart guard active for another %.0fs.",
                    elapsed, CYCLE_INTERVAL_SECONDS - elapsed,
                )
            elif elapsed < CYCLE_INTERVAL_SECONDS:
                # Still within the 30-min window; wait out the remainder
                _state["last_cycle_dt"] = last_dt
                logger.info(
                    "Startup (ledger): last cycle %.0fs ago — "
                    "waiting %.0fs before next cycle.",
                    elapsed, CYCLE_INTERVAL_SECONDS - elapsed,
                )
            else:
                logger.info(
                    "Startup (ledger): last cycle was %.0fs ago — "
                    "running normally.",
                    elapsed,
                )
        except Exception as exc:
            logger.warning("Could not parse last cycle from ledger: %s", exc)

    # ── Fallback: seed from data.json if ledger gave nothing ─────────────────
    _pstate = _load_persistent_state()

    if _state["last_cycle_dt"] is None and _pstate["last_cycle_utc"]:
        try:
            last_dt = datetime.fromisoformat(_pstate["last_cycle_utc"])
            if last_dt.tzinfo is None:
                last_dt = ET_ZONE.localize(last_dt)
            _state["last_cycle_dt"] = last_dt
            logger.info("Startup (data.json fallback): last cycle %s.", last_dt)
        except Exception as exc:
            logger.warning("Could not parse last_cycle_utc from data.json: %s", exc)

    if _pstate["last_outlook_date"]:
        try:
            _state["morning_outlook_posted"].add(
                date.fromisoformat(_pstate["last_outlook_date"])
            )
            logger.info(
                "Startup: morning outlook already posted %s — will skip today.",
                _pstate["last_outlook_date"],
            )
        except Exception as exc:
            logger.warning("Could not parse last_outlook_date: %s", exc)

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        now_et     = datetime.now(ET_ZONE)
        today      = now_et.date()
        is_weekday = now_et.weekday() < 5

        # ── Lightweight portfolio snapshot for EOD/monthly handlers ──────────
        market_data = {}
        portfolio   = {}
        try:
            if is_weekday:
                market_data  = agent.fetch_market_data(agent.TICKERS)
                snapshot_cid = ldr.generate_cycle_id()
                portfolio    = _to_legacy_portfolio(reconciliation.get_reconciled_portfolio(snapshot_cid))
        except Exception as exc:
            logger.warning("Could not fetch market snapshot: %s", exc)

        market_open     = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        market_close    = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
        in_market_hours = is_weekday and market_open <= now_et < market_close

        # ── Morning outlook at 9:30am ET ──────────────────────────────────────
        if is_weekday and now_et >= market_open and today not in _state["morning_outlook_posted"]:
            if market_data:
                _handle_morning_outlook(market_data)

        # ── Trade cycle every 30 minutes during market hours ──────────────────
        if in_market_hours:
            elapsed = (
                (now_et - _state["last_cycle_dt"]).total_seconds()
                if _state["last_cycle_dt"] is not None
                else CYCLE_INTERVAL_SECONDS   # force run on first entry
            )
            if elapsed >= CYCLE_INTERVAL_SECONDS:
                run_cycle()
                _state["last_cycle_dt"] = now_et

        # ── EOD events at 4:05pm ET on weekdays ──────────────────────────────
        eod_threshold = now_et.replace(hour=16, minute=5, second=0, microsecond=0)
        if is_weekday and now_et >= eod_threshold and portfolio:
            _handle_eod(portfolio)

        # ── Monthly deep dive on the 1st ──────────────────────────────────────
        if is_weekday and portfolio:
            _handle_monthly_deep_dive(portfolio)

        time.sleep(60)   # poll every minute


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start()
