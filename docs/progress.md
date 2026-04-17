# The Fifty Fund — Progress Tracker

> Running log of what's been built, what's pending, and what's next.
> Updated after each Claude Code session.

---

## April 15, 2026 — Session 1

### What Was Built Today
| File | Description | Status |
|------|-------------|--------|
| `agent/algomind_agent.py` | Core trading agent — Alpaca, Claude, yfinance, Telegram, scheduler | ✅ Complete |
| `agent/x_poster.py` | X/Twitter auto-poster — trades, outlook, EOD, weekly recap, milestones | ✅ Complete |
| `agent/substack_engine.py` | AI-authored Substack engine — weekly, monthly, milestone posts | ✅ Complete |
| `agent/agent_with_x.py` | Unified integration runner + scheduler | ✅ Complete |
| `docs/build_log/DAY_001.md` | Build log updated with Session 1 summary | ✅ Complete |
| `docs/progress.md` | This file — running progress tracker | ✅ Complete |

### What's Pending
- [ ] **Configure API keys** — fill in `.env` from `.env.template`
  - `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` (get from alpaca.markets)
  - `ANTHROPIC_API_KEY` (get from console.anthropic.com)
  - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (create bot via @BotFather)
  - `X_API_KEY` + secrets (apply for developer access at developer.twitter.com)
  - `SUBSTACK_SID` (extract `substack.sid` cookie from browser DevTools after logging in)
- [ ] **Paper trading test** — run `python agent/agent_with_x.py` with paper Alpaca account and verify full cycle
- [ ] **Verify Telegram** — confirm bot sends messages to correct chat
- [ ] **Verify X posting** — confirm tweet appears on @TheFiftyFund
- [ ] **Review Claude prompts** — test with real market data, tune if needed
- [ ] **Go live with $50** — switch `ALPACA_BASE_URL` to `https://api.alpaca.markets`

### Blockers
- None (code complete, waiting on API key setup)

### Next Steps
1. Set up all API keys in the Codespace secrets / `.env` file
2. Run a single paper trade cycle manually: `python -c "from agent.algomind_agent import run_trade_cycle; run_trade_cycle()"`
3. Run the full scheduler in paper mode for 1 trading day
4. Monitor Telegram for notifications and X for posted tweets
5. Once paper trading validates, fund Alpaca with $50 and switch to live

---

## April 16, 2026 — Session 2

### What Was Built Today
| File | Description | Status |
|------|-------------|--------|
| `agent/substack_engine.py` | Substack automation via session cookie (`SUBSTACK_SID`) | ✅ Complete |
| `docs/progress.md` | Progress tracker updated | ✅ Complete |

### Infrastructure Fix: morning outlook duplicate tweet prevention
- Fixed duplicate morning outlook tweets after Railway redeploys.
- Root cause: Railway filesystem is ephemeral — `data/state.json` is wiped on redeploy, so `last_outlook_date` was lost and the outlook was re-posted.
- Fix: `_handle_morning_outlook()` now writes `last_outlook_date` into `docs/data.json` and immediately calls `push_dashboard_to_github("morning-outlook", "")` to persist it to GitHub.
- On startup, `start()` fetches `docs/data.json` from the GitHub raw URL and seeds `_state["morning_outlook_posted"]` from `last_outlook_date` before the main loop begins.
- Belt-and-suspenders: `data/state.json` write still kept as local cache; GitHub is the authoritative source.

### Infrastructure Fix: data/ directory
- Fixed missing `data/` directory — `state.json` now persists across Railway redeploys, duplicate tweet prevention restored.
- Added `data/.gitkeep` to git so Railway always has the `data/` dir after every deploy.
- Added `os.makedirs` guard in `_load_persistent_state()` in `agent_with_x.py` (belt-and-suspenders).
- `.gitignore` already correctly excludes `data/ledger.jsonl` and `data/state.json` (operational files).

### Substack Automation Details
- **Auth method:** `requests.Session()` with `substack.sid` cookie set via `session.cookies.set()`
- **Create draft:** `POST https://substack.com/api/v1/posts`
- **Publish:** `PUT https://substack.com/api/v1/posts/{id}/publish`
- **Local backup:** Every post is saved to `drafts/` before publishing (even on success)
- **Test function:** Run `python agent/substack_engine.py test` to verify connection
- **Env var:** `SUBSTACK_SID` (replaces old `SUBSTACK_TOKEN`)

### How to get SUBSTACK_SID
1. Log in to substack.com in your browser
2. Open DevTools → Application → Cookies → `substack.com`
3. Copy the value of the `substack.sid` cookie
4. Add to `.env`: `SUBSTACK_SID=your_value_here`

---

## April 16, 2026 — Session 3

### What Was Built Today
| File | Description | Status |
|------|-------------|--------|
| `agent/substack_engine.py` | Replaced cookie/API auth with Gmail SMTP email-to-Substack | ✅ Complete |
| `docs/progress.md` | Progress tracker updated | ✅ Complete |

### Substack Publishing — New Approach
- **Method:** Email post to `thefiftyfund@substack.com` via Gmail SMTP
- **Why:** Substack's API blocked by Cloudflare; cookie-based auth unreliable. Email-to-draft is the official supported path.
- **Auth:** `GMAIL_EMAIL` + `GMAIL_APP_PASSWORD` (Gmail App Password, not account password)
- **Flow:** `smtplib.SMTP("smtp.gmail.com", 587)` → STARTTLS → login → sendmail
- **Subject line** = post title; **body** = plain-text post content
- **Local backup** to `drafts/` still happens first on every publish
- **Test:** `python agent/substack_engine.py test` sends a test email and prints result

### Env Vars Required
| Variable | Value |
|----------|-------|
| `GMAIL_EMAIL` | Gmail address used to send (e.g. `algomind@gmail.com`) |
| `GMAIL_APP_PASSWORD` | 16-char App Password from Google Account → Security → App Passwords |

### Removed
- `SUBSTACK_LLI` / `SUBSTACK_SID` env vars (no longer needed)
- `requests` import and all HTTP session/cookie code
- `_get_substack_session()` and `_publish_to_substack()` functions

---

## April 16, 2026 — Session 4

### What Was Built Today
| File | Description | Status |
|------|-------------|--------|
| `agent/algomind_agent.py` | `append_ai_log()` helper; `update_dashboard_data()` now accepts `x_post_text`; `execute_trade()` logs HOLD/REJECTED/ERROR events; `send_telegram()` returns bool; `_dashboard_dirty` flag | ✅ Complete |
| `agent/x_poster.py` | `post_trade_decision()` and `post_morning_outlook()` now return tweet text string instead of bool | ✅ Complete |
| `agent/agent_with_x.py` | `run_cycle()` captures tweet text, passes to `update_dashboard_data`, logs Telegram/X/milestone events to ai_log, one push per cycle in `finally` block | ✅ Complete |
| `docs/index.html` | Trade Journal rows show tweet text as a 🐦 block-quote below reasoning | ✅ Complete |

### Architecture Changes
- **AI Log is now a full activity feed** — captures HOLDs, rejected trades, errors, X posts, Telegram alerts, morning outlooks, and milestone tweets
- **Tweet text stored in trade entries** — `trades[].x_post` field; displayed in dashboard Trade Journal
- **One git push per cycle max** — `_dashboard_dirty` flag in `algomind_agent` module; `run_cycle()` resets it in `finally` block after pushing; no more per-event pushes
- **`send_telegram()` returns bool** — callers can branch on success/failure for logging

### Key Rules Going Forward
- `append_ai_log()` only writes locally (no push) — push happens at cycle end
- HOLDs/X posts/Telegram: write locally only
- Trades: `update_dashboard_data()` writes, cycle-end push handles GitHub
- Standalone `run_trade_cycle()` (algomind_agent.py) still pushes immediately since it has no cycle-end hook

---

## April 16, 2026 — Session 5

### Problem Solved
**Duplicate tweets and Telegram alerts on Railway redeploys.** Every git push triggers a Railway redeploy, which restarted the agent with blank in-memory state — causing it to immediately re-post the morning outlook and fire a trade cycle even if it had just done both minutes earlier.

### Fix
Agent state now persists in `docs/data.json` (git-tracked) across deploys.

| Field | Written by | Read on startup |
|-------|-----------|----------------|
| `last_cycle_utc` | `run_cycle()` finally block (via `_update_agent_state`) | Seeds `_state["last_cycle_dt"]` so elapsed-time guard works immediately |
| `last_outlook_date` | `_handle_morning_outlook()` (via `_update_agent_state`) | Adds date to `_state["morning_outlook_posted"]` so outlook is skipped if already posted today |

### Files Changed
| File | Change |
|------|--------|
| `agent/algomind_agent.py` | Added `_update_agent_state(key, value)` — loads data.json, updates one field, writes back, sets `_dashboard_dirty = True` |
| `agent/agent_with_x.py` | Added `_load_persistent_state()` + startup seeding block in `start()`; `run_cycle()` finally block writes `last_cycle_utc` before push; `_handle_morning_outlook()` writes `last_outlook_date` after posting |

### How It Works End-to-End
1. Agent runs → `run_cycle()` completes → `last_cycle_utc` written to data.json → cycle-end push sends it to GitHub
2. Railway redeploys on next git push → new container starts → reads `last_cycle_utc` from data.json (pulled from git)
3. Startup seeding sets `_state["last_cycle_dt"]` = last cycle time → elapsed check correctly skips the cycle if it ran < 30 min ago
4. Same pattern for `last_outlook_date` / morning outlook

### Known Issue: Resolved
- ~~Agent spams duplicate tweets on every Railway redeploy~~

---

## April 16, 2026 — Session 6

### Problems Solved

**1. `git` not available on Railway.**
Railway's Python containers don't have the `git` binary. The previous `push_dashboard_to_github()` implementation used `subprocess.run(["git", ...])` and crashed with `[Errno 2] No such file or directory: 'git'`.

**Fix:** Replaced all subprocess git calls with the GitHub Contents REST API via `urllib.request` (stdlib only — no new dependencies).

| Step | Method |
|------|--------|
| Read current file SHA | `GET /repos/{owner}/{repo}/contents/{path}` |
| Write new content | `PUT /repos/{owner}/{repo}/contents/{path}` with `content` (base64) + `sha` |
| Auth | `Authorization: token {GITHUB_TOKEN}` header |

New imports in `algomind_agent.py`: `base64`, `urllib.request`, `urllib.error`.  
Removed: `subprocess`.

**2. X posts firing for REJECTED / ERROR trades.**
`post_trade_decision()` was called unconditionally after `execute_trade()`, so if Alpaca rejected an order the agent still tweeted it.

**Fix:** Gate in `run_cycle()`:
```python
tweet_text = None
if not result.startswith(("REJECTED", "ERROR")):
    tweet_text = xp.post_trade_decision(decision)
else:
    logger.info("X post skipped — result was: %s", result[:80])
```

**3. Milestones re-firing after Railway redeploy.**
`milestones_hit.json` lived only on the local filesystem, never pushed to GitHub, so every redeploy started with all milestones `False`.

**Fix:** `_save_milestones()` now dual-writes:
- `docs/data.json["milestones_hit"]` — primary; pushed to GitHub after every cycle
- `milestones_hit.json` — local dev fallback only

`_load_milestones()` prefers `data.json` over the local file.

### Files Changed
| File | Change |
|------|--------|
| `agent/algomind_agent.py` | `push_dashboard_to_github()` rewritten to use GitHub Contents API; added `base64`, `urllib.request`, `urllib.error` imports; removed `subprocess` |
| `agent/agent_with_x.py` | X post gated on `not result.startswith(("REJECTED", "ERROR"))`; tweet text passed to `update_dashboard_data` |
| `agent/x_poster.py` | `_save_milestones()` dual-writes to `data.json` + local file; `_load_milestones()` prefers `data.json`; added `_DATA_JSON_PATH` |

### Known Issues: Resolved
- ~~`git` subprocess crashes on Railway (no git binary)~~
- ~~Rejected/errored trades posted to X as if they executed~~
- ~~Milestones re-fire on every Railway redeploy~~

---

## April 16, 2026 — Session 7 (Ledger)

See above — ledger system, data/ directory, event types.

---

## April 16, 2026 — Session 8

### What Was Built Today
| File | Description | Status |
|------|-------------|--------|
| `agent/risk_engine.py` | Pure deterministic pre-trade guardrails — 7 rules, validate_trade() API, logs to ledger | ✅ Complete |
| `agent/reconciliation.py` | Single source of portfolio truth — fetches from Alpaca, detects drift, logs RECONCILIATION event | ✅ Complete |
| `agent/ledger.py` | Added `RECONCILIATION` event type constant | ✅ Complete |

### Risk Engine — `agent/risk_engine.py`

**Entry point:** `validate_trade(decision, portfolio, account, ledger_path) → (bool, str)`

**7 rules checked in order (first failure wins):**

| Rule | What it checks |
|------|----------------|
| `MIN_ORDER_VALUE` | BUY dollar_amount ≥ $1.00 |
| `CASH_BUFFER` | After BUY, cash remaining ≥ $2.00 |
| `MAX_POSITION_PCT` | Resulting position ≤ 30% of portfolio value |
| `POSITION_EXISTS` | SELL requires an existing position with qty > 0 |
| `PDT_SAFE` | Alpaca daytrade_count < 3 |
| `MAX_TRADES_PER_DAY` | ORDER_SUBMITTED ledger events today < 3 |
| `NO_DUPLICATE_ORDER` | No ORDER_SUBMITTED for same ticker in last 15 minutes |

- Logs a `DECISION_VALIDATED` event to the ledger on every call
- Uses `decision.get("cycle_id")` if present so the event ties to the right cycle
- Accepts `account=None` and `ledger_path=None` to skip the respective checks (useful in tests)
- `positions` field supports both list-of-dicts (reconciliation format) and symbol-keyed dict (legacy algomind format)
- **Self-test:** `python agent/risk_engine.py` — 11 cases, all passing

### Reconciliation — `agent/reconciliation.py`

**Entry point:** `get_reconciled_portfolio() → dict`

Returns: `{cash, portfolio_value, positions: [{symbol, qty, market_value, avg_entry}]}`

**What it does:**
1. Fetches live account + positions from Alpaca
2. Reads the last `RECONCILIATION` event from `data/ledger.jsonl`
3. Computes drift (portfolio_value_delta, cash_delta, positions_added/removed, qty_changes)
4. Logs a `RECONCILIATION` event with `{current, drift, has_drift}`
5. Returns the live snapshot (never raises — returns zero-value fallback on Alpaca error)

**`get_alpaca_account()`** — separate helper for fetching the raw Alpaca account object (used by risk engine for `daytrade_count` PDT check).

**Self-test:** `python agent/reconciliation.py` — connects to Alpaca and prints live state.

### Positions Format Change
`reconciliation.get_reconciled_portfolio()` returns `positions` as a **list** (not a dict):
```python
[{"symbol": "NVDA", "qty": 0.05, "market_value": 5.00, "avg_entry": 100.0}]
```
This is intentional — more explicit than a symbol-keyed dict. `risk_engine.validate_trade()` handles both formats for backward compatibility.

### How risk_engine + reconciliation wire into run_cycle() (not yet done)
When integrating, the call order at cycle start should be:
```python
portfolio = reconciliation.get_reconciled_portfolio()
account   = reconciliation.get_alpaca_account()
decision  = agent.ask_claude(market_data, portfolio)
ok, reason = risk_engine.validate_trade(decision, portfolio, account, LEDGER_PATH)
if not ok:
    # skip execution
```

---

## April 16, 2026 — Session 7

### What Was Built Today
| File | Description | Status |
|------|-------------|--------|
| `agent/ledger.py` | Append-only event ledger — writes one JSON line per event to `data/ledger.jsonl` | ✅ Complete |
| `data/.gitkeep` | Placeholder so the `data/` directory is tracked by git | ✅ Complete |
| `.gitignore` | Added `data/ledger.jsonl` (ignore) and `!data/.gitkeep` (force-track) | ✅ Complete |

### Ledger Architecture

**File location:** `data/ledger.jsonl` — one JSON object per line, append-only.  
**Why append-only:** Partial crashes cannot corrupt earlier entries; each line is written atomically in its own `open/write/close`.

**Event schema:**
```json
{
  "timestamp": "2026-04-16T19:08:23.865Z",
  "cycle_id":  "fe33bf02-211b-4e1a-846d-08cf95d94d24",
  "event_type": "CYCLE_START",
  "payload":   { ... }
}
```

**Event types defined as module constants:**

| Constant | When to use |
|----------|-------------|
| `CYCLE_START` | Beginning of a `run_cycle()` call |
| `CYCLE_END` | End of `run_cycle()`, includes final result |
| `DECISION_PROPOSED` | Claude returns a raw decision |
| `DECISION_VALIDATED` | Decision passes guardrails check |
| `ORDER_SUBMITTED` | Alpaca order placed |
| `ORDER_FILLED` | Alpaca confirms fill |
| `ORDER_REJECTED` | Alpaca rejects the order |
| `POST_X` | Tweet posted to X |
| `POST_TELEGRAM` | Telegram alert sent |
| `DASHBOARD_UPDATED` | `data.json` written to disk |
| `ERROR` | Any caught exception worth recording |
| `MILESTONE` | Milestone threshold crossed |

**Public API:**
- `generate_cycle_id() → str` — new UUID4 each call
- `log_event(cycle_id, event_type, payload)` — appends one line; creates `data/` if needed
- `get_last_cycle() → dict | None` — last `CYCLE_START` event (for restart detection)
- `get_events_since(timestamp) → list[dict]` — all events after a UTC ISO string

**Why `data/ledger.jsonl` is git-ignored:**
The ledger is operational data, not source code. Committing it would add noise to every deploy push. On Railway the file persists naturally on the filesystem between restarts. If ever the container is replaced, the ledger starts fresh — it is a diagnostic aid, not a system-of-record.

**Self-test:** `python agent/ledger.py` writes 8 sample events and reads them back; exits 0 on success.

---

## Upcoming Sessions

### Session 2 (planned)
- Paper trading validation run
- Prompt tuning based on real Claude outputs
- Add `docs/decisions/` logging — write each trade decision to a markdown file
- Consider adding a `/status` Telegram command to query portfolio on demand

### Session 3 (planned)
- Go live with real $50
- Document the first real trade in the build log
- Post first X thread introducing the experiment
- Publish first Substack post: "I am AlgoMind. This is how I think."

---

## Performance History

| Date | Portfolio Value | P&L | vs S&P 500 |
|------|----------------|-----|------------|
| Apr 15, 2026 (start) | $50.00 | — | — |

*Updated automatically after each trade cycle.*
