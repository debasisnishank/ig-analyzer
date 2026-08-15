#!/usr/bin/env python3
"""Optional Flask dashboard for browsing analyses in a browser.

Run:
    pip install flask markdown
    python dashboard.py
    # visit http://127.0.0.1:5000

Security: binds to 127.0.0.1 by default. If you expose it on a VPS, put it behind
a reverse proxy with auth and/or restrict the port via firewall — the reports may
contain private account data.
"""

from __future__ import annotations

import json
import os

from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, render_template, request

import chat
import store
from api import api as api_blueprint

try:
    import markdown as md_lib
except ImportError:  # pragma: no cover
    md_lib = None

RAW_DIR = store.RAW_DIR

HANDLE = os.getenv("IG_HANDLE", "your_handle")

app = Flask(__name__)
app.register_blueprint(api_blueprint)


# --------------------------------------------------------------------------- #
# Chart geometry (inline SVG — no chart library)
# --------------------------------------------------------------------------- #

# Mobile-first viewBox. Marks only — every label is HTML positioned over the SVG,
# so text stays crisp and consistently sized however wide the phone or window is.
CHART_W, CHART_H = 360, 150
PAD_L, PAD_R, PAD_T, PAD_B = 6, 6, 10, 8


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round axis ticks to clean numbers spanning the data."""
    if high == low:
        return [low]
    raw = (high - low) / count
    magnitude = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 0.1
    step = max(round(raw / magnitude) * magnitude, magnitude)
    start = (low // step) * step
    ticks, value = [], start
    while value <= high + step * 0.5:
        ticks.append(round(value, 4))
        value += step
    return ticks


def _line_chart(
    rows: list[dict[str, Any]], key: str, title: str, precision: int = 0
) -> dict[str, Any]:
    """Build one single-series line chart. One series → no legend; the title names it."""
    pts = [(r["at"], r[key]) for r in rows if isinstance(r.get(key), (int, float))]
    chart: dict[str, Any] = {"title": title, "empty": len(pts) < 2, "runs": len(pts)}
    if len(pts) < 2:
        return chart

    values = [v for _, v in pts]
    low, high = min(values), max(values)
    if high == low:
        # Flat series: pad so the line sits mid-plot, and label the one real
        # value rather than repeating it across invented gridlines.
        flat = low
        low, high = low - 1, high + 1
        ticks = [flat]
    else:
        ticks = _nice_ticks(low, high)
        low, high = min(low, ticks[0]), max(high, ticks[-1])
    span = (high - low) or 1

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B
    step = plot_w / (len(pts) - 1)

    def x_of(i: int) -> float:
        return round(PAD_L + i * step, 2)

    def y_of(v: float) -> float:
        return round(PAD_T + plot_h - (v - low) / span * plot_h, 2)

    coords = [(x_of(i), y_of(v)) for i, (_, v) in enumerate(pts)]
    chart["path"] = "M" + " L".join(f"{x},{y}" for x, y in coords)
    chart["area"] = (
        f"M{coords[0][0]},{PAD_T + plot_h} L"
        + " L".join(f"{x},{y}" for x, y in coords)
        + f" L{coords[-1][0]},{PAD_T + plot_h} Z"
    )
    # Positions are percentages so the HTML label layer tracks the SVG at any width.
    seen_labels, tick_marks = set(), []
    for t in ticks:
        if not (low <= t <= high):
            continue
        label = f"{t:,.{precision}f}"
        if label in seen_labels:
            continue
        seen_labels.add(label)
        tick_marks.append(
            {
                "y": y_of(t),
                "pct": round((y_of(t) - PAD_T) / plot_h * 100, 3),
                "label": label,
            }
        )
    chart["ticks"] = tick_marks
    # Label the ends only — a date under every point is unreadable on a phone.
    chart["xlabels"] = [pts[0][0][5:], pts[-1][0][5:]]
    chart["last"] = {
        "x": coords[-1][0],
        "y": coords[-1][1],
        "label": f"{values[-1]:,.{precision}f}",
    }
    chart["points"] = [
        {
            "x": x,
            "y": y,
            "xpct": round((x - PAD_L) / plot_w * 100, 3),
            "ypct": round((y - PAD_T) / plot_h * 100, 3),
            "at": pts[i][0],
            "value": f"{pts[i][1]:,.{precision}f}",
        }
        for i, (x, y) in enumerate(coords)
    ]
    chart["baseline"] = PAD_T + plot_h
    chart["viewbox"] = f"0 0 {CHART_W} {CHART_H}"
    return chart


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #


def _sparkline(values: list[float], width: int = 64, height: int = 20) -> dict[str, Any]:
    """A 12-point sparkline: history in the de-emphasis hue, current point lit."""
    if len(values) < 2:
        return {}
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = width / (len(values) - 1)
    pts = [
        (round(i * step, 2), round(height - 3 - (v - low) / span * (height - 6), 2))
        for i, v in enumerate(values)
    ]
    return {
        "path": "M" + " L".join(f"{x},{y}" for x, y in pts),
        "last": {"x": pts[-1][0], "y": pts[-1][1]},
    }


def _view_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """store.stat_row() plus the bits only the HTML view needs."""
    tiles = []
    for s in store.stat_row(rows):
        tile = dict(s)
        history = s.get("history") or []
        tile["spark"] = _sparkline(history) if len(history) >= 3 else {}
        if s.get("range"):
            tile["range"] = dict(
                s["range"],
                low=s["range"]["low_display"],
                high=s["range"]["high_display"],
            )
        tiles.append(tile)
    return tiles


def _report_path(date: str, filename: str) -> Path:
    """Resolve a report for the HTML routes, 404-ing anything outside the tree."""
    run_id = store.run_id_for(date, filename)
    path = store.report_path(run_id) if run_id else None
    if path is None:
        abort(404)
    return path


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.route("/")
def home() -> str:
    runs = store.list_runs()
    rows = store.snapshots()
    latest_html = None
    latest_meta = None
    if runs:
        latest_meta = runs[0]
        latest_html = _render_md(_report_path(latest_meta["date"], latest_meta["filename"]).read_text())
    return render_template(
        "dashboard.html",
        handle=HANDLE,
        analyses=runs,
        stats=_view_stats(rows),
        latest_html=latest_html,
        latest_meta=latest_meta,
        last_run=store.last_run_at(rows),
    )


@app.route("/analysis/<date>/<filename>")
def analysis(date: str, filename: str) -> str:
    path = _report_path(date, filename)
    rows = store.snapshots()
    return render_template(
        "analysis.html",
        handle=HANDLE,
        analyses=store.list_runs(),
        html=_render_md(path.read_text()),
        date=date,
        filename=filename,
        last_run=store.last_run_at(rows),
    )


@app.route("/trends")
def trends() -> str:
    rows = store.snapshots()
    return render_template(
        "trends.html",
        handle=HANDLE,
        stats=_view_stats(rows),
        charts=[
            _line_chart(rows, "followers", "Followers"),
            _line_chart(rows, "engagement", "Engagement rate (%)", precision=2),
            _line_chart(rows, "reach", "Reach per post"),
        ],
        rows=rows[::-1],
        last_run=store.last_run_at(rows),
    )


@app.route("/ask")
def ask_page() -> str:
    used, limit = chat.usage_today()
    rows = store.snapshots()
    return render_template(
        "ask.html",
        handle=HANDLE,
        active="ask",
        runs=len(store.list_runs()),
        remaining=max(0, limit - used),
        daily_limit=limit,
        max_chars=chat.MAX_QUESTION_CHARS,
        last_run=store.last_run_at(rows),
    )


@app.post("/ask")
def ask_submit() -> tuple[Response, int] | Response:
    """Browser-facing counterpart to /api/v1/ask.

    No bearer token here: this route is only reachable through the same gate as
    the rest of the HTML, whereas the API surface has its own. Answers come back
    as rendered HTML so the page uses the identical markdown path as the reports
    — escaped first, so a model answer quoting a caption can't inject script.
    """
    payload = request.get_json(silent=True) or {}
    try:
        result = chat.ask(payload.get("question", ""))
    except chat.RateLimited as exc:
        return jsonify({"error": {"message": str(exc)}}), 429
    except chat.ChatError as exc:
        return jsonify({"error": {"message": str(exc)}}), 400
    result["answer_html"] = _render_md(result["answer"])
    return jsonify(result)


@app.route("/download/<date>/<filename>")
def download(date: str, filename: str) -> Response:
    """Download the raw JSON that matches an analysis timestamp."""
    _report_path(date, filename)  # validates the pair exists
    stamp = filename.replace("analysis_", "").replace(".md", "")  # HHMMSS
    date_compact = date.replace("-", "")
    matches = sorted(RAW_DIR.glob(f"data_{date_compact}_{stamp}.json"))
    if not matches:
        # Fall back to any raw file from that date.
        matches = sorted(RAW_DIR.glob(f"data_{date_compact}_*.json"))
    if not matches:
        abort(404)
    return Response(
        matches[-1].read_text(),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={matches[-1].name}"},
    )


def _render_md(text: str) -> str:
    """Render report markdown to HTML.

    Reports embed Instagram captions and model output, so HTML is escaped before
    the markdown pass — otherwise a caption containing a `<script>` tag would
    execute in this page. Escaping first is safe for markdown, whose syntax uses
    none of the escaped characters.
    """
    safe = _escape(text)
    if md_lib is None:
        return f"<pre>{safe}</pre>"
    html = md_lib.markdown(safe, extensions=["tables", "fenced_code"])
    # Report tables are wider than a phone. Give each one its own scroll
    # container so it slides sideways instead of being clipped by the card.
    return html.replace("<table>", '<div class="scroller"><table>').replace(
        "</table>", "</table></div>"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
