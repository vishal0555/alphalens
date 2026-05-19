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
from datetime import datetime
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

# Display order for AI-stack layers (most compute-heavy first → most peripheral).
_LAYER_ORDER = [
    "silicon", "hyperscaler", "model_lab", "infra",
    "power", "application", "device", "sovereign",
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


@app.route("/morning")
@_login_required
def morning_page():
    briefing = _db.fetch_current_briefing()
    return render_template(
        "morning.html",
        active="morning",
        briefing=briefing,
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
    return render_template(
        "positions.html",
        active="positions",
        session_hdr=session_hdr,
        items=items or [],
        realised=realised,
    )


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
