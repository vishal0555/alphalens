"""
app.py — AlphaLens Flask app.

Read-only viewer: reads briefings and signals from Postgres and renders views.
No run controls, no config, no file system access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, redirect, render_template, url_for

# Load DB connection env — ~/.alphalens/dbconnector.env takes precedence
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


# ── Dot-accessible namespace ───────────────────────────────────────────────────

class _NS:
    """
    Recursively converts a JSON dict to a dot-accessible object so Jinja2
    templates can use obj.attr syntax on nested structures.

    The 'tickers' key is kept as a plain dict of _NS objects so that
    .items() works in templates (briefing.tickers.items()).
    """

    def __init__(self, d: dict):
        for k, v in d.items():
            if k == "tickers" and isinstance(v, dict):
                setattr(self, k, {tk: _NS(tv) if isinstance(tv, dict) else tv for tk, tv in v.items()})
            elif isinstance(v, dict):
                setattr(self, k, _NS(v))
            elif isinstance(v, list):
                setattr(self, k, [_NS(i) if isinstance(i, dict) else i for i in v])
            else:
                setattr(self, k, v)

    def __getattr__(self, name: str):
        return None  # return None for any missing attribute (safe for template conditionals)


# ── Signal context builder ─────────────────────────────────────────────────────

def _signal_color(label: str) -> str:
    return {"BUY": "#10b981", "HOLD": "#f59e0b", "NO SIGNAL": "#ef4444"}.get(label, "#94a3b8")


def _layer_summary(components: list[dict]) -> str:
    available = [c for c in components if c.get("score") is not None]
    if not available:
        return "All components failed to fetch"
    dominant = max(available, key=lambda c: c["score"])
    expl = dominant.get("explanation", "")
    return expl[:80] + ("…" if len(expl) > 80 else "")


def _generate_decision_summary(label: str, score: float, layers: list[dict], vetos: list[dict]) -> str:
    """Plain-English signal summary when AI enrichment is unavailable."""
    if label == "BUY":
        intro = (
            f"The signal is <strong>bullish</strong> with a composite score of {score:.0f}/90, "
            "indicating favorable conditions for new or incremental long positions."
        )
    elif label == "HOLD":
        intro = (
            f"The signal is <strong>neutral</strong> with a composite score of {score:.0f}/90 — "
            "current positioning can be maintained but new entries carry elevated risk."
        )
    else:
        intro = (
            f"The signal is <strong>negative</strong> with a composite score of {score:.0f}/90, "
            "suggesting risk/reward does not favor new long exposure at this time."
        )

    scored = [(l["name"], l["score"]) for l in layers if not l.get("is_veto") and l.get("score") is not None]
    if scored:
        strongest = max(scored, key=lambda x: x[1])
        weakest   = min(scored, key=lambda x: x[1])
        detail = (
            f" The strongest contributor is <strong>{strongest[0]}</strong> ({strongest[1]:.0f}/100), "
            f"while <strong>{weakest[0]}</strong> is the primary headwind ({weakest[1]:.0f}/100)."
        )
    else:
        detail = ""

    veto_note = ""
    if vetos:
        names = ", ".join(v["name"] for v in vetos[:2])
        veto_note = (
            f" Active veto(s) — <strong>{names}</strong> — have capped the score at {score:.0f}."
        )

    return intro + detail + veto_note


def _build_signal_context(payload: dict) -> dict:
    """
    Map the signal DB payload (from SignalResult.to_dict()) to the render
    context expected by signal.html.

    Payload structure (to_dict output):
      { timestamp, ticker,
        signal: { label, base_score, final_score, etf_price, etf_price_change_1d_pct },
        vetos:  { active: [{name, reason, cap}], dont_chase: {...} },
        layers: { fundamental: {...}, price_action: {...}, macro: {...} },
        data_quality_warnings: [...],
        ops: {...},
        ai_enrichment: {...} | null
      }
    """
    sig     = payload.get("signal", {})
    vetos_d = payload.get("vetos", {})
    layers_d = payload.get("layers", {})
    ai      = payload.get("ai_enrichment") or {}

    label       = sig.get("label", "?")
    final_score = float(sig.get("final_score", 0))
    base_score  = float(sig.get("base_score", 0))
    ticker      = payload.get("ticker", "?")
    etf_price   = sig.get("etf_price")
    etf_change  = sig.get("etf_price_change_1d_pct")
    color       = _signal_color(label)

    ts_raw = payload.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_raw)
        timestamp_display = ts.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        timestamp_display = ts_raw[:16] if ts_raw else "—"

    active_vetos = [
        {"name": v.get("name", "?"), "reason": v.get("reason", "")}
        for v in vetos_d.get("active", [])
    ]

    layers_ctx = []
    for key, display in [
        ("fundamental",  None),
        ("price_action", None),
        ("macro",        None),
    ]:
        lr = layers_d.get(key, {})
        if not lr:
            continue
        comps = [
            {
                "name":        c.get("name", "?"),
                "score":       c.get("score"),
                "explanation": c.get("explanation", ""),
                "warning":     c.get("warning"),
            }
            for c in lr.get("components", [])
        ]
        layers_ctx.append({
            "name":       lr.get("name", display or key),
            "score":      lr.get("score", 0),
            "weight":     lr.get("weight", 0),
            "summary":    _layer_summary(comps),
            "is_veto":    False,
            "components": comps,
        })

    # Don't-chase veto layer
    dc = vetos_d.get("dont_chase", {})
    triggered = dc.get("ma200_triggered") or dc.get("rsi_triggered")
    desc_parts = []
    if dc.get("ma200_triggered") and dc.get("ma200_detail"):
        desc_parts.append(dc["ma200_detail"])
    if dc.get("rsi_triggered") and dc.get("rsi_detail"):
        desc_parts.append(dc["rsi_detail"])
    layers_ctx.append({
        "name":       "Don't Chase",
        "score":      0 if triggered else 100,
        "weight":     0.0,
        "summary":    "; ".join(desc_parts) if desc_parts else "No overextension detected",
        "is_veto":    True,
        "components": [],
    })

    decision_summary = ai.get("pm_brief") or _generate_decision_summary(
        label, final_score, layers_ctx, active_vetos
    )
    ai_enriched = bool(ai.get("pm_brief"))

    signal_data = {
        "layers": [
            {"name": lc["name"], "score": lc["score"], "components": lc["components"]}
            for lc in layers_ctx if not lc["is_veto"]
        ],
        "sparklines": [],
    }

    warnings = payload.get("data_quality_warnings", [])
    ops = payload.get("ops", {})

    return {
        "timestamp_display": timestamp_display,
        "label":             label,
        "final_score":       final_score,
        "base_score":        base_score,
        "signal_color":      color,
        "ticker":            ticker,
        "etf_price":         etf_price,
        "etf_change_pct":    etf_change,
        "active_vetos":      active_vetos,
        "layers":            layers_ctx,
        "holdings_rows":     [],
        "macro_tiles":       [],
        "ai_stack_layers":   [],
        "decision_summary":  decision_summary,
        "ai_enriched":       ai_enriched,
        "risks":             ai.get("risks", []),
        "catalysts":         ai.get("catalysts", []),
        "signal_data_json":  json.dumps(signal_data),
        "data_fresh":        len(warnings) == 0,
        "freshness_label":   "Fresh" if not warnings else f"{len(warnings)} warning(s)",
        "data_warnings":     warnings[:5],
        "ops": {
            "input_tokens":  ops.get("input_tokens", 0),
            "output_tokens": ops.get("output_tokens", 0),
            "total_tokens":  ops.get("total_tokens", 0),
            "model":         ops.get("model"),
            "ai_enriched":   ai_enriched,
        },
        "sections": {
            "layers":      True,
            "drivers":     True,
            "holdings":    False,
            "macro":       False,
            "ai_stack":    False,
            "pm_brief":    True,
            "methodology": True,
            "ops":         True,
        },
        "home_url": "/",
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    briefings = _db.list_briefings()
    signals   = _db.list_signals(limit=10)
    db_ok     = briefings is not None
    return render_template(
        "index.html",
        briefings=briefings or [],
        signals=signals or [],
        db_ok=db_ok,
    )


@app.route("/briefing/latest")
def briefing_latest():
    bid = _db.get_latest_briefing_id()
    if not bid:
        abort(404)
    return redirect(url_for("briefing_view", row_id=bid))


@app.route("/briefing/<int:row_id>")
def briefing_view(row_id: int):
    payload = _db.get_briefing(row_id)
    if payload is None:
        return render_template("index.html", briefings=[], signals=[], db_ok=False), 503
    if payload is False:
        abort(404)

    briefing = _NS(payload)
    sections = {
        "market":    True,
        "catalysts": True,
        "tickers":   True,
        "signals":   True,
        "game_plan": True,
        "earnings":  True,
        "ai_stack":  True,
        "narrative": True,
        "ops":       True,
    }
    ai_stack_layers = [
        _NS(l) if isinstance(l, dict) else l
        for l in (payload.get("ai_stack_layers") or [])
    ]
    narrative_paras = [
        _NS(p) if isinstance(p, dict) else p
        for p in (payload.get("narrative_paras") or [])
    ]
    date_str = (briefing.meta.date or "") if briefing.meta else ""

    return render_template(
        "briefing.html",
        briefing=briefing,
        date_str=date_str,
        sections=sections,
        ai_stack_layers=ai_stack_layers,
        narrative_paras=narrative_paras,
    )


@app.route("/signal/latest")
def signal_latest():
    ticker = None  # latest across all tickers
    sid = _db.get_latest_signal_id(ticker)
    if not sid:
        abort(404)
    return redirect(url_for("signal_view", row_id=sid))


@app.route("/signal/<int:row_id>")
def signal_view(row_id: int):
    payload = _db.get_signal(row_id)
    if payload is None:
        return render_template("index.html", briefings=[], signals=[], db_ok=False), 503
    if payload is False:
        abort(404)

    ctx = _build_signal_context(payload)
    return render_template("signal.html", **ctx)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
