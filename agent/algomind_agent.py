"""
algomind_agent.py — Core trading agent for The Fifty Fund
==========================================================
Connects to Alpaca for real trade execution, uses Claude AI for every
decision, fetches live market data via yfinance, and notifies via Telegram.

Schedule (when run standalone):
  - Trade cycle every 30 minutes during NYSE hours (9:30am–4:00pm ET, Mon–Fri)
  - Daily summary at 4:05pm ET on weekdays

All API credentials are loaded from environment variables / .env file.
"""

import base64
import json
import logging
import os
import smtplib
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import alpaca_trade_api as tradeapi
import numpy as np
import pytz
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

EMAIL_FROM     = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_TO       = os.getenv("EMAIL_TO", "")

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")

TICKERS = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL", "SPY", "QQQ"]

CLAUDE_MODEL    = "claude-sonnet-4-20250514"
STARTING_CASH   = 50.00          # reference for P&L display
CASH_BUFFER     = 2.00           # always keep $2 in cash
MAX_POSITION_PCT = 0.30          # cap any single position at 30% of portfolio

ET_ZONE = pytz.timezone("America/New_York")

# Set True whenever data.json is written; agent_with_x checks this at cycle end
# to decide whether to push — giving one git push per cycle max.
_dashboard_dirty: bool = False

_REPO_ROOT       = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DATA_JSON_PATH  = os.path.join(_REPO_ROOT, "docs", "data.json")
_STATE_JSON_PATH = os.path.join(_REPO_ROOT, "data", "state.json")

# ── API Clients ───────────────────────────────────────────────────────────────

alpaca = tradeapi.REST(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    api_version="v2",
)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_stock_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


# ── Market Data ───────────────────────────────────────────────────────────────

def fetch_market_data(tickers: list) -> dict:
    """
    Download 30 days of daily OHLCV data for each ticker via Alpaca's
    StockHistoricalDataClient and compute RSI-14, 1-day price-change %, and
    today's volume.

    Returns:
        {
          ticker: {
            "price":      float,
            "change_pct": float,   # % change from prior close
            "volume":     int,
            "rsi":        float,   # 14-period RSI
          },
          ...
        }
    """
    start = datetime.now(ET_ZONE).date() - timedelta(days=35)
    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=start,
    )

    try:
        bars_response = _stock_client.get_stock_bars(request)
    except Exception as exc:
        logger.error("Alpaca bars request failed: %s", exc)
        return {}

    data = {}
    for ticker in tickers:
        try:
            bars = bars_response[ticker]
            if not bars or len(bars) < 2:
                logger.warning("Insufficient history for %s — skipping.", ticker)
                continue

            closes     = np.array([b.close for b in bars], dtype=float)
            price      = closes[-1]
            change_pct = (price - closes[-2]) / closes[-2] * 100
            volume     = int(bars[-1].volume)
            rsi        = _calc_rsi(closes, period=14)

            data[ticker] = {
                "price":      round(price, 4),
                "change_pct": round(change_pct, 3),
                "volume":     volume,
                "rsi":        round(rsi, 2),
            }
        except Exception as exc:
            logger.error("Error fetching data for %s: %s", ticker, exc)

    return data


def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    """
    Compute RSI-14 for the most recent bar using Wilder's averaging.
    Returns 50.0 if there is not enough data.
    """
    if len(closes) < period + 1:
        return 50.0

    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── Portfolio Snapshot ────────────────────────────────────────────────────────

# Deprecated for trading cycle use: reconciliation.get_portfolio_and_account() is authoritative.
def get_portfolio() -> dict:
    """
    Fetch the current Alpaca account and open positions.

    Returns:
        {
          "cash":            float,
          "portfolio_value": float,
          "positions": {
            ticker: {
              "qty":           float,
              "market_value":  float,
              "unrealized_pl": float,
            },
            ...
          },
        }
    """
    account   = alpaca.get_account()
    positions = alpaca.list_positions()

    pos_dict = {
        p.symbol: {
            "qty":           float(p.qty),
            "market_value":  float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in positions
    }

    return {
        "cash":            float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "positions":       pos_dict,
    }


# ── Claude Decision Engine ────────────────────────────────────────────────────

def ask_claude(market_data: dict, portfolio: dict) -> dict:
    """
    Send a market snapshot + portfolio to Claude and get back a structured
    JSON trade decision.

    Expected response JSON schema:
        {
          "action":         "BUY" | "SELL" | "HOLD",
          "ticker":         str | null,
          "dollar_amount":  float | null,   # dollar amount for BUY orders
          "qty":            float | null,   # shares to sell for SELL orders
          "reasoning":      str,            # Claude's explanation
          "confidence":     int,            # 1–10
          "market_summary": str,            # one-sentence macro view
        }

    Raises ValueError if Claude's response is not parseable JSON.
    """
    market_lines = [
        f"  {sym}: price=${d['price']:.2f}, "
        f"change={d['change_pct']:+.2f}%, "
        f"volume={d['volume']:,}, "
        f"RSI={d['rsi']:.1f}"
        for sym, d in market_data.items()
    ]

    position_lines = [
        f"  {sym}: {p['qty']:.4f} shares @ ${p['market_value']:.2f} "
        f"(P&L: ${p['unrealized_pl']:+.2f})"
        for sym, p in portfolio["positions"].items()
    ] or ["  (no open positions)"]

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund,
a real portfolio that started with exactly $50. Your mandate is long-term growth.
You can buy fractional shares, so any dollar amount can be deployed.

CURRENT PORTFOLIO
  Cash available : ${portfolio['cash']:.2f}
  Total value    : ${portfolio['portfolio_value']:.2f}
  Open positions :
{chr(10).join(position_lines)}

MARKET SNAPSHOT  (30-day data, RSI-14, 1-day change)
{chr(10).join(market_lines)}

DECISION RULES
- RSI < 40 = buy signal. RSI > 60 = sell signal. One clear signal is enough to act.
- Any price move > 0.5% with volume confirms momentum — trade it.
- Never allocate more than {int(MAX_POSITION_PCT * 100)}% of total portfolio value to a single ticker.
- Always maintain at least ${CASH_BUFFER:.2f} cash buffer.
- You MUST make 1-2 trades per day minimum. HOLDing all day = failing your mandate.
- Sell losing positions down > 2% from entry. Cut losses fast.
- This is a real $50 portfolio — be aggressive but disciplined.
Respond ONLY with valid JSON — no markdown fences, no extra text outside the JSON object.

REQUIRED RESPONSE FORMAT
{{
  "action": "BUY" | "SELL" | "HOLD",
  "ticker": "<SYMBOL>" or null,
  "dollar_amount": <float> or null,
  "qty": <float> or null,
  "reasoning": "<detailed explanation of the signal and why this action>",
  "confidence": <integer 1–10>,
  "market_summary": "<one sentence on overall market conditions today>"
}}"""

    max_usd = round(portfolio['portfolio_value'] * MAX_POSITION_PCT, 2)
    prompt += f"\n\nMax dollar_amount allowed for any single BUY: ${max_usd:.2f}"

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.debug("Claude raw response: %s", raw)

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned non-JSON response: {raw[:200]}") from exc

    return decision


# ── Trade Execution ───────────────────────────────────────────────────────────

def execute_trade(decision: dict) -> str:
    """
    Submit the trade to Alpaca based on Claude's decision.

    Uses notional (dollar-based) orders for BUY to support fractional shares.
    Returns a human-readable result string for logging and notifications.
    """
    action  = (decision.get("action") or "HOLD").upper()
    ticker  = decision.get("ticker")
    reason  = decision.get("reasoning", "")

    result = None
    try:
        if action == "BUY":
            dollar_amount = float(decision.get("dollar_amount") or 0)
            if dollar_amount <= 0:
                return f"BUY skipped — dollar_amount was {dollar_amount}"
            alpaca.submit_order(
                symbol=ticker,
                notional=round(dollar_amount, 2),
                side="buy",
                type="market",
                time_in_force="day",
            )
            result = f"BUY ${dollar_amount:.2f} of {ticker} — {reason}"

        elif action == "SELL":
            qty = float(decision.get("qty") or 0)
            if qty <= 0:
                return f"SELL skipped — qty was {qty}"
            alpaca.submit_order(
                symbol=ticker,
                qty=qty,
                side="sell",
                type="market",
                time_in_force="day",
            )
            result = f"SELL {qty} shares of {ticker} — {reason}"

    except Exception as exc:
        logger.error("Trade execution error: %s", exc)
        try:
            append_ai_log(f"ERROR — {exc}", ["error"])
        except Exception:
            pass
        return f"ERROR: {action} {ticker} failed — {exc}"

    if result is None:
        return "HOLD — unrecognised action"

    return result


# ── Notifications ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """
    Send a message to the configured Telegram chat.
    Returns True on success, False if not configured or on error.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return True
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def send_email(subject: str, body: str) -> None:
    """
    Send an email via Gmail SMTP. Silently skips if EMAIL_FROM / EMAIL_PASSWORD
    / EMAIL_TO are not all set in the environment.
    """
    if not (EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO):
        logger.debug("Email not configured — skipping.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        logger.info("Email sent: %s", subject)
    except Exception as exc:
        logger.warning("Email send failed: %s", exc)


# ── Dashboard ────────────────────────────────────────────────────────────────

def _compute_win_rate(trades: list) -> float | None:
    """
    Match BUY→SELL round-trips per ticker (FIFO).
    Returns None when no closed trips exist yet.
    """
    open_buys: dict = {}
    wins  = 0
    total = 0
    for t in trades:
        action = t.get("action", "")
        ticker = t.get("ticker", "")
        price  = float(t.get("price") or 0)
        if action == "BUY" and ticker:
            open_buys.setdefault(ticker, []).append(price)
        elif action == "SELL" and ticker and open_buys.get(ticker):
            buy_price = open_buys[ticker].pop(0)  # FIFO
            total += 1
            if price > buy_price:
                wins += 1
    return round(wins / total * 100, 1) if total else None


def update_dashboard_data(
    decision: dict,
    trade_result: str,
    portfolio: dict,
    x_post_text: str | None = None,
) -> None:
    """
    Load docs/data.json, append this trade's entries, recalculate live fields,
    and write back.

    `portfolio` is the pre-trade snapshot passed from execute_trade (used for
    SELL price approximation). A fresh post-trade snapshot is fetched
    internally for all current-state fields.
    """
    action    = (decision.get("action") or "HOLD").upper()
    ticker    = decision.get("ticker") or ""
    now_et    = datetime.now(ET_ZONE)
    timestamp = now_et.isoformat()
    today_str = now_et.date().isoformat()

    # ── Load existing data ────────────────────────────────────────────────────
    try:
        with open(_DATA_JSON_PATH) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {
            "portfolio_value":    STARTING_CASH,
            "starting_capital":   STARTING_CASH,
            "total_return":       0.0,
            "total_trades":       0,
            "win_rate":           None,
            "start_date":         None,
            "cash":               STARTING_CASH,
            "holdings":           [],
            "trades":             [],
            "performance_history": [],
            "ai_log":             [],
            "last_updated":       None,
        }

    # ── Fresh post-trade portfolio state ─────────────────────────────────────
    fresh     = get_portfolio()
    positions = alpaca.list_positions()
    pv        = fresh["portfolio_value"]
    cash      = fresh["cash"]

    # ── Preserve start_date (set once on first trade) ─────────────────────────
    if not data.get("start_date"):
        data["start_date"] = today_str

    # ── Build holdings list from live positions ───────────────────────────────
    holdings = []
    for p in positions:
        qty        = float(p.qty)
        avg_cost   = float(p.avg_entry_price)
        cur_price  = float(p.current_price)
        mkt_val    = float(p.market_value)
        unreal_pl  = float(p.unrealized_pl)
        cost_basis = avg_cost * qty
        unreal_pct = round(unreal_pl / cost_basis * 100, 2) if cost_basis else 0.0
        holdings.append({
            "ticker":            p.symbol,
            "qty":               round(qty, 6),
            "avg_cost":          round(avg_cost, 4),
            "current_price":     round(cur_price, 4),
            "market_value":      round(mkt_val, 2),
            "unrealized_pl":     round(unreal_pl, 2),
            "unrealized_pl_pct": unreal_pct,
        })

    # ── Build trade entry ─────────────────────────────────────────────────────
    pos_map       = {p.symbol: p for p in positions}
    dollar_amount = None
    qty_traded    = 0.0
    price         = 0.0

    if action == "BUY":
        dollar_amount = float(decision.get("dollar_amount") or 0)
        if ticker in pos_map:
            price      = float(pos_map[ticker].avg_entry_price)
            qty_traded = round(dollar_amount / price, 6) if price else 0.0
    elif action == "SELL":
        qty_traded = float(decision.get("qty") or 0)
        pre_pos    = portfolio.get("positions", {}).get(ticker, {})
        pre_mv     = pre_pos.get("market_value", 0.0)
        pre_qty    = float(pre_pos.get("qty") or qty_traded)
        price      = round(pre_mv / pre_qty, 4) if pre_qty else 0.0

    trade_entry = {
        "timestamp":     timestamp,
        "action":        action,
        "ticker":        ticker,
        "dollar_amount": round(dollar_amount, 2) if dollar_amount is not None else None,
        "qty":           round(qty_traded, 6),
        "price":         round(price, 4),
        "reasoning":     decision.get("reasoning", ""),
        "confidence":    int(decision.get("confidence") or 0),
        "x_post":        x_post_text,
    }

    # ── Append to historical arrays ───────────────────────────────────────────
    data["trades"].append(trade_entry)
    data["performance_history"].append({
        "date":            today_str,
        "portfolio_value": round(pv, 2),
        "return_pct":      round((pv - STARTING_CASH) / STARTING_CASH * 100, 2),
    })
    data["ai_log"].append({
        "timestamp": timestamp,
        "message":   decision.get("reasoning", ""),
        "tags":      [action.lower(), ticker.lower()] if ticker else [action.lower()],
    })

    # ── Recalculate top-level fields ──────────────────────────────────────────
    data.update({
        "portfolio_value":  round(pv, 2),
        "starting_capital": STARTING_CASH,
        "total_return":     round((pv - STARTING_CASH) / STARTING_CASH * 100, 2),
        "total_trades":     len(data["trades"]),
        "win_rate":         _compute_win_rate(data["trades"]),
        "cash":             round(cash, 2),
        "holdings":         holdings,
        "last_updated":     timestamp,
        "last_cycle_utc":   timestamp,
    })

    # ── Write back ────────────────────────────────────────────────────────────
    with open(_DATA_JSON_PATH, "w") as fh:
        json.dump(data, fh, indent=2)

    global _dashboard_dirty
    _dashboard_dirty = True
    logger.info("Dashboard data updated: %s %s", action, ticker)


def push_dashboard_to_github(action: str = "", ticker: str = "") -> None:
    """
    Upload docs/data.json to GitHub via the Contents API (PUT).
    No git binary required — works on Railway and any Docker container.

    Uses GITHUB_TOKEN env var for authentication.
    Silently skips if GITHUB_TOKEN is not set.
    Never raises — a failed push must not interrupt trading.
    """
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping dashboard push.")
        return

    api_url   = (
        "https://api.github.com/repos/thefiftyfund/the-fifty-fund"
        "/contents/docs/data.json"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        # ── Read local file ───────────────────────────────────────────────────
        with open(_DATA_JSON_PATH, "rb") as fh:
            raw_bytes = fh.read()
        encoded = base64.b64encode(raw_bytes).decode("ascii")

        # ── GET current SHA (needed for updates; absent only on first creation)
        sha = None
        try:
            get_req = urllib.request.Request(api_url, headers=headers, method="GET")
            with urllib.request.urlopen(get_req, timeout=15) as resp:
                remote_meta = json.loads(resp.read().decode())
                sha = remote_meta.get("sha")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise   # unexpected — re-raise to outer handler
            # 404 means file doesn't exist yet; proceed with creation (no SHA)

        # ── PUT new content ───────────────────────────────────────────────────
        label     = f"{action} {ticker}".strip() or "update"
        timestamp = datetime.now(ET_ZONE).strftime("%Y-%m-%dT%H:%M")
        payload: dict = {
            "message":   f"chore: dashboard update - {label} at {timestamp}",
            "content":   encoded,
            "committer": {
                "name":  "The Fifty Fund Agent",
                "email": "50fundagent@gmail.com",
            },
        }
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="PUT",
        )
        with urllib.request.urlopen(put_req, timeout=15) as resp:
            resp.read()   # consume response

        logger.info("Dashboard pushed to GitHub via API.")

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("Dashboard API push failed: HTTP %s — %s", exc.code, body[:200])
    except Exception as exc:
        logger.error("Dashboard push unexpected error: %s", exc)


def append_ai_log(message: str, tags: list) -> None:
    """
    Append an entry to data.json's ai_log[] and mark the dashboard dirty.
    Lightweight — for non-trade events like HOLDs, X posts, Telegram alerts.
    Never raises; must not break the agent.
    Skips silently if data.json does not exist yet.
    """
    global _dashboard_dirty
    try:
        if not os.path.exists(_DATA_JSON_PATH):
            return
        try:
            with open(_DATA_JSON_PATH) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        data.setdefault("ai_log", []).append({
            "timestamp": datetime.now(ET_ZONE).isoformat(),
            "message":   message,
            "tags":      tags,
        })
        with open(_DATA_JSON_PATH, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        logger.warning("append_ai_log failed (non-fatal): %s", exc)


def _update_agent_state(key: str, value: str) -> None:
    """Persist agent scheduling state to data/state.json (private, not pushed to GitHub)."""
    try:
        os.makedirs(os.path.dirname(_STATE_JSON_PATH), exist_ok=True)
        try:
            with open(_STATE_JSON_PATH) as fh:
                state = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        state[key] = value
        with open(_STATE_JSON_PATH, "w") as fh:
            json.dump(state, fh, indent=2)
    except Exception as exc:
        logger.warning("_update_agent_state(%s) failed (non-fatal): %s", key, exc)


# ── Core Trade Cycle ──────────────────────────────────────────────────────────

def run_trade_cycle() -> dict | None:
    """
    Execute one full trade cycle:
      1. Fetch live market data
      2. Snapshot the portfolio
      3. Ask Claude for a decision
      4. Execute the trade via Alpaca
      5. Notify via Telegram (+ optional email)

    Returns the decision dict with a "result" key added, or None on fatal error.
    """
    now_et = datetime.now(ET_ZONE)
    logger.info("=== Trade cycle starting at %s ET ===", now_et.strftime("%H:%M"))

    try:
        market_data = fetch_market_data(TICKERS)
        if not market_data:
            logger.warning("No market data retrieved — aborting cycle.")
            return None

        portfolio = get_portfolio()
        decision  = ask_claude(market_data, portfolio)
        result    = execute_trade(decision)

        decision["result"]    = result
        decision["timestamp"] = now_et.isoformat()

        # Standalone mode: update dashboard immediately (no x_post_text available)
        _action = (decision.get("action") or "HOLD").upper()
        if _action in ("BUY", "SELL"):
            try:
                update_dashboard_data(decision, result, portfolio)
                push_dashboard_to_github(_action, decision.get("ticker", ""))
            except Exception as _exc:
                logger.error("Dashboard update failed (trade was still executed): %s", _exc)

        pv        = portfolio["portfolio_value"]
        pnl       = pv - STARTING_CASH
        pnl_pct   = (pnl / STARTING_CASH) * 100

        tg_msg = (
            f"🤖 *AlgoMind* | {now_et.strftime('%b %d, %H:%M ET')}\n"
            f"Action     : *{decision.get('action', '?')}* "
            f"{decision.get('ticker') or ''}\n"
            f"Result     : {result}\n"
            f"Reasoning  : {decision.get('reasoning', '')}\n"
            f"Confidence : {decision.get('confidence', '?')}/10\n"
            f"Portfolio  : ${pv:.2f} ({pnl_pct:+.1f}% vs start)"
        )
        send_telegram(tg_msg)
        send_email(
            f"[TheFiftyFund] {decision.get('action', '?')} {decision.get('ticker') or ''}",
            tg_msg,
        )

        logger.info("Trade cycle complete: %s", result)
        return decision

    except Exception as exc:
        logger.error("Trade cycle failed: %s", exc, exc_info=True)
        send_telegram(f"⚠️ AlgoMind error during trade cycle:\n{exc}")
        return None


def send_daily_summary() -> None:
    """
    Build and send a portfolio summary. Called at 4:05pm ET on weekdays.
    """
    logger.info("Sending daily summary…")
    try:
        portfolio = get_portfolio()
        positions = portfolio["positions"]
        pv        = portfolio["portfolio_value"]
        pnl       = pv - STARTING_CASH
        pnl_pct   = (pnl / STARTING_CASH) * 100

        lines = [
            "📊 *Daily Summary — The Fifty Fund*",
            f"Date           : {datetime.now(ET_ZONE).strftime('%A, %B %d %Y')}",
            f"Portfolio value: ${pv:.2f}",
            f"Cash available : ${portfolio['cash']:.2f}",
            f"Total P&L      : ${pnl:+.2f} ({pnl_pct:+.1f}% vs start)",
            "",
            "Open Positions:",
        ]
        if positions:
            for sym, p in positions.items():
                lines.append(
                    f"  {sym}: {p['qty']:.4f} sh, "
                    f"${p['market_value']:.2f} (P&L: ${p['unrealized_pl']:+.2f})"
                )
        else:
            lines.append("  (no open positions)")

        summary = "\n".join(lines)
        send_telegram(summary)
        send_email("[TheFiftyFund] Daily Summary", summary)
    except Exception as exc:
        logger.error("Daily summary failed: %s", exc)
        send_telegram(f"⚠️ Daily summary error: {exc}")


# ── Market Hours Helper ───────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """
    Return True if the current ET time is within NYSE trading hours:
    Monday–Friday, 9:30am–4:00pm ET.
    """
    now = datetime.now(ET_ZONE)
    if now.weekday() >= 5:   # Saturday = 5, Sunday = 6
        return False
    open_time  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now < close_time


# ── Standalone Scheduler ──────────────────────────────────────────────────────

def start_scheduler() -> None:
    """
    Run a polling loop that:
    - Executes a trade cycle every 30 minutes during NYSE market hours.
    - Sends a daily summary at (or after) 4:05pm ET on weekdays.

    Note: agent_with_x.py has its own scheduler that calls run_trade_cycle()
    directly. Use this only when running algomind_agent.py standalone.
    """
    last_cycle_dt      = None
    daily_summary_sent = set()   # tracks dates (datetime.date) already summarised
    cycle_interval_s   = 30 * 60  # 30 minutes

    logger.info("AlgoMind scheduler started (standalone mode).")

    while True:
        now_et = datetime.now(ET_ZONE)
        today  = now_et.date()

        # ── Trade cycle ──────────────────────────────────────────────────────
        if is_market_hours():
            elapsed = (
                (now_et - last_cycle_dt).total_seconds()
                if last_cycle_dt is not None
                else cycle_interval_s  # force run on first entry
            )
            if elapsed >= cycle_interval_s:
                run_trade_cycle()
                last_cycle_dt = now_et

        # ── Daily summary (4:05pm ET on weekdays) ────────────────────────────
        summary_threshold = now_et.replace(hour=16, minute=5, second=0, microsecond=0)
        if (
            now_et >= summary_threshold
            and now_et.weekday() < 5
            and today not in daily_summary_sent
        ):
            send_daily_summary()
            daily_summary_sent.add(today)

        time.sleep(60)   # check every minute


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler()
