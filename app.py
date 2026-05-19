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


def _today_header() -> dict:
    """Date pieces for the iOS Today View header + Calendar widget."""
    today = date.today()
    return {
        "iso":     today.isoformat(),
        "weekday": today.strftime("%A"),
        "weekday_short": today.strftime("%a").upper(),
        "month":   today.strftime("%B"),
        "month_short": today.strftime("%b").upper(),
        "day":     today.day,
    }


def _calendar_events(session_hdr, items) -> list[dict]:
    """Today's session as a sequence of calendar events for the iOS-style
    Calendar widget. Each event = {time, title, state} where state is one of
    'done' | 'next' | 'later'."""
    now = datetime.now().time()
    open_t  = (9, 30)
    close_t = (16, 0)

    has_session = bool(session_hdr) and session_hdr is not False
    session_done = (
        has_session
        and session_hdr.get("status") in ("completed", "settled")
    )

    def _state_for(hour: int, minute: int) -> str:
        if (now.hour, now.minute) > (hour, minute):
            return "done"
        return "now"

    events = [
        {"time": "08:30", "title": "Morning brief",
         "state": "done" if has_session else _state_for(8, 30)},
        {"time": "09:30", "title": "Open · execute plan",
         "state": _state_for(*open_t) if not session_done else "done"},
        {"time": "16:00", "title": "Close · mark book",
         "state": _state_for(*close_t) if not session_done else "done"},
        {"time": "17:00", "title": "Debrief lessons",
         "state": "done" if session_done else "later"},
    ]
    # Mark the next-upcoming event as 'next'
    flipped_next = False
    for e in events:
        if e["state"] == "now":
            e["state"] = "next" if not flipped_next else "later"
            flipped_next = True
    return events


@app.route("/")
@_login_required
def index():
    universe = _db.fetch_current_universe()
    briefing = _db.fetch_current_briefing()
    nav      = _db.fetch_current_nav()
    session_hdr = _db.fetch_today_session()

    items = None
    realised = None
    if session_hdr and session_hdr is not False and session_hdr.get("plan_id"):
        items = _db.fetch_session_items(str(session_hdr["plan_id"]))
        pnl   = _db.session_pnl_summary(str(session_hdr["plan_id"]))
        realised = pnl["realised_pnl"] if pnl else None

    lessons  = _db.fetch_recent_lessons(limit=5)
    universe_groups = _group_picks_by_layer(universe.get("picks", [])) if universe else []

    db_ok = not (universe is None and briefing is None and nav is None)

    return render_template(
        "dashboard.html",
        active="dashboard",
        db_ok=db_ok,
        universe=universe,
        universe_groups=universe_groups,
        briefing=briefing,
        nav=nav,
        session_hdr=session_hdr,
        items=items,
        realised=realised,
        lessons=lessons,
        today=_today_header(),
        calendar_events=_calendar_events(session_hdr, items),
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
    debriefs = _db.list_recent_debriefs(limit=50) or []
    return render_template(
        "debriefs.html",
        active="debriefs",
        debriefs=debriefs,
    )


@app.route("/debrief/<debrief_id>")
@_login_required
def debrief_detail(debrief_id: str):
    d = _db.fetch_debrief(debrief_id)
    if d in (None, False):
        abort(404)
    return render_template(
        "debrief_detail.html",
        active="debriefs",
        debrief=d,
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
