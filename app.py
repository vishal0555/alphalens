"""
app.py — AlphaLens Flask app (fund-mode dashboard).

Read-only mobile-first viewer of the AlphaLab fund:
  /            Dashboard — NAV, posture, universe tiles, positions, lessons
  /universe    Today's universe with diff vs yesterday
  /morning     Per-pick playbooks (entry/stop/target/action)
  /positions   Today's session — filled / exited / no_fill, with PnL
  /debriefs    List of recent debriefs
  /debrief/<id>  One debrief detail with lessons
  /nav         NAV history (table + simple sparkline)

Auth: single-password gate (AUTH_PASSWORD env). Same as before.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Market timezone — "today" on the dashboard means the trading day in ET,
# regardless of where the server runs (Vercel is UTC).
_MARKET_TZ = ZoneInfo("America/New_York")


def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)

from flask import Flask, abort, redirect, render_template, request, session, url_for

# Load DB connection env — ~/.alphalens/dbconnector.env takes precedence.
try:
    from dotenv import load_dotenv
    for _env in [
        Path.home() / ".alphalens" / "dbconnector.env",
        Path(__file__).parent / "dbconnector.env",
    ]:
        if _env.exists():
            load_dotenv(_env, override=False)
except ImportError:
    pass

import db as _db

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")


@app.after_request
def _no_cache(response):
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ── Auth ────────────────────────────────────────────────────────────────────

def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = os.environ.get("AUTH_PASSWORD", "")
        if password and request.form.get("password") == password:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Jinja filters (PM-friendly formatting) ──────────────────────────────────

@app.template_filter("money")
def _money(n):
    if n is None: return "—"
    try: return "${:,.0f}".format(float(n))
    except (TypeError, ValueError): return "—"


@app.template_filter("money2")
def _money2(n):
    if n is None: return "—"
    try: return "${:,.2f}".format(float(n))
    except (TypeError, ValueError): return "—"


@app.template_filter("signed_money")
def _signed_money(n):
    if n is None: return "—"
    try: x = float(n)
    except (TypeError, ValueError): return "—"
    sign = "+" if x > 0 else ("−" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


@app.template_filter("signed_pct")
def _signed_pct(n):
    if n is None: return "—"
    try: return "{:+.2f}%".format(float(n))
    except (TypeError, ValueError): return "—"


@app.template_filter("score_class")
def _score_class(s):
    try:
        x = float(s)
    except (TypeError, ValueError):
        return "flat"
    if x >= 0.6: return "pos"
    if x <= 0.4: return "neg"
    return "flat"


@app.template_filter("pnl_class")
def _pnl_class(n):
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "flat"
    if x > 0: return "pos"
    if x < 0: return "neg"
    return "flat"


@app.template_filter("short_dt")
def _short_dt(s):
    if not s: return "—"
    try:
        if isinstance(s, str):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = s
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return str(s)[:16]


# ── Routes ──────────────────────────────────────────────────────────────────

# Display order for the AI stack — from the user-facing top down to the
# foundational compute / power layers. Sovereign (non-US exposure) is an
# orthogonal axis so it tails the list.
_LAYER_ORDER = [
    "application", "device", "model_lab", "hyperscaler",
    "infra", "power", "silicon", "sovereign",
]


def _group_picks_by_layer(picks: list[dict]) -> list[dict]:
    """[(layer, [pick, pick, ...], total_weight), ...] sorted by total weight desc.

    Picks within a layer are sorted by weight desc. Empty layers are dropped.
    """
    by_layer: dict[str, list[dict]] = {}
    for p in picks or []:
        by_layer.setdefault(p["layer"], []).append(p)
    for layer in by_layer:
        by_layer[layer].sort(key=lambda x: -x["weight_pct"])

    groups = [
        {"layer": layer, "picks": ps, "total_weight": sum(p["weight_pct"] for p in ps)}
        for layer, ps in by_layer.items()
    ]
    # Sort: known layers in canonical order first, then anything else by total weight.
    groups.sort(key=lambda g: (
        _LAYER_ORDER.index(g["layer"]) if g["layer"] in _LAYER_ORDER else 99,
        -g["total_weight"],
    ))
    return groups


def _active_trading_date(universe, briefing) -> date:
    """The trading day the dashboard is currently showing data for.

    Anchored to the latest event the pipeline has emitted today:
      • universe.as_of_date (scout fires first, ~09:00 ET)
      • briefing.as_of_date (morning briefing, ~09:08 ET)

    Falls back to the ET wall-clock date when nothing has been emitted yet.
    This stops the header from drifting ahead of the data during the
    overnight window (after midnight ET, before next scout run)."""
    candidates: list[str] = []
    if universe and universe is not False and universe.get("as_of_date"):
        candidates.append(str(universe["as_of_date"]))
    if briefing and isinstance(briefing, dict) and briefing.get("as_of_date"):
        candidates.append(str(briefing["as_of_date"]))
    if candidates:
        try:
            return max(date.fromisoformat(s) for s in candidates)
        except ValueError:
            pass
    return _now_market().date()


def _today_header(active: Optional[date] = None) -> dict:
    """Date pieces for the iOS Today View header + Calendar widget.
    `active` is the active trading day (see _active_trading_date)."""
    today = active or _now_market().date()
    return {
        "iso":     today.isoformat(),
        "weekday": today.strftime("%A"),
        "weekday_short": today.strftime("%a").upper(),
        "month":   today.strftime("%B"),
        "month_short": today.strftime("%b").upper(),
        "day":     today.day,
    }


def _parse_view_date(s: Optional[str]) -> Optional[str]:
    """A valid *past* trading date from ?d=YYYY-MM-DD, else None (live/today view).

    Future or today resolves to None so the dashboard shows the live view; only a
    strictly-past date switches to the historical render.
    """
    if not s:
        return None
    try:
        d = date.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d.isoformat() if d < _now_market().date() else None


def _next_update_label(now_dt: datetime, *, what: str) -> str:
    """Human-readable 'next update at HH:MM ET' string for a pending card."""
    targets = {
        "scout":    (9, 0,  "Scout · pick universe"),
        "briefing": (9, 10, "Briefing · plan ready"),
        "session":  (9, 30, "Open · execute plan"),
        "nav":      (16, 0, "Close · mark book"),
    }
    if what not in targets:
        return "Next update pending"
    h, m, label = targets[what]
    if (now_dt.hour, now_dt.minute) < (h, m):
        return f"Next update at {h:02d}:{m:02d} ET · {label}"
    return f"{label} — pending"


def _card_freshness(*, active_date: date, universe, briefing, nav, session_hdr) -> dict:
    """Per-card flag: is the card's data fresh for `active_date`?

    When fresh is False, the template shows a 'next update at HH:MM ET'
    placeholder instead of stale prior-day data.
    """
    iso = active_date.isoformat()
    now_dt = _now_market()
    def _matches(d) -> bool:
        return bool(d and str(d) == iso)

    return {
        "universe": {
            "fresh":  _matches(universe.get("as_of_date") if universe and universe is not False else None),
            "note":   _next_update_label(now_dt, what="scout"),
        },
        "briefing": {
            "fresh":  _matches(briefing.get("as_of_date") if briefing and isinstance(briefing, dict) else None),
            "note":   _next_update_label(now_dt, what="briefing"),
        },
        "nav": {
            "fresh":  _matches(nav.get("as_of_date") if nav else None),
            "note":   _next_update_label(now_dt, what="nav"),
        },
        "plan": {
            "fresh":  _matches(session_hdr.get("briefing_date") if session_hdr and session_hdr is not False else None),
            "note":   _next_update_label(now_dt, what="session"),
        },
    }


def _nav_chart(history, *, w: int = 300, h: int = 64, pad: int = 6) -> Optional[dict]:
    """Build an inline-SVG NAV equity curve from fetch_nav_history (newest-first).
    A single mark renders as a dashed flat baseline — the curve fills in as
    daily NAV marks accrue. Returns geometry + the change over the window."""
    if not history:
        return None
    pts = list(reversed(history))  # chronological
    ys = [float(p["ending_nav"]) for p in pts]
    n = len(ys)
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0

    def _x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5)

    def _y(v: float) -> float:
        return h - pad - (h - 2 * pad) * ((v - lo) / span)

    coords = [(_x(i), _y(v)) for i, v in enumerate(ys)]
    if n == 1:
        mid = h / 2
        line = f"M{pad},{mid:.1f} L{w - pad},{mid:.1f}"
        area = None
        dot = (w / 2, mid)
    else:
        line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        area = (f"M{coords[0][0]:.1f},{h - pad} L"
                + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
                + f" L{coords[-1][0]:.1f},{h - pad} Z")
        dot = coords[-1]

    first, last = ys[0], ys[-1]
    change = last - first
    return {
        "w": w, "h": h, "line": line, "area": area, "dot": dot, "n": n,
        "first": first, "last": last, "change": change,
        "change_pct": (change / first * 100) if first else 0.0,
        "up": change >= 0,
    }


@app.route("/")
@_login_required
def index():
    # Optional ?d=YYYY-MM-DD renders a past trading day; otherwise the live view.
    as_of = _parse_view_date(request.args.get("d"))
    universe = _db.fetch_current_universe(as_of=as_of)
    briefing = None if as_of else _db.fetch_current_briefing()
    nav      = _db.fetch_current_nav(as_of=as_of)
    session_hdr = None if as_of else _db.fetch_today_session()

    items = None
    realised = None
    if session_hdr and session_hdr is not False and session_hdr.get("plan_id"):
        items = _db.fetch_session_items(str(session_hdr["plan_id"]))
        pnl   = _db.session_pnl_summary(str(session_hdr["plan_id"]))
        realised = pnl["realised_pnl"] if pnl else None

    # Post-consolidation: the legacy briefing/plan tables are gone. Synthesize
    # a briefing + plan from the scout's curated universe so Posture (the day's
    # thesis) and Plan (today's intended holdings) reflect real scout output
    # instead of sitting blank.
    synthetic_plan = False
    if briefing is None and universe and universe is not False:
        synthetic_plan = True
        picks = universe.get("picks", []) or []
        # Derive posture from the scout's conviction: a concentrated top weight
        # signals an aggressive tilt; an even spread is neutral.
        _max_w = max((p.get("weight_pct") or 0) for p in picks) if picks else 0
        briefing = {
            "as_of_date": universe.get("as_of_date"),
            "plan": {
                "posture": "aggressive" if _max_w >= 16 else "neutral",
                "session_thesis": universe.get("rationale")
                    or "Curated AI-stack universe for today.",
                "regime_summary": "",
                "risks_today": [],
                "playbooks": [],
            },
        }
        # Plan = today's execution checklist: each universe name marked filled
        # (and held) or pending, from real fills. Execution reflects *current*
        # positions, so it's only meaningful for the live view — skip it when
        # viewing a past date (the "Today's book" panel carries that day's record).
        if not as_of:
            execu = _db.execution_by_ticker() or {}
            if not items:
                items = []
                for p in picks:
                    t = p.get("ticker")
                    ex = execu.get(t) or {}
                    filled = bool(ex.get("fills"))
                    items.append({
                        "ticker": t, "side": "long",
                        "layer": p.get("layer"), "weight_pct": p.get("weight_pct"),
                        "rationale": p.get("rationale"),
                        "status": "filled" if filled else "pending",
                        "fill_price": ex.get("entry_price"),
                        "held": abs(ex.get("net") or 0) > 0.0001,
                    })
        if not session_hdr or session_hdr is False:
            session_hdr = {"briefing_date": universe.get("as_of_date"), "plan_id": None}

    # Carry book — positions left over from a prior, already-settled plan.
    # Distinct from today's items: separate card, separate semantics.
    carry = [] if as_of else (_db.fetch_carrying_positions() or [])

    universe_groups = _group_picks_by_layer(universe.get("picks", [])) if universe else []
    book = _db.fetch_book(as_of=as_of) or []
    book_groups = _group_picks_by_layer(book) if book else []

    # Pair each plan item with its playbook so the per-ticker drilldown
    # sheet can surface catalyst / technicals / action rationale.
    playbooks_by_ticker = {}
    if briefing and isinstance(briefing, dict):
        for pb in (briefing.get("plan", {}).get("playbooks") or []):
            playbooks_by_ticker[pb["ticker"]] = pb

    db_ok = not (universe is None and briefing is None and nav is None)

    active_date = date.fromisoformat(as_of) if as_of else _active_trading_date(universe, briefing)
    freshness   = _card_freshness(
        active_date=active_date, universe=universe, briefing=briefing,
        nav=nav, session_hdr=session_hdr,
    )

    def _iso(d) -> Optional[str]:
        return str(d) if d else None
    last_updated = {
        "universe":  _iso(universe.get("as_of_date")) if universe and universe is not False else None,
        "briefing":  _iso(briefing.get("as_of_date")) if briefing and isinstance(briefing, dict) else None,
        "nav":       _iso(nav.get("as_of_date")) if nav else None,
        "session":   _iso(session_hdr.get("briefing_date")) if session_hdr and session_hdr is not False else None,
    }

    return render_template(
        "dashboard.html",
        active="dashboard",
        db_ok=db_ok,
        universe=universe,
        universe_groups=universe_groups,
        book=book,
        book_groups=book_groups,
        briefing=briefing,
        nav=nav,
        session_hdr=session_hdr,
        items=items,
        realised=realised,
        playbooks=playbooks_by_ticker,
        today=_today_header(active_date),
        freshness=freshness,
        last_updated=last_updated,
        carry=carry,
        planned=synthetic_plan,
        held=set() if as_of else _db.held_tickers(),
        confidence=_db.decision_confidence(),
        nav_chart=_nav_chart(_db.fetch_nav_history(60, as_of=as_of)),
        run=_db.today_run(as_of=as_of),
        viewing_date=as_of,
        today_iso=_now_market().date().isoformat(),
    )


@app.route("/universe")
@_login_required
def universe_page():
    u = _db.fetch_current_universe()
    yesterday = None
    groups = []
    if u and u is not False:
        yesterday = _db.fetch_previous_universe(u["as_of_date"])
        groups = _group_picks_by_layer(u.get("picks", []))
    return render_template(
        "universe.html",
        active="universe",
        universe=u,
        yesterday=yesterday,
        groups=groups,
        held=_db.held_tickers(),
    )


@app.route("/positions")
@_login_required
def positions_page():
    session_hdr = _db.fetch_today_session()
    items = None
    realised = None
    if session_hdr and session_hdr is not False and session_hdr.get("plan_id"):
        items = _db.fetch_session_items(str(session_hdr["plan_id"]))
        pnl = _db.session_pnl_summary(str(session_hdr["plan_id"]))
        realised = pnl["realised_pnl"] if pnl else None

    # Pair each plan item with its playbook (if the morning has been built).
    # The playbook carries the catalyst / technicals / recommended_action that
    # explain *why* this item was on the plan in the first place.
    briefing = _db.fetch_current_briefing()
    playbooks_by_ticker = {}
    if briefing and isinstance(briefing, dict):
        for pb in (briefing.get("plan", {}).get("playbooks") or []):
            playbooks_by_ticker[pb["ticker"]] = pb

    return render_template(
        "positions.html",
        active="positions",
        session_hdr=session_hdr,
        items=items or [],
        realised=realised,
        playbooks=playbooks_by_ticker,
        briefing=briefing,
    )


# /morning kept as a redirect so any bookmark or tab survives the merge.
@app.route("/morning")
@_login_required
def morning_redirect():
    return redirect(url_for("positions_page"))


@app.route("/debriefs")
@_login_required
def debriefs_page():
    decisions = _db.list_decisions(limit=60) or []
    return render_template(
        "debriefs.html",
        active="debriefs",
        decisions=decisions,
    )


@app.route("/debrief/<debrief_id>")
@_login_required
def debrief_detail(debrief_id: str):
    d = _db.fetch_decision(debrief_id)
    if d in (None, False):
        abort(404)
    # Back returns to wherever you came from — the landing page's "Today's book"
    # or the journal list — instead of always the journal. Default to the journal
    # for a direct visit / refresh (no referrer).
    if urlparse(request.referrer or "").path == "/":
        back_url, back_label = "/", "Today"
    else:
        back_url, back_label = "/debriefs", "Debrief"
    return render_template(
        "debrief_detail.html",
        active="debriefs",
        d=d,
        back_url=back_url,
        back_label=back_label,
    )


@app.route("/nav")
@_login_required
def nav_page():
    history = _db.fetch_nav_history(limit=60) or []
    current = _db.fetch_current_nav()
    return render_template(
        "nav.html",
        active="nav",
        history=history,
        current=current,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
