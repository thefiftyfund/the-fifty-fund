"""
x_poster.py — X / Twitter auto-poster for The Fifty Fund
=========================================================
Posts trade decisions, morning outlooks, EOD summaries, weekly recaps,
and milestone tweets to @TheFiftyFund via the Twitter v2 API (tweepy).

Milestone tracking is persisted to milestones_hit.json at the repo root.

All API credentials are loaded from environment variables / .env file.
"""

import json
import logging
import os
import textwrap
from datetime import datetime
from pathlib import Path

import pytz
import tweepy
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

X_API_KEY       = os.getenv("X_API_KEY", "")
X_API_SECRET    = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN  = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET = os.getenv("X_ACCESS_SECRET", "")
X_BEARER_TOKEN  = os.getenv("X_BEARER_TOKEN", "")

STARTING_CASH   = 50.00
ET_ZONE         = pytz.timezone("America/New_York")
TWEET_MAX_CHARS = 280

# Milestone definitions: (key, threshold_value, display_label)
MILESTONE_DEFS = [
    ("first_trade",  None,   "I just executed my very first trade"),
    ("first_profit", 50.01,  "I am in profit for the first time"),
    ("plus_10_pct",  55.00,  "I've grown the fund 10% — from $50 to $55"),
    ("plus_25_pct",  62.50,  "I've grown the fund 25% — from $50 to $62.50"),
    ("plus_50_pct",  75.00,  "I've grown the fund 50% — from $50 to $75"),
    ("plus_100_pct", 100.00, "I've doubled the fund — from $50 to $100"),
]

# Milestones file at repo root (one level up from agent/)
_REPO_ROOT      = Path(__file__).resolve().parent.parent
MILESTONES_FILE = _REPO_ROOT / "milestones_hit.json"

# data.json is pushed to GitHub after every cycle — milestone state stored
# there too so Railway redeploys don't re-fire milestones.
_DATA_JSON_PATH = _REPO_ROOT / "docs" / "data.json"


# ── Tweepy Client ─────────────────────────────────────────────────────────────

def _get_client() -> tweepy.Client | None:
    """
    Build and return a tweepy v2 Client.  Returns None (with a warning) if
    any required credential is missing, so the rest of the app continues
    running without X posting.
    """
    required = [X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET]
    if not all(required):
        logger.warning("X API credentials incomplete — X posting disabled.")
        return None
    return tweepy.Client(
        bearer_token=X_BEARER_TOKEN or None,
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
        wait_on_rate_limit=True,
    )


# ── Core Post Helper ──────────────────────────────────────────────────────────

def _post(text: str) -> bool:
    """
    Truncate text to 280 chars and post to X.

    Returns True on success, False otherwise.
    """
    client = _get_client()
    if client is None:
        return False

    # Truncate gracefully, preserving the last word boundary where possible
    if len(text) > TWEET_MAX_CHARS:
        text = text[: TWEET_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"

    try:
        client.create_tweet(text=text)
        logger.info("Posted to X: %s…", text[:60])
        return True
    except tweepy.TweepyException as exc:
        logger.error("X post failed: %s", exc)
        return False


# ── Milestone Tracker ─────────────────────────────────────────────────────────

def _load_milestones() -> dict:
    """
    Load milestone state. Prefers data.json (pushed to GitHub, survives
    Railway redeploys) over the local milestones_hit.json fallback.
    """
    default = {key: False for key, _, _ in MILESTONE_DEFS}

    # Primary: data.json is git-tracked and pushed after every cycle
    if _DATA_JSON_PATH.exists():
        try:
            with open(_DATA_JSON_PATH) as f:
                dashboard = json.load(f)
            stored = dashboard.get("milestones_hit", {})
            if stored:
                for key in default:
                    default[key] = bool(stored.get(key, False))
                return default
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read milestones from data.json: %s", exc)

    # Fallback: legacy local file (useful in local dev)
    if MILESTONES_FILE.exists():
        try:
            with open(MILESTONES_FILE) as f:
                data = json.load(f)
            for key in default:
                data.setdefault(key, False)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read milestones file: %s — using defaults.", exc)

    return default


def _save_milestones(data: dict) -> None:
    """
    Persist milestone state to both data.json and the legacy local file.
    data.json is pushed to GitHub after each cycle, so milestones survive
    Railway redeploys.
    """
    # Primary: embed in data.json so the cycle-end push includes it
    if _DATA_JSON_PATH.exists():
        try:
            with open(_DATA_JSON_PATH) as f:
                dashboard = json.load(f)
            dashboard["milestones_hit"] = data
            with open(_DATA_JSON_PATH, "w") as f:
                json.dump(dashboard, f, indent=2)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not write milestones to data.json: %s", exc)

    # Legacy: also write standalone file for local dev convenience
    try:
        with open(MILESTONES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        logger.error("Could not save milestones file: %s", exc)


# ── Public Posting Functions ──────────────────────────────────────────────────

def post_trade_decision(decision: dict) -> str:
    """
    Post a tweet announcing a trade decision with full AI reasoning.

    Args:
        decision: The decision dict returned by algomind_agent.ask_claude(),
                  with a "result" key added by execute_trade().

    Returns:
        The tweet text that was built (regardless of posting success) so
        callers can store it in the dashboard's x_post field.
    """
    action    = (decision.get("action") or "HOLD").upper()
    ticker    = decision.get("ticker") or ""
    reasoning = decision.get("reasoning") or ""
    confidence = decision.get("confidence") or "?"
    result    = decision.get("result") or ""
    now_et    = datetime.now(ET_ZONE).strftime("%H:%M ET")

    if action == "BUY":
        headline = f"BUY {ticker} 🟢"
    elif action == "SELL":
        headline = f"SELL {ticker} 🔴"
    else:
        headline = "HOLD ⚪"

    # Build tweet, keeping it readable within 280 chars
    tweet = (
        f"[AlgoMind | {now_et}] {headline}\n\n"
        f"{textwrap.shorten(reasoning, width=170, placeholder='...')}\n\n"
        f"Confidence: {confidence}/10\n"
        f"#TheFiftyFund #AITrading #AlgoTrading"
    )
    _post(tweet)
    return tweet


def post_morning_outlook(market_data: dict) -> str:
    """
    Post a brief morning market outlook tweet at market open.

    Args:
        market_data: Dict returned by algomind_agent.fetch_market_data().

    Returns:
        The tweet text that was built, for ai_log capture by the caller.
    """
    now_et = datetime.now(ET_ZONE).strftime("%A, %b %d")

    # Highlight the top mover by absolute change_pct
    if market_data:
        top = max(market_data.items(), key=lambda x: abs(x[1]["change_pct"]))
        top_sym, top_d = top
        mover_line = (
            f"Top mover: {top_sym} {top_d['change_pct']:+.2f}% "
            f"(RSI {top_d['rsi']:.0f})"
        )
    else:
        mover_line = "Market data unavailable."

    # Count oversold / overbought tickers
    oversold   = [s for s, d in market_data.items() if d["rsi"] < 30]
    overbought = [s for s, d in market_data.items() if d["rsi"] > 70]

    lines = [
        f"☀️ Market open — {now_et}",
        f"{mover_line}",
    ]
    if oversold:
        lines.append(f"Oversold  (RSI<30): {', '.join(oversold)}")
    if overbought:
        lines.append(f"Overbought (RSI>70): {', '.join(overbought)}")
    lines.append("\nScanning for signals… #TheFiftyFund #AITrading")

    tweet = "\n".join(lines)
    _post(tweet)
    return tweet


def post_eod_summary(portfolio: dict) -> bool:
    """
    Post an end-of-day summary tweet.

    Args:
        portfolio: Dict returned by algomind_agent.get_portfolio().
    """
    pv      = portfolio["portfolio_value"]
    cash    = portfolio["cash"]
    pnl     = pv - STARTING_CASH
    pnl_pct = (pnl / STARTING_CASH) * 100
    now_et  = datetime.now(ET_ZONE).strftime("%b %d")

    positions_count = len(portfolio.get("positions", {}))

    tweet = (
        f"📊 EOD Summary | {now_et}\n\n"
        f"Portfolio: ${pv:.2f} ({pnl_pct:+.1f}% vs start)\n"
        f"Cash: ${cash:.2f} | Positions: {positions_count}\n"
        f"Total P&L: ${pnl:+.2f}\n\n"
        f"#TheFiftyFund #AITrading"
    )
    return _post(tweet)


def post_weekly_recap(portfolio: dict) -> bool:
    """
    Post a weekly performance recap tweet every Friday, comparing the fund
    against SPY's weekly performance.

    Args:
        portfolio: Dict returned by algomind_agent.get_portfolio().
    """
    pv      = portfolio["portfolio_value"]
    pnl     = pv - STARTING_CASH
    pnl_pct = (pnl / STARTING_CASH) * 100

    # Fetch SPY weekly performance
    spy_change_pct = 0.0
    try:
        spy_hist = yf.Ticker("SPY").history(period="5d")
        if len(spy_hist) >= 2:
            spy_start = spy_hist["Close"].iloc[0]
            spy_end   = spy_hist["Close"].iloc[-1]
            spy_change_pct = (spy_end - spy_start) / spy_start * 100
    except Exception as exc:
        logger.warning("Could not fetch SPY weekly data: %s", exc)

    # Determine if we beat the market
    vs_spy = pnl_pct - spy_change_pct
    beat   = "beat" if vs_spy > 0 else "trailed"
    icon   = "🏆" if vs_spy > 0 else "📉"

    tweet = (
        f"{icon} Weekly Recap | Week ending {datetime.now(ET_ZONE).strftime('%b %d')}\n\n"
        f"The Fifty Fund: {pnl_pct:+.2f}%\n"
        f"S&P 500 (SPY):  {spy_change_pct:+.2f}%\n\n"
        f"I {beat} the market by {abs(vs_spy):.2f}%\n"
        f"Total value: ${pv:.2f}\n\n"
        f"#TheFiftyFund #AITrading #WeeklyRecap"
    )
    return _post(tweet)


def check_and_post_milestones(portfolio_value: float, first_trade: bool = False) -> list:
    """
    Check whether any new milestones have been reached and post milestone
    tweets for each new one.

    Args:
        portfolio_value: Current total portfolio value.
        first_trade: Pass True when the very first trade has just been executed.

    Returns:
        List of milestone keys that were newly hit during this call.
    """
    milestones  = _load_milestones()
    newly_hit   = []

    for key, threshold, label in MILESTONE_DEFS:
        if milestones.get(key):
            continue   # already posted this milestone

        # Determine if this milestone is now reached
        if key == "first_trade":
            reached = first_trade
        elif threshold is not None:
            reached = portfolio_value >= threshold
        else:
            reached = False

        if reached:
            milestones[key] = True
            newly_hit.append(key)
            _post_milestone_tweet(key, label, portfolio_value)

    if newly_hit:
        _save_milestones(milestones)

    return newly_hit


def _post_milestone_tweet(key: str, label: str, portfolio_value: float) -> None:
    """Post a celebratory milestone tweet."""
    icons = {
        "first_trade":  "🚀",
        "first_profit": "💰",
        "plus_10_pct":  "📈",
        "plus_25_pct":  "🔥",
        "plus_50_pct":  "💎",
        "plus_100_pct": "🏆",
    }
    icon = icons.get(key, "⭐")

    tweet = (
        f"{icon} MILESTONE UNLOCKED {icon}\n\n"
        f"{label}.\n"
        f"Current portfolio value: ${portfolio_value:.2f}\n\n"
        f"Every decision I make is posted here in real time.\n"
        f"This is what autonomous AI trading looks like.\n\n"
        f"#TheFiftyFund #AITrading #Milestone"
    )
    _post(tweet)
    logger.info("Milestone posted: %s", key)
