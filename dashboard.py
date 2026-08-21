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
import secrets
import time

from datetime import timedelta
from pathlib import Path
from typing import Any

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

import chat
import settings
import store
from api import api as api_blueprint

try:
    import markdown as md_lib
except ImportError:  # pragma: no cover
    md_lib = None

RAW_DIR = store.RAW_DIR
STATIC_DIR = Path(__file__).resolve().parent / "static"

HANDLE = os.getenv("IG_HANDLE", "your_handle")

app = Flask(__name__)
app.register_blueprint(api_blueprint)

# --------------------------------------------------------------------------- #
# Login (shared password) for the HTML views
# --------------------------------------------------------------------------- #
# The JSON API under /api/v1 has its own bearer-token gate and is left alone;
# this session login guards only the browser-facing HTML pages. The password is
# read LIVE through settings (config.json override, else DASHBOARD_PASSWORD env),
# so a change from the admin page takes effect without a restart. When no
# password is configured anywhere the gate is disabled (pages open) — set one
# before removing any proxy-level auth, or the pages become public.

# Sessions need a stable secret so cookies survive restarts. A generated
# fallback keeps dev working but logs everyone out on restart, so set
# DASHBOARD_SECRET_KEY in production.
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(
    days=int(os.getenv("DASHBOARD_SESSION_DAYS", "30"))
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Behind Caddy the site is HTTPS, so keep cookies Secure. Set
    # DASHBOARD_COOKIE_SECURE=0 for plain-http local testing.
    SESSION_COOKIE_SECURE=os.getenv("DASHBOARD_COOKIE_SECURE", "1") != "0",
)

# Endpoints reachable without logging in: the login form itself and the PWA
# shell assets (manifest, service worker, icons). None expose account data.
PUBLIC_ENDPOINTS = {
    "login", "static", "manifest", "service_worker", "favicon", "profile_pic",
}

if not settings.login_enabled():
    app.logger.warning(
        "No dashboard password configured — login is DISABLED and HTML pages are "
        "open. Set DASHBOARD_PASSWORD (or one via the admin page) before removing "
        "proxy-level auth."
    )


@app.context_processor
def _inject_auth_flags() -> dict[str, Any]:
    """Expose login state to every template (for the logout / admin controls)."""
    return {
        "login_enabled": settings.login_enabled(),
        "authed": bool(session.get("authed")),
    }


@app.before_request
def _require_login():
    """Gate the HTML views behind the shared password.

    The API (/api/v1/*) authenticates itself, so it is skipped here. Static and
    PWA assets stay public so the app is installable and the login screen can
    style itself.
    """
    if not settings.login_enabled():
        return None
    if request.path.startswith("/api/"):
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("authed"):
        return None
    # Remember where they were headed so login can send them back.
    nxt = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=nxt))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not settings.login_enabled():
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if settings.verify_password(supplied):
            session["authed"] = True
            session.permanent = True
            return redirect(_safe_next(request.form.get("next")))
        error = "Incorrect password."
    if request.method == "GET" and session.get("authed"):
        return redirect(_safe_next(request.args.get("next")))
    return render_template(
        "login.html",
        handle=HANDLE,
        error=error,
        next=_safe_next(request.args.get("next")),
    )


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    """Settings the operator can change without SSH: AI token, model, password.

    Reachable only through the login gate (enforced by _require_login). Secret
    fields are write-only — a set value is shown as "configured", never echoed.
    Changes are written to data/config.json and take effect on the next request
    (login) or the next analysis (model/token) — no restart.
    """
    saved: list[str] = []
    errors: list[str] = []

    if request.method == "POST":
        model = (request.form.get("model") or "").strip()
        token = (request.form.get("ai_token") or "").strip()
        new_pw = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        updates: dict[str, str] = {}
        if model:
            updates["LLM_MODEL"] = model
            saved.append("model")
        if token:
            updates["LLM_API_KEY"] = token
            saved.append("AI token")
        if new_pw or confirm:
            if len(new_pw) < 6:
                errors.append("Password must be at least 6 characters.")
            elif new_pw != confirm:
                errors.append("The two password fields do not match.")
            else:
                settings.set_password(new_pw)
                saved.append("dashboard password")
        if updates:
            settings.update(updates)

    current_model = settings.get("LLM_MODEL") or settings.get(
        "CLAUDE_MODEL", "claude-opus-5"
    )
    provider_override = (
        settings.get("LLM_PROVIDER", os.getenv("LLM_PROVIDER", "auto")) or "auto"
    ).lower()
    if provider_override in ("anthropic", "openai", "cli"):
        provider = provider_override
    else:
        provider = "anthropic" if current_model.startswith("claude") else "openai"

    token_configured = settings.has_override("LLM_API_KEY") or bool(
        os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    )
    if settings.has_override("DASHBOARD_PASSWORD_HASH"):
        password_source = "custom (set here)"
    elif os.getenv("DASHBOARD_PASSWORD"):
        password_source = "from .env"
    else:
        password_source = "not set"

    return render_template(
        "admin.html",
        handle=HANDLE,
        active="admin",
        last_run=store.last_run_at(store.snapshots()),
        saved=saved,
        errors=errors,
        current_model=current_model,
        provider=provider,
        token_configured=token_configured,
        password_source=password_source,
    )


def _safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site — never an absolute URL."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


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


# --------------------------------------------------------------------------- #
# PWA shell — manifest, service worker, icons (all public, no account data)
# --------------------------------------------------------------------------- #

_MANIFEST = {
    "name": "IG Analyzer",
    "short_name": "IG Analyzer",
    "description": "Instagram performance analysis dashboard.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait-primary",
    "background_color": "#0B0E14",
    "theme_color": "#0B0E14",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Service worker: cache-first for /static/ assets only. HTML pages are always
# fetched from the network so login state and fresh reports are never bypassed
# by a stale cache. A manifest + service worker + icons make the app installable.
_SERVICE_WORKER = """\
const CACHE = 'ig-analyzer-v1';
const ASSETS = [
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/manifest.webmanifest',
];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === 'GET' && url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  }
});
"""


@app.route("/manifest.webmanifest")
def manifest() -> Response:
    return Response(json.dumps(_MANIFEST), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker() -> Response:
    resp = Response(_SERVICE_WORKER, mimetype="application/javascript")
    # Allow a root-scoped worker even though the file lives at /sw.js.
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/favicon.ico")
def favicon() -> Response:
    return send_from_directory(STATIC_DIR / "icons", "favicon-32.png")


# --------------------------------------------------------------------------- #
# Live profile picture (shown on the login screen)
# --------------------------------------------------------------------------- #
# Pulled from the Graph API and cached to disk. The Graph `profile_picture_url`
# is a short-lived CDN link, so we download the bytes and serve those; the cache
# refreshes at most once per TTL no matter how many people hit the login page,
# so this public route can't be used to hammer the Instagram API. Falls back to
# the bundled app icon whenever the token is missing or the fetch fails.

PROFILE_PIC_CACHE = RAW_DIR.parent / "profile_pic.jpg"
PROFILE_PIC_TTL = int(os.getenv("PROFILE_PIC_TTL", "21600"))  # 6 hours


def _refresh_profile_pic() -> bool:
    token = os.getenv("IG_TOKEN", "")
    uid = os.getenv("IG_USER_ID", "")
    base = os.getenv("IG_API_BASE", "https://graph.facebook.com/v21.0").rstrip("/")
    if not token or not uid:
        return False
    try:
        meta = requests.get(
            f"{base}/{uid}",
            params={"fields": "profile_picture_url", "access_token": token},
            timeout=10,
        )
        url = (meta.json() or {}).get("profile_picture_url")
        if not url:
            return False
        img = requests.get(url, timeout=10)
        if img.status_code != 200 or not img.content:
            return False
        PROFILE_PIC_CACHE.write_bytes(img.content)
        return True
    except Exception:  # noqa: BLE001 - any failure just falls back to the icon
        return False


@app.route("/profile-pic")
def profile_pic() -> Response:
    fresh = (
        PROFILE_PIC_CACHE.exists()
        and (time.time() - PROFILE_PIC_CACHE.stat().st_mtime) < PROFILE_PIC_TTL
    )
    if not fresh:
        _refresh_profile_pic()  # refresh in place; stale cache still serves on failure
    if PROFILE_PIC_CACHE.exists():
        resp = send_file(PROFILE_PIC_CACHE, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    return send_from_directory(STATIC_DIR / "icons", "icon-192.png")


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
