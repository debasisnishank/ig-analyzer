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
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, render_template

try:
    import markdown as md_lib
except ImportError:  # pragma: no cover
    md_lib = None

BASE_DIR = Path(__file__).resolve().parent
ANALYSES_DIR = BASE_DIR / "data" / "analyses"
RAW_DIR = BASE_DIR / "data" / "raw"

HANDLE = os.getenv("IG_HANDLE", "nsk.rides")

# analysis_HHMMSS.md, written by main.save_results()
ANALYSIS_RE = re.compile(r"^analysis_\d{6}\.md$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = Flask(__name__)


def _all_analyses() -> list[dict[str, str]]:
    if not ANALYSES_DIR.exists():
        return []
    items = []
    for f in sorted(ANALYSES_DIR.glob("*/analysis_*.md"), reverse=True):
        items.append(
            {
                "date": f.parent.name,
                "filename": f.name,
                "size": f"{f.stat().st_size:,}",
            }
        )
    return items


def _safe_analysis_path(date: str, filename: str) -> Path:
    """Resolve a report path, refusing anything that escapes the analyses dir.

    Both components are matched against the exact shapes `save_results` writes,
    so traversal, symlinks and sibling directories (`../analyses_backup`) are all
    rejected before touching the filesystem.
    """
    if not DATE_RE.match(date) or not ANALYSIS_RE.match(filename):
        abort(400)
    root = ANALYSES_DIR.resolve()
    path = (root / date / filename).resolve()
    if root not in path.parents or not path.is_file():
        abort(404)
    return path


@app.route("/")
def home() -> str:
    analyses = _all_analyses()
    latest_html = None
    latest_meta = None
    if analyses:
        latest_meta = analyses[0]
        path = _safe_analysis_path(latest_meta["date"], latest_meta["filename"])
        latest_html = _render_md(path.read_text())
    return render_template(
        "dashboard.html",
        handle=HANDLE,
        analyses=analyses,
        latest_html=latest_html,
        latest_meta=latest_meta,
    )


@app.route("/analysis/<date>/<filename>")
def analysis(date: str, filename: str) -> str:
    path = _safe_analysis_path(date, filename)
    return render_template(
        "analysis.html",
        html=_render_md(path.read_text()),
        date=date,
        filename=filename,
    )


@app.route("/download/<date>/<filename>")
def download(date: str, filename: str) -> Response:
    """Download the raw JSON that matches an analysis timestamp."""
    _safe_analysis_path(date, filename)  # validates the pair exists
    stamp = filename.replace("analysis_", "").replace(".md", "")  # HHMMSS
    date_compact = date.replace("-", "")
    matches = sorted(RAW_DIR.glob(f"data_{date_compact}_{stamp}.json"))
    if not matches:
        # Fall back to any raw file from that date.
        matches = sorted(RAW_DIR.glob(f"data_{date_compact}_*.json"))
    if not matches:
        abort(404)
    content = matches[-1].read_text()
    return Response(
        content,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={matches[-1].name}"},
    )


@app.route("/trends")
def trends() -> str:
    """Follower / engagement history assembled from the stored raw snapshots."""
    rows = []
    for path in sorted(RAW_DIR.glob("data_*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        overall = (data.get("derived") or {}).get("overall") or {}
        collected = (data.get("collected_at") or "")[:16].replace("T", " ")
        rows.append(
            {
                "collected_at": collected,
                "followers": (data.get("account") or {}).get("followers_count"),
                "posts": len(data.get("media") or []),
                "avg_engagement_rate": overall.get("avg_engagement_rate"),
                "avg_reach": overall.get("avg_reach"),
            }
        )
    return render_template("trends.html", handle=HANDLE, rows=rows[::-1])


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
    return md_lib.markdown(safe, extensions=["tables", "fenced_code"])


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
