"""
agent/db.py — Postgres helpers for The Fifty Fund
Writes trades, AI log, and performance to shared Arena DB.

Connection strategy: prefer DATABASE_URL (internal Railway hostname) and
fall back to DATABASE_PUBLIC_URL on DNS/connect failures. The internal
hostname is faster but occasionally fails to resolve; the public URL works
reliably as a fallback. The chosen DSN is cached for the rest of the
session; if both candidates are unreachable, DB writes are disabled (logged
only) so a Postgres outage never blocks a trading cycle.
"""
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool, OperationalError

logger = logging.getLogger(__name__)

_pool = None
_pool_source: str | None = None      # "DATABASE_URL" or "DATABASE_PUBLIC_URL"
_pool_failed = False                  # latches True when every candidate has failed

_FALLBACK_SIGNS = ("translate host name", "could not connect")


def _candidates() -> list[tuple[str, str]]:
    """Return (label, dsn) pairs for every configured candidate, primary first."""
    out: list[tuple[str, str]] = []
    primary  = os.environ.get("DATABASE_URL")
    fallback = os.environ.get("DATABASE_PUBLIC_URL")
    if primary:
        out.append(("DATABASE_URL", primary))
    if fallback and fallback != primary:
        out.append(("DATABASE_PUBLIC_URL", fallback))
    return out


def _should_fallback(exc: Exception) -> bool:
    """True if exc looks like a DNS / connect failure we can recover from."""
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc).lower()
    return any(s in msg for s in _FALLBACK_SIGNS)


def _build_pool(skip: set[str] | None = None):
    """Try each candidate in order, returning the first pool that connects."""
    global _pool, _pool_failed, _pool_source
    skip = skip or set()
    for label, dsn in _candidates():
        if label in skip:
            continue
        try:
            p = pool.SimpleConnectionPool(1, 5, dsn=dsn)
            _pool, _pool_source = p, label
            logger.info("FF DB pool initialized via %s.", label)
            return p
        except Exception as exc:
            if _should_fallback(exc):
                logger.warning("DB init via %s failed (%s); trying next candidate.", label, exc)
                continue
            logger.error("DB init via %s raised: %s", label, exc)
            continue
    _pool_failed = True
    logger.error("All DB candidates exhausted; DB writes disabled for this session.")
    return None


def get_pool():
    """Lazy pool getter. Returns None if no DSN is reachable."""
    global _pool, _pool_failed
    if _pool is not None:
        return _pool
    if _pool_failed:
        return None
    if not _candidates():
        logger.warning("No DATABASE_URL or DATABASE_PUBLIC_URL set; DB writes disabled.")
        _pool_failed = True
        return None
    return _build_pool()


def _run(fn, *, default=None):
    """
    Run fn(pool) under the fallback contract:
      - if no pool can be built, return `default`
      - on a DNS/connect failure while currently on DATABASE_URL, rebuild
        from DATABASE_PUBLIC_URL and retry once
      - any other failure is swallowed (logged) so DB problems never crash
        the trading cycle
    """
    global _pool
    p = get_pool()
    if p is None:
        return default
    try:
        return fn(p)
    except Exception as exc:
        if _should_fallback(exc) and _pool_source == "DATABASE_URL":
            logger.warning("DB op failed on DATABASE_URL (%s); falling back to public.", exc)
            try:
                p.closeall()
            except Exception:
                pass
            _pool = None
            p = _build_pool(skip={"DATABASE_URL"})
            if p is None:
                return default
            try:
                return fn(p)
            except Exception as exc2:
                logger.error("DB op failed after fallback: %s", exc2)
                return default
        logger.error("DB op failed: %s", exc)
        return default


def insert_trade(cycle_id, action, ticker, dollar_amount, qty, price, reasoning, confidence, x_post=None):
    def _do(p):
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ff_trades (cycle_id, action, ticker, dollar_amount, qty, price, reasoning, confidence, x_post)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (cycle_id, action, ticker, dollar_amount, qty, price, reasoning, confidence, x_post))
            conn.commit()
            p.putconn(conn)
    _run(_do)


def insert_ai_log(message, tags):
    def _do(p):
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ff_ai_log (message, tags) VALUES (%s, %s)", (message, tags))
            conn.commit()
            p.putconn(conn)
    _run(_do)


def upsert_performance(date_str, portfolio_value, return_pct):
    def _do(p):
        with p.getconn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ff_performance (date, portfolio_value, return_pct)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (date) DO UPDATE SET portfolio_value = EXCLUDED.portfolio_value, return_pct = EXCLUDED.return_pct
                """, (date_str, portfolio_value, return_pct))
            conn.commit()
            p.putconn(conn)
    _run(_do)


def get_trades(limit=50):
    def _do(p):
        with p.getconn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM ff_trades ORDER BY created_at DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
            p.putconn(conn)
            return [dict(r) for r in rows]
    return _run(_do, default=[])


def get_ai_log(limit=100):
    def _do(p):
        with p.getconn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM ff_ai_log ORDER BY created_at DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
            p.putconn(conn)
            return [dict(r) for r in rows]
    return _run(_do, default=[])


def get_performance():
    def _do(p):
        with p.getconn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM ff_performance ORDER BY date ASC")
                rows = cur.fetchall()
            p.putconn(conn)
            return [dict(r) for r in rows]
    return _run(_do, default=[])
