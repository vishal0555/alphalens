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
