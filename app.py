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
from concurrent.futures import ThreadPoolExecutor
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


# ── Parallel fan-out ─────────────────────────────────────────────────────────

def _gather(tasks: dict) -> dict:
    """Run independent DB reads concurrently; return {key: result}.

    Each db.py helper opens its own short-lived connection, so the cost of a
    page is dominated by connection setup (TLS + SET search_path, ~hundreds of
    ms each on Neon) rather than the SQL. Running the reads on threads lets the
    connections overlap: wall time collapses to the slowest single read instead
    of the sum. The helpers swallow their own errors (return None/{}/[]), so a
    future never raises here. This is orchestration only — no data is cached.
    """
    if not tasks:
        return {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {key: ex.submit(fn) for key, fn in tasks.items()}
        return {key: fut.result() for key, fut in futures.items()}


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


def _synth_briefing(universe) -> Optional[dict]:
    """Synthesize the briefing/posture from the scout's curated universe.

    The legacy fund_briefings/pm_plans tables were consolidated away, so Posture
    (the day's thesis) is derived from the universe: a concentrated top weight
    signals an aggressive tilt, an even spread is neutral. Returns None when there
    is no universe to derive from.
    """
    if not universe or universe is False:
        return None
    picks = universe.get("picks", []) or []
    _max_w = max((p.get("weight_pct") or 0) for p in picks) if picks else 0
    return {
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


@app.route("/")
@_login_required
def index():
    # The dashboard is rendered progressively: this route returns the shell
    # (header + skeletons) with no DB work, so first paint is instant. The
    # heavy panels are fetched as fragments (/_dash/*) and stream in client-side.
    as_of = _parse_view_date(request.args.get("d"))
    active = date.fromisoformat(as_of) if as_of else _now_market().date()
    return render_template(
        "dashboard.html",
        active="dashboard",
        today=_today_header(active),
        viewing_date=as_of,
        today_iso=_now_market().date().isoformat(),
    )


@app.route("/_dash/track")
@_login_required
def dash_track():
    """Fragment: track record — NAV equity curve + decision quality."""
    as_of = _parse_view_date(request.args.get("d"))
    r = _gather({
        "nav":        lambda: _db.fetch_current_nav(as_of=as_of),
        "nav_hist":   lambda: _db.fetch_nav_history(60, as_of=as_of),
        "confidence": _db.decision_confidence,
    })
    nav = r["nav"]
    return render_template(
        "_dash_track.html",
        nav=nav,
        nav_chart=_nav_chart(r["nav_hist"]),
        confidence=r["confidence"],
        last_updated={"nav": str(nav["as_of_date"]) if nav else None},
    )


@app.route("/_dash/row1")
@_login_required
def dash_row1():
    """Fragment: posture widget + macro calendar (and their detail sheets)."""
    as_of = _parse_view_date(request.args.get("d"))
    today_iso = _now_market().date().isoformat()
    r = _gather({
        "universe": lambda: _db.fetch_current_universe(as_of=as_of),
        "macro":    lambda: _db.macro_events(as_of or today_iso),
        "spx":      lambda: _db.spx_move(as_of or today_iso),
    })
    universe = r["universe"]
    briefing = _synth_briefing(universe)
    active_date = date.fromisoformat(as_of) if as_of else _active_trading_date(universe, briefing)
    freshness = _card_freshness(
        active_date=active_date, universe=universe, briefing=briefing,
        nav=None, session_hdr=None,
    )
    return render_template(
        "_dash_row1.html",
        briefing=briefing,
        macro=r["macro"],
        spx=r["spx"],
        freshness=freshness,
        viewing_date=as_of,
        today=_today_header(active_date),
        last_updated={"briefing": str(briefing["as_of_date"]) if briefing else None},
    )


@app.route("/_dash/main")
@_login_required
def dash_main():
    """Fragment: today's book + risk exits + the fund's EOD narrative."""
    as_of = _parse_view_date(request.args.get("d"))
    live = not as_of
    today_iso = _now_market().date().isoformat()
    tasks = {
        "universe":  lambda: _db.fetch_current_universe(as_of=as_of),
        "book":      lambda: _db.fetch_book(as_of=as_of),
        "narrative": lambda: _db.fetch_day_narrative(as_of or today_iso),
    }
    if live:
        tasks["held"]     = _db.held_sides
        tasks["exposure"] = _db.book_exposure
    r = _gather(tasks)

    universe = r["universe"]
    book = r["book"] or []
    briefing = _synth_briefing(universe)
    active_date = date.fromisoformat(as_of) if as_of else _active_trading_date(universe, briefing)
    return render_template(
        "_dash_main.html",
        # universe is None only on a DB error (False = simply no universe yet).
        db_ok=universe is not None,
        universe=universe,
        briefing=briefing,
        book_groups=_group_picks_by_layer(book) if book else [],
        book_count=len(book),
        exposure=r.get("exposure"),
        held=r.get("held") or {},
        risk_exits=_db.risk_exits(active_date.isoformat()),
        narrative=r["narrative"],
    )


@app.route("/universe")
@_login_required
def universe_page():
    data = _gather({"u": _db.fetch_current_universe, "held": _db.held_tickers})
    u = data["u"]
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
        held=data["held"],
    )


@app.route("/positions")
@_login_required
def positions_page():
    # The live book from the consolidated schema: every currently-held name
    # (held_sides applies the fund's dust filter), enriched with its entry/size
    # from fills and its layer/weight/score from today's universe pick.
    data = _gather({
        "sides":    _db.held_sides,
        "execu":    _db.execution_by_ticker,
        "marked":   _db.holdings_marked,
        "book":     _db.fetch_book,
        "exposure": _db.book_exposure,
        "nav":      _db.fetch_current_nav,
    })
    sides = data["sides"] or {}
    execu = data["execu"] or {}
    marked = data["marked"] or {}
    book_by_t = {row["ticker"]: row for row in (data["book"] or [])}
    holdings = []
    for ticker, side in sides.items():
        ex = execu.get(ticker) or {}
        b = book_by_t.get(ticker) or {}
        m = marked.get(ticker) or {}
        # Prefer the snapshot's average-cost basis (the OMS entry) over the
        # max-buy-fill proxy; fall back to fills when the view has no row yet.
        entry = m.get("avg_cost") if m.get("avg_cost") is not None else ex.get("entry_price")
        qty = abs(m["quantity"]) if m.get("quantity") is not None else abs(ex.get("net") or 0.0)
        mkt_val = m.get("market_value")
        holdings.append({
            "ticker": ticker, "side": side,
            "layer": b.get("layer"), "weight_pct": b.get("weight_pct"),
            "entry_price": entry, "qty": qty,
            # Live marks from current_holdings (None until the first marked snapshot).
            "market_price": m.get("market_price"),
            "unrealized_pnl": m.get("unrealized_pnl"),
            "unrealized_pnl_pct": m.get("unrealized_pnl_pct"),
            "nav_weight_pct": m.get("nav_weight_pct"),
            # Market value when marked, else the entry-cost proxy.
            "notional": abs(mkt_val) if mkt_val is not None
                        else ((qty * entry) if (entry and qty) else None),
            "score": b.get("score"), "outcome": b.get("pipeline_outcome"),
            "decision_id": b.get("decision_id"),
        })
    holdings.sort(key=lambda h: h["notional"] or 0, reverse=True)
    return render_template(
        "positions.html",
        active="positions",
        holdings=holdings,
        exposure=data["exposure"],
        nav=data["nav"],
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


@app.route("/rotation")
@_login_required
def rotation_page():
    """Momentum thematic rotation cockpit (read-only; written by the thematic runner)."""
    r = _gather({
        "series":  lambda: _db.rotation_nav_series(800),
        "latest":  _db.rotation_latest,
        "sleeves": _db.rotation_sleeves,
        "rebals":  lambda: _db.rotation_rebalances(12),
    })
    series = r["series"] or []
    chart = _nav_chart(
        list(reversed([{"ending_nav": p["nav"]} for p in series]))
    ) if series else None
    # Strategy vs benchmark total return over the shown window.
    perf = None
    if series:
        s0, s1 = series[0], series[-1]
        strat = (float(s1["nav"]) / float(s0["nav"]) - 1) * 100 if s0["nav"] else None
        b0, b1 = s0.get("benchmark_nav"), s1.get("benchmark_nav")
        bench = (float(b1) / float(b0) - 1) * 100 if (b0 and b1) else None
        perf = {"strat": strat, "bench": bench}
    # Group the rebalance log by date for the journal.
    rebals: list[dict] = []
    for row in (r["rebals"] or []):
        if not rebals or rebals[-1]["date"] != row["rebalance_date"]:
            rebals.append({"date": row["rebalance_date"], "rows": []})
        rebals[-1]["rows"].append(row)
    return render_template(
        "rotation.html",
        active="rotation",
        latest=r["latest"],
        sleeves=r["sleeves"] or [],
        chart=chart,
        perf=perf,
        rebals=rebals,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
