"""US macro economic calendar for the dashboard's "Macro today" card.

Pulls the day's scheduled US releases from Nasdaq's free economic-calendar API
(no key), keeps the market-moving ones, and reports how each came in vs the
consensus once it's out — the surprise that drives the market's reaction.
Cached briefly and fails soft (empty list) so a fetch hiccup never breaks the page.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

_URL = "https://api.nasdaq.com/api/calendar/economicevents?date={date}"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_TIMEOUT = 12
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 600  # 10 min

# Market-moving US releases we surface (substring match on the event name).
_KEEP = (
    "core cpi", "cpi", "core ppi", "ppi", "core pce", "pce price",
    "nonfarm payrolls", "unemployment rate", "average hourly earnings",
    "initial jobless claims", "retail sales", "gdp", "ism manufacturing",
    "ism services", "ism non-manufacturing", "fomc", "interest rate decision",
    "fed interest rate", "consumer sentiment", "consumer confidence",
    "durable goods", "jolts", "adp employment", "ppi",
)
# …but drop sub-series, nowcasts and minor weekly prints — keep the headline releases.
_DROP = ("index", "n.s.a", "s.a.", "s.a ", "cleveland", "real earnings",
         "gdpnow", "weekly", "redbook", "atlanta fed", "current account",
         "price index", "deflator")


def _num(s: Any) -> Optional[float]:
    if not s or not str(s).strip():
        return None
    t = str(s).strip().replace("%", "").replace(",", "")
    mult = 1.0
    if t and t[-1] in "BMK":
        mult = {"B": 1e9, "M": 1e6, "K": 1e3}[t[-1]]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _keep(name: str) -> bool:
    n = name.lower()
    if any(d in n for d in _DROP):
        return False
    return any(k in n for k in _KEEP)


def macro_events(as_of_date: str) -> list[dict]:
    """The day's notable US macro releases, with surprise vs consensus once out.

    Each item: {time, name, forecast, actual, previous, released, surprise}
    where surprise is 'above' | 'below' | 'inline' | None (no consensus / not out).
    """
    cached = _CACHE.get(as_of_date)
    if cached and time.time() - cached[0] < _TTL:
        return cached[1]
    try:
        r = requests.get(_URL.format(date=as_of_date), headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        rows = (r.json().get("data") or {}).get("rows") or []
    except (requests.RequestException, ValueError):
        return _CACHE.get(as_of_date, (0, []))[1]

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in rows:
        if e.get("country") != "United States":
            continue
        name = (e.get("eventName") or "").strip()
        if not name or not _keep(name):
            continue
        key = (e.get("gmt", ""), name)
        if key in seen:
            continue
        seen.add(key)
        actual, cons = e.get("actual"), e.get("consensus")
        a, c = _num(actual), _num(cons)
        released = bool(actual and str(actual).strip())
        surprise = None
        if released and a is not None and c is not None:
            surprise = "above" if a > c else ("below" if a < c else "inline")
        out.append({
            "time": e.get("gmt", ""),
            "name": name,
            "forecast": (str(cons).strip() or None) if cons else None,
            "actual": str(actual).strip() if released else None,
            "previous": (str(e.get("previous")).strip() or None) if e.get("previous") else None,
            "released": released,
            "surprise": surprise,
        })
    out.sort(key=lambda x: x["time"])
    _CACHE[as_of_date] = (time.time(), out)
    return out


_SPX: dict[str, tuple[float, Optional[float]]] = {}


def spx_move() -> Optional[float]:
    """S&P 500 day % change — overall market reaction context (cached 5 min)."""
    c = _SPX.get("v")
    if c and time.time() - c[0] < 300:
        return c[1]
    move: Optional[float] = None
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d",
                         headers=_HEADERS, timeout=_TIMEOUT)
        meta = r.json()["chart"]["result"][0]["meta"]
        px, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose") or meta.get("previousClose")
        if px and prev:
            move = round((px / prev - 1) * 100, 2)
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        move = None
    _SPX["v"] = (time.time(), move)
    return move
