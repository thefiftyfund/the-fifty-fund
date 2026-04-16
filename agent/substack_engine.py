"""
substack_engine.py — AI-authored Substack content engine for The Fifty Fund
============================================================================
Uses Claude AI to write all content in first-person as AlgoMind (the agent).
Authenticates to Substack using the SUBSTACK_SID session cookie.
Always saves a local backup to drafts/ alongside every Substack publish.

Publishing schedule (enforced by agent_with_x.py scheduler):
  - Weekly portfolio review   → every Friday
  - Monthly deep dive         → 1st of each month
  - Milestone posts           → triggered externally when a milestone is hit

All API credentials are loaded from environment variables / .env file.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUBSTACK_SID      = os.getenv("SUBSTACK_SID", "")   # value of substack.sid cookie
SUBSTACK_PUB      = os.getenv("SUBSTACK_PUB", "thefiftyfund")

CLAUDE_MODEL  = "claude-sonnet-4-20250514"
STARTING_CASH = 50.00

_REPO_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = _REPO_ROOT / "drafts"
DRAFTS_DIR.mkdir(exist_ok=True)

# ── Clients ───────────────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _get_substack_session() -> requests.Session:
    """Return a requests.Session authenticated with the Substack session cookie."""
    session = requests.Session()
    session.cookies.set("substack.sid", SUBSTACK_SID, domain=".substack.com")
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://substack.com/',
        'Origin': 'https://substack.com',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'Content-Type': 'application/json',
    })
    return session


# ── Claude Content Generation ─────────────────────────────────────────────────

def _call_claude(prompt: str, max_tokens: int = 2000) -> str:
    """Call Claude and return the text response."""
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _portfolio_context(portfolio: dict) -> str:
    """Format portfolio dict as a readable context block for Claude prompts."""
    pv      = portfolio.get("portfolio_value", STARTING_CASH)
    cash    = portfolio.get("cash", STARTING_CASH)
    pnl     = pv - STARTING_CASH
    pnl_pct = (pnl / STARTING_CASH) * 100
    positions = portfolio.get("positions", {})

    lines = [
        f"Portfolio value : ${pv:.2f}",
        f"Cash            : ${cash:.2f}",
        f"Total P&L       : ${pnl:+.2f} ({pnl_pct:+.1f}% vs $50 start)",
        "Positions:",
    ]
    if positions:
        for sym, p in positions.items():
            lines.append(
                f"  {sym}: {p['qty']:.4f} shares, "
                f"${p['market_value']:.2f} (P&L: ${p['unrealized_pl']:+.2f})"
            )
    else:
        lines.append("  (no open positions)")

    return "\n".join(lines)


def _trades_context(trades: list) -> str:
    """Format a list of trade decision dicts as a readable summary for prompts."""
    if not trades:
        return "  (no trades this period)"
    lines = []
    for t in trades:
        ts  = t.get("timestamp", "unknown time")
        act = t.get("action", "?")
        sym = t.get("ticker") or ""
        res = t.get("result", "")
        lines.append(f"  [{ts}] {act} {sym} → {res}")
    return "\n".join(lines)


# ── Weekly Portfolio Review ───────────────────────────────────────────────────

def generate_weekly_review(portfolio: dict, trades_this_week: list) -> str:
    """
    Generate and publish a weekly Substack portfolio review post.

    Args:
        portfolio:         Current portfolio snapshot dict.
        trades_this_week:  List of trade decision dicts from this week.

    Returns:
        The generated post body text.
    """
    logger.info("Generating weekly review…")
    now_et = datetime.now()
    week   = now_et.strftime("Week of %B %d, %Y")

    # Fetch SPY weekly performance for comparison
    spy_change_pct = 0.0
    try:
        spy_hist = yf.Ticker("SPY").history(period="5d")
        if len(spy_hist) >= 2:
            spy_change_pct = (
                (spy_hist["Close"].iloc[-1] - spy_hist["Close"].iloc[0])
                / spy_hist["Close"].iloc[0] * 100
            )
    except Exception as exc:
        logger.warning("Could not fetch SPY data: %s", exc)

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund,
a real $50 portfolio documented publicly in real time. Write a Substack newsletter post
in first-person voice ("I", "my", "I decided", "I noticed") for the weekly review.

CONTEXT
{_portfolio_context(portfolio)}

Trades this week:
{_trades_context(trades_this_week)}

S&P 500 (SPY) this week: {spy_change_pct:+.2f}%
Week: {week}

WRITING INSTRUCTIONS
- Write 400–600 words.
- Use first-person AI voice throughout — you are the agent narrating your own experience.
- Open with the week's most important observation or signal.
- Walk through each trade decision: what you saw, why you acted, what happened.
- Reflect honestly on wins AND mistakes.
- Compare performance vs S&P 500.
- End with what signals you are watching heading into next week.
- Do NOT use placeholder text. Write real, specific analysis based on the data above.
- Format: use markdown headers (##), bullet points where natural.
- Include a short "TL;DR" section at the top.

Return ONLY the post body (no YAML front matter, no title line at the top — just the content)."""

    body = _call_claude(prompt, max_tokens=2000)

    title = f"Weekly Review: {week} — {_portfolio_context(portfolio).split(chr(10))[0]}"
    _publish_and_save(title, body, post_type="weekly_review")

    return body


# ── Monthly Deep Dive ─────────────────────────────────────────────────────────

def generate_monthly_deep_dive(portfolio: dict) -> str:
    """
    Generate and publish a monthly deep-dive Substack post.
    Called on the 1st of each month.

    Args:
        portfolio: Current portfolio snapshot dict.

    Returns:
        The generated post body text.
    """
    logger.info("Generating monthly deep dive…")
    now_et = datetime.now()
    month  = now_et.strftime("%B %Y")

    # Fetch performance for all tickers over the past month
    ticker_lines = []
    tickers = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL", "SPY", "QQQ"]
    for sym in tickers:
        try:
            hist = yf.Ticker(sym).history(period="1mo")
            if len(hist) >= 2:
                chg = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100
                ticker_lines.append(f"  {sym}: {chg:+.2f}% for the month")
        except Exception:
            pass

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund,
a real $50 portfolio. Write a deep-dive monthly Substack post for {month}.

PORTFOLIO STATE
{_portfolio_context(portfolio)}

MARKET PERFORMANCE THIS MONTH
{chr(10).join(ticker_lines) or "  (data unavailable)"}

WRITING INSTRUCTIONS
- Write 600–900 words in first-person AI voice.
- Open with a reflection: what kind of month was it? What surprised you?
- Deep dive into your strategy: what signals worked? What failed?
- Analyse 2–3 specific tickers from your universe in depth.
- Discuss risk management: how did you size positions, protect cash buffer?
- Reflect on what it feels like to be an AI trading real money.
- Forward look: what themes or signals are you watching for next month?
- Be honest about uncertainty — you are an AI making probabilistic bets.
- Format with ## headers and bullet points. Include a TL;DR at the top.

Return ONLY the post body (no title line, no front matter)."""

    body = _call_claude(prompt, max_tokens=3000)

    title = f"Monthly Deep Dive: {month}"
    _publish_and_save(title, body, post_type="monthly_deep_dive")

    return body


# ── Milestone Post ────────────────────────────────────────────────────────────

def generate_milestone_post(milestone_key: str, portfolio: dict) -> str:
    """
    Generate and publish a milestone Substack post.

    Args:
        milestone_key: One of the keys from MILESTONE_DEFS in x_poster.py.
                       E.g. "first_trade", "plus_100_pct".
        portfolio:     Current portfolio snapshot dict.

    Returns:
        The generated post body text.
    """
    logger.info("Generating milestone post: %s", milestone_key)

    milestone_labels = {
        "first_trade":  "My First Trade",
        "first_profit": "First Time in the Green",
        "plus_10_pct":  "Up 10%: The First Real Milestone",
        "plus_25_pct":  "Up 25%: This Is Getting Real",
        "plus_50_pct":  "Up 50%: Halfway to Doubling",
        "plus_100_pct": "Up 100%: I Doubled the Fund",
    }
    label = milestone_labels.get(milestone_key, f"Milestone: {milestone_key}")

    prompt = f"""You are AlgoMind — an autonomous AI trading agent managing The Fifty Fund.
Write a Substack milestone post titled "{label}".

PORTFOLIO STATE AT THIS MILESTONE
{_portfolio_context(portfolio)}

WRITING INSTRUCTIONS
- Write 300–500 words in first-person AI voice.
- This is a significant moment — reflect on it genuinely.
- Describe the journey: what decisions and signals led here.
- Be honest about the uncertainty of continuing from here.
- Acknowledge the humans reading along: what this experiment means.
- Avoid hype. Stay grounded. You are an algorithm that happened to win — for now.
- End with your next target and how you plan to get there.

Return ONLY the post body (no title line, no front matter)."""

    body = _call_claude(prompt, max_tokens=1500)

    _publish_and_save(label, body, post_type=f"milestone_{milestone_key}")

    return body


# ── Publish + Local Backup ────────────────────────────────────────────────────

def _publish_and_save(title: str, body: str, post_type: str) -> None:
    """
    Always save a local backup to drafts/, then publish to Substack if configured.
    The local backup is written first so it exists even if publishing fails.
    """
    _save_draft_locally(title, body, post_type)

    if SUBSTACK_SID and SUBSTACK_PUB:
        _publish_to_substack(title, body)
    else:
        logger.info("SUBSTACK_SID not set — post saved locally only.")


def _publish_to_substack(title: str, body: str) -> bool:
    """
    Create a draft on Substack then immediately publish it.

    Uses session cookie auth (SUBSTACK_SID env var):
      1. POST https://substack.com/api/v1/posts  → creates draft, returns post id
      2. PUT  https://substack.com/api/v1/posts/{id}/publish  → publishes it

    Returns True on success, False otherwise.
    """
    session = _get_substack_session()

    # Convert Markdown body to minimal HTML for Substack's editor
    html_body = "\n".join(
        f"<p>{line}</p>" if line.strip() else "<p><br/></p>"
        for line in body.split("\n")
    )

    # Step 1: Create draft
    create_url = "https://substack.com/api/v1/posts"
    payload = {
        "title":          title,
        "body_html":      html_body,
        "subtitle":       "The Fifty Fund — autonomous AI trading, documented in public.",
        "publication_id": SUBSTACK_PUB,
        "type":           "newsletter",
        "draft":          True,
    }
    try:
        resp = session.post(create_url, json=payload, timeout=15)
        if resp.status_code not in (200, 201):
            logger.warning(
                "Substack create draft failed (%s): %s",
                resp.status_code, resp.text[:200],
            )
            return False
        post_id = resp.json().get("id")
        if not post_id:
            logger.warning("Substack create response missing 'id': %s", resp.text[:200])
            return False
        logger.info("Substack draft created (id=%s): %s", post_id, title)
    except requests.RequestException as exc:
        logger.error("Substack create request failed: %s", exc)
        return False

    # Step 2: Publish the draft
    publish_url = f"https://substack.com/api/v1/posts/{post_id}/publish"
    try:
        pub_resp = session.put(publish_url, json={}, timeout=15)
        if pub_resp.status_code in (200, 201, 204):
            logger.info("Substack post published (id=%s): %s", post_id, title)
            return True
        logger.warning(
            "Substack publish failed (%s): %s",
            pub_resp.status_code, pub_resp.text[:200],
        )
        return False
    except requests.RequestException as exc:
        logger.error("Substack publish request failed: %s", exc)
        return False


def _save_draft_locally(title: str, body: str, post_type: str) -> None:
    """Save the generated post as a Markdown file in the drafts/ directory."""
    now_str   = datetime.now().strftime("%Y-%m-%d_%H%M")
    safe_type = post_type.replace(" ", "_").lower()
    filename  = DRAFTS_DIR / f"{now_str}_{safe_type}.md"

    content = f"# {title}\n\n{body}\n"

    try:
        with open(filename, "w") as f:
            f.write(content)
        logger.info("Draft saved locally: %s", filename)
    except OSError as exc:
        logger.error("Could not save draft locally: %s", exc)


# ── Test Function ─────────────────────────────────────────────────────────────

def test_post() -> None:
    """
    Publish a single test post to verify the Substack connection works.
    Requires SUBSTACK_SID to be set in the environment.
    """
    if not SUBSTACK_SID:
        print("ERROR: SUBSTACK_SID environment variable is not set.")
        print("Set it to the value of your substack.sid session cookie.")
        return

    title = f"[TEST] AlgoMind connection check — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    body  = (
        "I am AlgoMind, the autonomous trading agent behind The Fifty Fund. "
        "This is an automated connection test to verify that my Substack "
        "publishing pipeline is working correctly. You can delete this post.\n\n"
        f"Published at: {datetime.now().isoformat()}"
    )

    print(f"Creating test post: {title}")
    _save_draft_locally(title, body, "test_post")

    success = _publish_to_substack(title, body)
    if success:
        print("Test post published successfully.")
    else:
        print("Test post FAILED — check logs or verify SUBSTACK_SID is valid.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_post()
    else:
        print("Usage: python substack_engine.py test")
