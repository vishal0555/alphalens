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


def _calendar_events(session_hdr, items, *, active_date: date, universe, briefing,
                      debrief_cov: Optional[dict] = None,
                      nav_marked: bool = False) -> list[dict]:
    """Today's trading session as calendar events for the iOS-style
    Schedule widget. Each event = {time, title, state} where state is one
    of 'done' | 'next' | 'later'. Times are ET market hours.
    """
    now_dt = _now_market()
    today_et = now_dt.date()
    now_hm = (now_dt.hour, now_dt.minute)

    active_is_today = (active_date == today_et)
    active_is_past  = (active_date < today_et)

    have_universe = bool(universe and universe is not False
                         and str(universe.get("as_of_date") or "") == active_date.isoformat())
    have_briefing = bool(briefing and isinstance(briefing, dict)
                         and str(briefing.get("as_of_date") or "") == active_date.isoformat())
    session_status = (session_hdr or {}).get("status") if session_hdr and session_hdr is not False else None
    session_done   = session_status in ("completed", "settled")
    session_live   = session_status in ("pending", "active")
    # The session row must belong to the active trading day before we
    # can claim its status reflects "today's plan."
    session_today  = bool(session_hdr and session_hdr is not False
                          and str(session_hdr.get("briefing_date") or "") == active_date.isoformat())

    def _point(hm, *, done: bool) -> str:
        # Single-moment event (scout, briefing, debrief).
        if done:           return "done"
        if active_is_past: return "done"
        if now_hm < hm:    return "later"
        return "now"  # deadline lapsed, artefact still missing

    def _window(start_hm, end_hm, *, done: bool, live: bool, exists: bool = True) -> str:
        # Window event — start has happened, work is ongoing until end_hm.
        # Past end_hm we treat the event as 'done' on wall-clock alone IF the
        # underlying artefact exists for the active day. If `exists=False`
        # (e.g. no pm_plans row — session never spawned), the event remains
        # 'now' (overdue) past the window so the dashboard surfaces the gap
        # rather than reporting a phantom completion.
        if done:               return "done"
        if active_is_past:     return "done" if exists else "now"
        if now_hm < start_hm:  return "later"
        if now_hm >= end_hm:   return "done" if exists else "now"
        return "in_progress" if live else "now"

    events = [
        {"time": "09:00", "title": "Scout · pick universe",
         "state": _point((9, 0), done=have_universe)},
        {"time": "09:10", "title": "Briefing · plan ready",
         "state": _point((9, 10), done=have_briefing)},
        {"time": "09:30", "title": "Open · execute plan",
         "state": _window((9, 30), (16, 0),
                          done=session_done and session_today,
                          live=session_live and session_today,
                          exists=session_today)},
        # "Close · mark book" reads `done` only when today's session
        # actually wrote a fund_nav row. Unlike Open (a time-anchored
        # event — the trading day ends at 16:00 regardless), Close is
        # data-anchored: the NAV write happens when the ExitMonitor's
        # completion handler fires, which can lag past close (or never
        # happen this calendar day for overnight holds). Use the explicit
        # state machine — no wall-clock-equals-done fallback.
        {"time": "16:00", "title": "Close · mark book",
         "state": ("done"        if nav_marked
                   else "later"       if now_hm < (16, 0)
                   else "in_progress" if now_hm < (16, 30)
                   else "now")},
        # Debrief flips done as soon as every closed item on the active
        # day has a debrief row — independent of plan settlement. With
        # per-item debriefs (alphalab/exit_monitor.py), this happens
        # within seconds of each exit.
        #
        # All-carry days (every position holds overnight, closed=0) used
        # to sit at "now/overdue" forever because the old condition
        # required `closed > 0`. They now flip to "done" at 17:00 wall-
        # clock — there's nothing to autopsy and never will be for this
        # trading day, so claiming it overdue is misleading.
        {"time": "17:00", "title": "Debrief lessons",
         "state": _point((17, 0),
                         done=bool(debrief_cov)
                              and debrief_cov.get("undebriefed", 0) == 0
                              and (debrief_cov.get("closed", 0) > 0
                                   or now_hm >= (17, 0)))},
    ]
    # Promote the first 'later' to 'next' only when nothing is actively
    # happening (in_progress / overdue) — otherwise the focus is already
    # carried by the current event.
    has_focus = any(e["state"] in ("in_progress", "now") for e in events)
    if not has_focus:
        for e in events:
            if e["state"] == "later":
                e["state"] = "next"
                break
    return events


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
        # (and held) or pending, from real fills.
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
    carry = _db.fetch_carrying_positions() or []

    lessons  = _db.fetch_recent_lessons(limit=5)
    universe_groups = _group_picks_by_layer(universe.get("picks", [])) if universe else []

    # Pair each plan item with its playbook so the per-ticker drilldown
    # sheet can surface catalyst / technicals / action rationale.
    playbooks_by_ticker = {}
    if briefing and isinstance(briefing, dict):
        for pb in (briefing.get("plan", {}).get("playbooks") or []):
            playbooks_by_ticker[pb["ticker"]] = pb

    db_ok = not (universe is None and briefing is None and nav is None)

    active_date = _active_trading_date(universe, briefing)
    freshness   = _card_freshness(
        active_date=active_date, universe=universe, briefing=briefing,
        nav=nav, session_hdr=session_hdr,
    )
    debrief_cov = _db.debrief_coverage_for_date(active_date.isoformat())
    nav_marked  = bool(_db.nav_marked_for_date(active_date.isoformat()))

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
        briefing=briefing,
        nav=nav,
        session_hdr=session_hdr,
        items=items,
        realised=realised,
        lessons=lessons,
        playbooks=playbooks_by_ticker,
        today=_today_header(active_date),
        calendar_events=_calendar_events(
            session_hdr, items,
            active_date=active_date, universe=universe, briefing=briefing,
            debrief_cov=debrief_cov, nav_marked=nav_marked,
        ),
        freshness=freshness,
        last_updated=last_updated,
        carry=carry,
        planned=synthetic_plan,
        held=_db.held_tickers(),
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
