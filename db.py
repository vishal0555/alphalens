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

def fetch_current_universe(as_of: Optional[str] = None) -> Optional[dict]:
    """Return the active universe with picks, or the universe curated on a given
    `as_of` date (any status — past universes are 'superseded'). None if missing."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if as_of:
                cur.execute("""
                    SELECT universe_id, as_of_date, status, model, source, rationale,
                           layer_coverage, created_at
                      FROM universes
                     WHERE as_of_date = %s
                     ORDER BY created_at DESC
                     LIMIT 1
                """, (as_of,))
            else:
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


def fetch_book(limit: int = 30, as_of: Optional[str] = None) -> Optional[list[dict]]:
    """Active universe enriched with execution + EOD score — the connected book.

    One row per pick carrying its whole arc: layer, target weight, the pipeline
    outcome, conviction and (once scored) the EOD score, plus its decision_id so
    each name links to its full decision record. With `as_of`, the book for the
    universe curated on that date (for the historical date view).
    """
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if as_of:
                u_cte = ("WITH u AS (SELECT universe_id FROM universes WHERE as_of_date = %s "
                         "ORDER BY created_at DESC LIMIT 1)")
                params: tuple = (as_of, limit)
            else:
                u_cte = ("WITH u AS (SELECT universe_id FROM universes WHERE status = 'active' "
                         "ORDER BY as_of_date DESC, created_at DESC LIMIT 1)")
                params = (limit,)
            cur.execute(u_cte + """
                SELECT p.ticker, p.layer, p.weight_pct,
                       d.decision_id, d.pipeline_outcome, d.conviction, d.score
                  FROM universe_picks p
                  JOIN u ON u.universe_id = p.universe_id
                  LEFT JOIN stock_decisions d
                    ON d.universe_id = p.universe_id AND d.ticker = p.ticker
                 ORDER BY p.weight_pct DESC
                 LIMIT %s
            """, params)
            rows = []
            for r in cur.fetchall():
                r = dict(r)
                if r.get("decision_id") is not None:
                    r["decision_id"] = str(r["decision_id"])
                rows.append(r)
            return rows
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


def holdings_marked() -> dict:
    """Per-ticker live marks + unrealized P&L from the ``current_holdings`` view.

    The view is the makeup of NAV: the latest portfolio snapshot's average entry
    cost, the mark it was valued at, market value, unrealized P&L and the name's
    share of NAV. Keyed by ticker. Empty dict if the view is unavailable (e.g.
    before migration 0009 / before the first marked snapshot).
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT ticker, quantity, avg_cost, market_price, market_value,
                       cost_basis, unrealized_pnl, unrealized_pnl_pct, nav_weight_pct
                  FROM current_holdings
            """)
            out = {}
            for r in cur.fetchall():
                out[r[0]] = {
                    "quantity": float(r[1]) if r[1] is not None else None,
                    "avg_cost": float(r[2]) if r[2] is not None else None,
                    "market_price": float(r[3]) if r[3] is not None else None,
                    "market_value": float(r[4]) if r[4] is not None else None,
                    "cost_basis": float(r[5]) if r[5] is not None else None,
                    "unrealized_pnl": float(r[6]) if r[6] is not None else None,
                    "unrealized_pnl_pct": float(r[7]) if r[7] is not None else None,
                    "nav_weight_pct": float(r[8]) if r[8] is not None else None,
                }
            return out
    return _safe(_q) or {}


def held_sides() -> dict:
    """Currently-held tickers mapped to 'long' or 'short' (signed net from fills)."""
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            # Net value above $1 (matches the fund's dust rule) — so sub-dollar
            # exit residuals aren't shown as held longs/shorts.
            cur.execute("""
                SELECT ticker, SUM(CASE WHEN lower(side)='buy' THEN qty ELSE -qty END) AS net
                  FROM executed_trades GROUP BY ticker
                HAVING ABS(SUM(CASE WHEN lower(side)='buy' THEN qty ELSE -qty END)) * AVG(fill_price) > 1
            """)
            return {r[0]: ("short" if float(r[1]) < 0 else "long") for r in cur.fetchall()}
    return _safe(_q) or {}


def book_exposure() -> Optional[dict]:
    """Long/short counts and gross/net exposure (% of NAV) from current fills.

    Position value uses the average fill price as a proxy (alphalens has no live
    marks), so gross/net are approximate — enough for a posture-at-a-glance read.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT SUM(CASE WHEN lower(side)='buy' THEN qty ELSE -qty END) AS net,
                       AVG(fill_price) AS px
                  FROM executed_trades GROUP BY ticker
                HAVING ABS(SUM(CASE WHEN lower(side)='buy' THEN qty ELSE -qty END)) * AVG(fill_price) > 1
            """)
            longs = shorts = 0
            gross = net = 0.0
            for r in cur.fetchall():
                n = float(r[0]); px = float(r[1]) if r[1] is not None else 0.0
                val = n * px
                shorts += n < 0
                longs += n > 0
                gross += abs(val); net += val
            if longs + shorts == 0:
                return None
            cur.execute("SELECT ending_nav FROM fund_nav ORDER BY as_of_date DESC, created_at DESC LIMIT 1")
            nav_row = cur.fetchone()
            nav = float(nav_row[0]) if nav_row else None
            return {"longs": longs, "shorts": shorts,
                    "gross_pct": round(gross / nav * 100, 0) if nav else None,
                    "net_pct": round(net / nav * 100, 0) if nav else None}
    return _safe(_q)


def risk_exits(as_of_date: str) -> list[dict]:
    """Intraday stop-loss exits (risk-monitor fills) on a date — ticker + time."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT payload->>'symbol' AS ticker,
                       to_char((COALESCE((payload->>'filled_at')::timestamptz, timestamp)
                                AT TIME ZONE 'America/New_York'), 'HH24:MI') AS at
                  FROM events
                 WHERE subject = 'trade.fill' AND source = 'risk-monitor'
                   AND (COALESCE((payload->>'filled_at')::timestamptz, timestamp)
                        AT TIME ZONE 'America/New_York')::date = %s::date
                 ORDER BY timestamp DESC
            """, (as_of_date,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q) or []


def macro_events(as_of_date: str) -> list[dict]:
    """The day's captured US macro releases (from macro_events, written by the fund)."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT event_time AS time, name, forecast, actual, previous, released, surprise
                  FROM macro_events WHERE as_of_date = %s ORDER BY event_time
            """, (as_of_date,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q) or []


def spx_move(as_of_date: str) -> Optional[float]:
    """Captured S&P 500 day move for a date (market-reaction context)."""
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT spx_pct FROM macro_market WHERE as_of_date = %s", (as_of_date,))
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
    return _safe(_q)


def fetch_day_narrative(as_of: Optional[str] = None) -> Optional[dict]:
    """The fund's stored end-of-day narrative for a date (or the latest).

    Read-only: the narrative is composed and persisted by the trading system at
    the close (table ``day_narratives``); alphalens only displays it. Returns None
    when no narrative has been written for the day (e.g. before the debrief runs).
    """
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if as_of:
                cur.execute("SELECT * FROM day_narratives WHERE as_of_date = %s", (as_of,))
            else:
                cur.execute(
                    "SELECT * FROM day_narratives ORDER BY as_of_date DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else None
    return _safe(_q)


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


# ── NAV ─────────────────────────────────────────────────────────────────────

STARTING_NAV_DEFAULT = 100_000.0


def fetch_current_nav(as_of: Optional[str] = None) -> Optional[dict]:
    """{starting_nav, ending_nav, realised_pnl, as_of_date} of the most recent
    mark (or the most recent on/before `as_of`), or a synthetic seed dict if none.
    """
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            if as_of:
                cur.execute("""
                    SELECT as_of_date, starting_nav, realised_pnl, ending_nav, created_at
                      FROM fund_nav
                     WHERE as_of_date <= %s
                     ORDER BY as_of_date DESC, created_at DESC
                     LIMIT 1
                """, (as_of,))
            else:
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


def fetch_nav_history(limit: int = 60, as_of: Optional[str] = None) -> Optional[list[dict]]:
    """Recent NAV marks, oldest → newest for charting (up to `as_of` if given)."""
    def _q():
        with _conn() as conn, conn.cursor() as cur:
            if as_of:
                cur.execute("""
                    SELECT as_of_date, starting_nav, realised_pnl, ending_nav, created_at
                      FROM fund_nav
                     WHERE as_of_date <= %s
                     ORDER BY as_of_date DESC, created_at DESC
                     LIMIT %s
                """, (as_of, limit))
            else:
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


# ── Oversight: today's run liveness ─────────────────────────────────────────

def today_run(as_of: Optional[str] = None) -> Optional[dict]:
    """Did the autonomous cycle run? curate -> rebalance -> debrief. For `as_of`
    (a past date) the day is over, so anything that didn't happen reads overdue."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if as_of:
                today = as_of
                now_h = 24.0   # the day is fully past
            else:
                cur.execute("""
                    SELECT (now() AT TIME ZONE 'America/New_York')::date::text AS d,
                           extract(hour FROM now() AT TIME ZONE 'America/New_York')
                           + extract(minute FROM now() AT TIME ZONE 'America/New_York') / 60.0 AS h
                """)
                r = cur.fetchone(); today = r["d"]; now_h = float(r["h"])
            cur.execute("SELECT count(*) AS c FROM universes WHERE as_of_date = %s", (today,))
            curated = cur.fetchone()["c"]
            cur.execute("""SELECT count(*) AS c FROM executed_trades
                            WHERE (created_at AT TIME ZONE 'America/New_York')::date = %s::date""", (today,))
            fills = cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM stock_decisions WHERE as_of_date = %s AND score IS NOT NULL", (today,))
            scored = cur.fetchone()["c"]

            def step(ok, n, unit, due):
                if ok:
                    return {"state": "done", "detail": "%d %s" % (n, unit)}
                return {"state": "overdue" if now_h >= due else "pending", "detail": ""}

            return {
                "curate":    {"label": "Scout · curate",   **step(curated > 0, curated, "universe", 9.5)},
                "rebalance": {"label": "Rebalance · trade", **step(fills > 0, fills, "fills", 9.6)},
                "debrief":   {"label": "Debrief · score",   **step(scored > 0, scored, "scored", 16.0)},
            }
    return _safe(_q)


# ── Oversight: trust verdict + exceptions ───────────────────────────────────

def decision_confidence() -> Optional[dict]:
    """Aggregate trust signal for the latest scored session (+ trend vs prior)."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT as_of_date, avg(score)::float AS avg_score, count(*) AS n,
                       count(*) FILTER (WHERE pipeline_outcome = 'executed') AS executed
                  FROM stock_decisions WHERE score IS NOT NULL
                 GROUP BY as_of_date ORDER BY as_of_date DESC LIMIT 2
            """)
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return None
            cur.execute("SELECT count(DISTINCT as_of_date) AS c FROM stock_decisions WHERE score IS NOT NULL")
            sessions = cur.fetchone()["c"]
            latest, prior = rows[0], (rows[1] if len(rows) > 1 else None)
            trend = None
            if prior:
                delta = latest["avg_score"] - prior["avg_score"]
                trend = "up" if delta > 0.01 else ("down" if delta < -0.01 else "flat")
            return {"avg_score": latest["avg_score"], "n": latest["n"],
                    "executed": latest["executed"], "as_of_date": latest["as_of_date"],
                    "trend": trend, "sessions": sessions}
    return _safe(_q)


# ── Decision journal (stock_decisions) ──────────────────────────────────────

def list_decisions(limit: int = 60) -> Optional[list[dict]]:
    """Recent scored decisions — the EOD decision journal, best-scored first."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT decision_id, ticker, layer, scout_decision, pipeline_outcome,
                       weight_pct, score, revised_view, reflection,
                       morning_price, eod_price, macro_regime, as_of_date, scored_at,
                       lessons_applied
                  FROM stock_decisions
                 WHERE score IS NOT NULL
                 ORDER BY as_of_date DESC, score DESC
                 LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


def fetch_decision(decision_id: str) -> Optional[dict]:
    """One decision with the full morning -> EOD record and lineage ids."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT decision_id, universe_id, ticker, layer, scout_decision,
                       pipeline_outcome, weight_pct, conviction, scout_rationale,
                       pipeline_rationale, morning_sentiment, morning_price, macro_regime,
                       eod_sentiment, eod_price, revised_view, score, reflection,
                       as_of_date, scored_at, signal_id, order_id, macro_id, lessons_applied
                  FROM stock_decisions WHERE decision_id = %s::uuid
            """, (decision_id,))
            row = cur.fetchone()
            return dict(row) if row else False
    return _safe(_q)


# ── Momentum rotation book (written by the thematic rotation runner) ──────────

def rotation_nav_series(limit: int = 800) -> Optional[list[dict]]:
    """Daily equity curve of the rotation book (oldest→newest), with benchmark."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT as_of_date, nav, ret_pct, benchmark_nav, drawdown_pct, trend_below
                  FROM (
                    SELECT * FROM rotation_nav ORDER BY as_of_date DESC LIMIT %s
                  ) t
                 ORDER BY as_of_date ASC
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


def rotation_latest() -> Optional[dict]:
    """The most recent rotation_nav row — current NAV + risk flags for the tiles."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT as_of_date, nav, benchmark_nav, gross, cash_weight,
                       trend_below, drawdown_pct
                  FROM rotation_nav ORDER BY as_of_date DESC LIMIT 1
            """)
            row = cur.fetchone()
            return dict(row) if row else None
    return _safe(_q)


def rotation_sleeves() -> Optional[list[dict]]:
    """Latest per-sleeve momentum leaderboard + current weights (rank order)."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT sleeve, instrument, ret_3m, ret_6m, blended, vol, score,
                       rank, eligible, state, weight
                  FROM rotation_sleeves
                 WHERE as_of_date = (SELECT max(as_of_date) FROM rotation_sleeves)
                 ORDER BY rank ASC NULLS LAST, weight DESC
            """)
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


def rotation_rebalances(limit_dates: int = 12) -> Optional[list[dict]]:
    """Recent monthly rebalances (most recent first), one row per sleeve held."""
    def _q():
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT rebalance_date, sleeve, instrument, weight
                  FROM rotation_rebalances
                 WHERE rebalance_date IN (
                    SELECT DISTINCT rebalance_date FROM rotation_rebalances
                     ORDER BY rebalance_date DESC LIMIT %s
                 )
                 ORDER BY rebalance_date DESC, weight DESC
            """, (limit_dates,))
            return [dict(r) for r in cur.fetchall()]
    return _safe(_q)


