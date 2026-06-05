"""
db.py — Postgres read queries for AlphaLens.

Read-only access to the fund tables written by alphalab. Each helper
returns:
  None  — DB unavailable / connection failed (page shows degraded state)
  False — row not found (caller usually 404s)
  dict  — success
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

_MARKET_TZ = ZoneInfo("America/New_York")

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


# ── Connection ──────────────────────────────────────────────────────────────

def _get_url() -> Optional[str]:
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if url:
        return url
    for env_file in (
        Path.home() / ".alphalens" / "dbconnector.env",
        Path(__file__).parent / "dbconnector.env",
    ):
        if not env_file.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
            url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        except ImportError:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith(("DATABASE_URL", "NEON_DATABASE_URL")):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if url:
            return url
    return None


def _conn():
    url = _get_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(url)
    # The fund tables live in the ai-thematic `ats` schema. Pin the search_path
    # to it (default `ats`, overridable via ALPHALENS_SCHEMA) so the app reads
    # the right schema even when no env var is set — e.g. on Vercel, where the
    # gitignored dbconnector.env is absent. Pinning to a single schema also keeps
    # unrelated tables invisible, so panels for tables not present here degrade
    # gracefully instead of erroring.
    schema = os.environ.get("ALPHALENS_SCHEMA", "ats")
    if schema:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
    return conn


def _safe(fn, *args, **kwargs):
    """Run a query helper; return None on any connection / SQL failure."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("db query failed: %s", exc)
        return None


# ── Universe ────────────────────────────────────────────────────────────────

def fetch_current_universe() -> Optional[dict]:
    """Return the active universe with picks, or None."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT universe_id, as_of_date, status, model, source, rationale,
                       layer_coverage, created_at
                  FROM universes
                 WHERE status = 'active'
                 ORDER BY as_of_date DESC, created_at DESC
                 LIMIT 1
            """)
            uni = cur.fetchone()
            if not uni:
                return False
            uni = dict(uni)
            cur.execute("""
                SELECT ticker, layer, weight_pct, rationale, rank
                  FROM universe_picks
                 WHERE universe_id = %s
                 ORDER BY rank ASC
            """, (str(uni["universe_id"]),))
            uni["picks"] = [dict(r) for r in cur.fetchall()]
            return uni
    return _safe(_q)


def held_tickers() -> set:
    """Tickers the fund currently holds (net nonzero position from fills).

    Used to mark which universe picks were actually acted on. Returns an empty
    set on any DB error so callers can treat it as 'none marked'.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT ticker
                  FROM executed_trades
                 GROUP BY ticker
                HAVING ABS(SUM(CASE WHEN lower(side) = 'buy' THEN qty ELSE -qty END)) > 0.0001
            """)
            return {r[0] for r in cur.fetchall()}
    return _safe(_q) or set()


def execution_by_ticker() -> dict:
    """Per-ticker execution status from fills: entry price, net position, fill count.

    Used to turn the universe into an execution checklist (filled / pending,
    held or not). Empty dict on any DB error.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT ticker,
                       MAX(fill_price) FILTER (WHERE lower(side) = 'buy') AS entry_price,
                       SUM(CASE WHEN lower(side) = 'buy' THEN qty ELSE -qty END) AS net,
                       COUNT(*) AS fills
                  FROM executed_trades
                 GROUP BY ticker
            """)
            out = {}
            for r in cur.fetchall():
                out[r[0]] = {
                    "entry_price": float(r[1]) if r[1] is not None else None,
                    "net": float(r[2]) if r[2] is not None else 0.0,
                    "fills": int(r[3]),
                }
            return out
    return _safe(_q) or {}


def fetch_previous_universe(before_date: str) -> Optional[dict]:
    """Yesterday's universe, for diff computation on /universe."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT universe_id, as_of_date
                  FROM universes
                 WHERE status IN ('active','superseded') AND as_of_date < %s
                 ORDER BY as_of_date DESC, created_at DESC
                 LIMIT 1
            """, (before_date,))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("""
                SELECT ticker, layer, weight_pct
                  FROM universe_picks
                 WHERE universe_id = %s
            """, (str(row["universe_id"]),))
            return {
                "as_of_date": row["as_of_date"],
                "picks_by_ticker": {p["ticker"]: dict(p) for p in cur.fetchall()},
            }
    return _safe(_q)


# ── FundBriefing (today's morning) ──────────────────────────────────────────

def fetch_current_briefing() -> Optional[dict]:
    """Most-recent FundBriefing payload (full)."""
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT payload FROM fund_briefings
                 ORDER BY as_of_date DESC, created_at DESC
                 LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return False
            payload = row[0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload
    return _safe(_q)


# ── NAV ─────────────────────────────────────────────────────────────────────

STARTING_NAV_DEFAULT = 100_000.0


def fetch_current_nav() -> Optional[dict]:
    """{starting_nav, ending_nav, realised_pnl, as_of_date} of the most recent
    mark, or a synthetic seed dict if no marks yet.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT as_of_date, starting_nav, realised_pnl, ending_nav, created_at
                  FROM fund_nav
                 ORDER BY as_of_date DESC, created_at DESC
                 LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return {
                    "as_of_date":   datetime.now(_MARKET_TZ).date().isoformat(),
                    "starting_nav": STARTING_NAV_DEFAULT,
                    "realised_pnl": 0.0,
                    "ending_nav":   STARTING_NAV_DEFAULT,
                    "seed":         True,
                }
            return {
                "as_of_date":   row[0],
                "starting_nav": float(row[1]),
                "realised_pnl": float(row[2]),
                "ending_nav":   float(row[3]),
                "seed":         False,
            }
    return _safe(_q)


def fetch_nav_history(limit: int = 60) -> Optional[list[dict]]:
    """Recent NAV marks, oldest → newest for charting."""
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT as_of_date, starting_nav, realised_pnl, ending_nav, created_at
                  FROM fund_nav
                 ORDER BY as_of_date DESC, created_at DESC
                 LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        rows = list(reversed(rows))   # oldest → newest
        return [
            {
                "as_of_date":   r[0],
                "starting_nav": float(r[1]),
                "realised_pnl": float(r[2]),
                "ending_nav":   float(r[3]),
            }
            for r in rows
        ]
    return _safe(_q)


# ── Sessions / positions / fills ────────────────────────────────────────────

def fetch_today_session() -> Optional[dict]:
    """Today's most-recent session header (or most recent regardless of date if
    nothing today). Returns {plan_id, session_id, status, briefing_date,
    created_at, completed_at, narrative, item_count, fill_count}.
    """
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.plan_id, p.session_id, p.status, p.briefing_date, p.narrative,
                       p.created_at, p.completed_at,
                       (SELECT COUNT(*) FROM pm_plan_items i WHERE i.plan_id = p.plan_id) AS item_count,
                       (SELECT COUNT(*) FROM executed_trades t
                          WHERE t.plan_id = p.plan_id AND t.exit_reason IS NULL) AS fill_count
                  FROM pm_plans p
                 ORDER BY p.created_at DESC
                 LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return False
            return dict(row)
    return _safe(_q)


def nav_marked_for_date(as_of_date: str) -> Optional[bool]:
    """True iff a fund_nav row exists for a session whose briefing_date
    matches `as_of_date`. Used by the Schedule's "Close · mark book"
    event so it reads `done` only when today's session actually wrote
    NAV — not just because the wall-clock crossed 16:00 ET.

    Joins fund_nav.session_id ← pm_plans to make sure the NAV row came
    from a *plan that belongs to today's trading day*, not from a carry
    plan that happens to be completing on today's calendar.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM fund_nav n
                  JOIN pm_plans p ON p.session_id = n.session_id
                 WHERE p.briefing_date = %s
                 LIMIT 1
            """, (as_of_date,))
            return cur.fetchone() is not None
    return _safe(_q)


def debrief_coverage_for_date(as_of_date: str) -> Optional[dict]:
    """Are all closed items on `as_of_date`'s plan debriefed?

    Returns {"closed": int, "undebriefed": int}. The Schedule widget's
    Debrief event uses this directly: done iff closed > 0 and
    undebriefed == 0, regardless of whether the plan itself has
    settled — overnight holds keep the plan `active` for days, but
    every closed item can be debriefed in the meantime.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE i.status IN (
                      'exited_target','exited_stop','exited_rotation',
                      'exited_cancel','expired','cancelled'
                  )) AS closed,
                  COUNT(*) FILTER (WHERE i.status IN (
                      'exited_target','exited_stop','exited_rotation',
                      'exited_cancel','expired','cancelled'
                  ) AND d.debrief_id IS NULL) AS undebriefed
                  FROM pm_plan_items i
                  JOIN pm_plans p ON p.plan_id = i.plan_id
             LEFT JOIN debriefs d ON d.plan_item_id = i.item_id
                 WHERE p.briefing_date = %s
            """, (as_of_date,))
            row = cur.fetchone()
            return {"closed": int(row[0]), "undebriefed": int(row[1])}
    return _safe(_q)


def fetch_carrying_positions() -> Optional[list[dict]]:
    """Open positions held over from a prior, already-settled plan.

    Every row is a plan_item in status='carrying_overnight' — the
    ExitMonitor is still watching its stop/target, but the position
    no longer belongs to today's plan.

    Returns each position with the entry fill price (so the dashboard
    can render an unrealised P&L hint once a live quote is available).
    """
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT i.item_id, i.plan_id, i.session_id, i.ticker, i.side,
                       i.size_shares, i.entry_low, i.entry_high, i.target, i.stop,
                       p.briefing_date AS opened_on,
                       (SELECT fill_price FROM executed_trades
                         WHERE plan_item_id = i.item_id AND exit_reason IS NULL
                         LIMIT 1) AS entry_fill_price
                  FROM pm_plan_items i
                  JOIN pm_plans p ON p.plan_id = i.plan_id
                 WHERE i.status = 'carrying_overnight'
                 ORDER BY p.briefing_date ASC, i.ticker ASC
            """)
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


def fetch_session_items(plan_id: str) -> Optional[list[dict]]:
    """All plan items for a session with linked entry/exit fills."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT i.item_id, i.ticker, i.side, i.entry_low, i.entry_high,
                       i.target, i.stop, i.size_shares, i.status, i.rationale,
                       (SELECT fill_price FROM executed_trades
                         WHERE plan_item_id = i.item_id AND exit_reason IS NULL
                         LIMIT 1) AS entry_fill_price,
                       (SELECT fill_price FROM executed_trades
                         WHERE plan_item_id = i.item_id AND exit_reason IS NOT NULL
                         LIMIT 1) AS exit_fill_price,
                       (SELECT exit_reason FROM executed_trades
                         WHERE plan_item_id = i.item_id AND exit_reason IS NOT NULL
                         LIMIT 1) AS exit_reason
                  FROM pm_plan_items i
                 WHERE i.plan_id = %s
                 ORDER BY i.ticker ASC
            """, (plan_id,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


def session_pnl_summary(plan_id: str) -> Optional[dict]:
    """Realised PnL across all closed round-trips in a plan."""
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(
                    (x.fill_price - e.fill_price) * e.qty
                    * CASE e.side WHEN 'long' THEN 1.0 ELSE -1.0 END
                ), 0) AS pnl
                  FROM executed_trades e
                  JOIN executed_trades x
                    ON x.plan_item_id = e.plan_item_id
                   AND x.exit_reason IS NOT NULL
                 WHERE e.plan_id = %s AND e.exit_reason IS NULL
            """, (plan_id,))
            return {"realised_pnl": float(cur.fetchone()[0] or 0.0)}
    return _safe(_q)


# ── Debriefs ────────────────────────────────────────────────────────────────

def list_recent_debriefs(limit: int = 30) -> Optional[list[dict]]:
    """Most-recent debriefs, newest first, with minimal fields for the list view."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT debrief_id, plan_item_id, session_id, ticker, side,
                       outcome, pnl_pct, pnl_abs, hold_time_minutes, verdict,
                       created_at
                  FROM debriefs
                 ORDER BY created_at DESC
                 LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


def fetch_debrief(debrief_id: str) -> Optional[dict]:
    """Full debrief with what_worked / what_failed arrays + lessons."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT debrief_id, plan_item_id, plan_id, session_id, ticker, side,
                       outcome, pnl_pct, pnl_abs, hold_time_minutes,
                       entry_slippage_bps, exit_slippage_bps,
                       what_worked, what_failed, verdict, model, payload, created_at
                  FROM debriefs WHERE debrief_id = %s
            """, (debrief_id,))
            row = cur.fetchone()
            if not row:
                return False
            row = dict(row)
            payload = row.get("payload")
            if isinstance(payload, str):
                row["payload"] = json.loads(payload)
            cur.execute("""
                SELECT lesson_id, scope, applies_when, guidance
                  FROM debrief_lessons WHERE debrief_id = %s
                 ORDER BY created_at ASC
            """, (debrief_id,))
            row["lessons"] = [dict(r) for r in cur.fetchall()]
            return row
    return _safe(_q)


def fetch_recent_lessons(limit: int = 8) -> Optional[list[dict]]:
    """Most-recent lessons across all debriefs — for the dashboard."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ticker, scope, applies_when, guidance, created_at
                  FROM debrief_lessons
                 ORDER BY created_at DESC
                 LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)
