"""
db.py — Postgres read queries for AlphaLens.

Reads briefings and signals from the tables written by alphalab.

Tables:
  briefings: id, date, generated_at, model, mode, payload(JSONB)
  signals:   id, date, ticker, generated_at, label, final_score, payload(JSONB)

Returns:
  None   — DB unavailable (show Coming Soon)
  False  — Row not found (404)
  data   — success
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras


def _get_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    candidates = [
        Path.home() / ".alphalens" / "dbconnector.env",
        Path(__file__).parent / "dbconnector.env",
    ]
    for env_file in candidates:
        if not env_file.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
            url = os.environ.get("DATABASE_URL")
        except ImportError:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("DATABASE_URL"):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if url:
            return url
    return url


def _conn():
    url = _get_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url)


def list_briefings(limit: int = 20) -> list[dict] | None:
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, date, model, mode, generated_at "
                    "FROM briefings ORDER BY generated_at DESC LIMIT %s",
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return None


def get_briefing(row_id: int) -> dict | None | bool:
    """Returns payload dict, None (DB down), or False (not found)."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT payload FROM briefings WHERE id = %s", (row_id,))
                row = cur.fetchone()
                if not row:
                    return False
                return dict(row)["payload"]
    except Exception:
        return None


def get_latest_briefing_id() -> int | None:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM briefings ORDER BY generated_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def list_signals(limit: int = 20) -> list[dict] | None:
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, date, ticker, label, final_score, generated_at "
                    "FROM signals ORDER BY generated_at DESC LIMIT %s",
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return None


def get_signal(row_id: int) -> dict | None | bool:
    """Returns payload dict, None (DB down), or False (not found)."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT payload FROM signals WHERE id = %s", (row_id,))
                row = cur.fetchone()
                if not row:
                    return False
                return dict(row)["payload"]
    except Exception:
        return None


def get_latest_signal_id(ticker: str | None = None) -> int | None:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if ticker:
                    cur.execute(
                        "SELECT id FROM signals WHERE ticker = %s "
                        "ORDER BY generated_at DESC LIMIT 1",
                        (ticker,),
                    )
                else:
                    cur.execute(
                        "SELECT id FROM signals ORDER BY generated_at DESC LIMIT 1"
                    )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


# ── Manual trades (user-keyed fills) ───────────────────────────────────────────

_MANUAL_TRADES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS manual_trades (
        id            BIGSERIAL    PRIMARY KEY,
        briefing_id   INTEGER      NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
        ticker        TEXT         NOT NULL,
        side          TEXT         NOT NULL,
        qty           DOUBLE PRECISION NOT NULL,
        fill_price    DOUBLE PRECISION NOT NULL,
        filled_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        notes         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS manual_trades_briefing_idx ON manual_trades(briefing_id, filled_at DESC)",
    "CREATE INDEX IF NOT EXISTS manual_trades_ticker_idx ON manual_trades(ticker, filled_at DESC)",
]


def _ensure_manual_trades_schema(conn) -> None:
    """Idempotently create manual_trades. Runs alongside writes so AlphaLens
    can stand on its own without depending on AlphaLab having migrated first.
    """
    with conn.cursor() as cur:
        for stmt in _MANUAL_TRADES_DDL:
            cur.execute(stmt)


def insert_manual_trade(
    briefing_id: int,
    ticker: str,
    side: str,
    qty: float,
    fill_price: float,
    notes: str | None,
) -> int | None:
    """Insert a manually-keyed trade fill. Returns new id, or None on failure."""
    try:
        with _conn() as conn:
            _ensure_manual_trades_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO manual_trades (briefing_id, ticker, side, qty, fill_price, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (briefing_id, ticker, side, qty, fill_price, notes or None),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
    except Exception:
        return None


def list_manual_trades(briefing_id: int) -> list[dict] | None:
    """Return fills for a briefing in reverse-chronological order. None on DB error."""
    try:
        with _conn() as conn:
            _ensure_manual_trades_schema(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, briefing_id, ticker, side, qty, fill_price, filled_at, notes
                      FROM manual_trades
                     WHERE briefing_id = %s
                     ORDER BY filled_at DESC, id DESC
                    """,
                    (briefing_id,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return None


def delete_manual_trade(trade_id: int, briefing_id: int) -> bool:
    """Delete one fill (scoped to its briefing). Returns True on success."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM manual_trades WHERE id = %s AND briefing_id = %s",
                    (trade_id, briefing_id),
                )
                deleted = cur.rowcount
            conn.commit()
            return deleted > 0
    except Exception:
        return False
