#!/usr/bin/env python3
"""JSON API — the surface a mobile app (or anything else) reads.

Mounted at /api/v1 by dashboard.py. Read-only by design: collection takes about
two minutes, so a phone should never be waiting on it. Cron produces runs; this
serves what's already stored.

Auth: a bearer token from API_TOKEN in .env.

    curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:5000/api/v1/summary

With API_TOKEN unset the API refuses every request rather than serving your
account data unauthenticated — fail closed, because this is the surface most
likely to end up exposed to the internet.
"""

from __future__ import annotations

import hmac
import os
from functools import wraps
from typing import Any

from flask import Blueprint, jsonify, request

import store

api = Blueprint("api", __name__, url_prefix="/api/v1")

API_TOKEN = os.getenv("API_TOKEN", "")
HANDLE = os.getenv("IG_HANDLE", "your_handle")


def _error(message: str, status: int, **extra: Any):
    return jsonify({"error": {"message": message, **extra}}), status


def require_token(view):
    """Bearer-token gate. Constant-time compare so the token can't be guessed
    a character at a time by timing the response."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not API_TOKEN:
            return _error(
                "API is disabled. Set API_TOKEN in .env to enable it.",
                503,
                code="api_disabled",
            )
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, API_TOKEN):
            return _error(
                "Missing or invalid bearer token.", 401, code="unauthorized"
            )
        return view(*args, **kwargs)

    return wrapped


@api.get("/health")
def health():
    """Unauthenticated liveness check. Deliberately carries no account data."""
    return jsonify(
        {
            "ok": True,
            "runs": len(store.list_runs()),
            "api_enabled": bool(API_TOKEN),
        }
    )


@api.get("/summary")
@require_token
def summary():
    """Everything a home screen needs in one call."""
    rows = store.snapshots()
    return jsonify(
        {
            "handle": HANDLE,
            "last_run_at": rows[-1]["collected_at"] if rows else None,
            "run_count": len(store.list_runs()),
            "stats": store.stat_row(rows),
        }
    )


@api.get("/runs")
@require_token
def runs():
    """Newest first. `limit` caps the list; omit it for everything."""
    items = store.list_runs()
    try:
        limit = int(request.args.get("limit", 0))
    except ValueError:
        return _error("limit must be an integer", 400, code="bad_request")
    if limit > 0:
        items = items[:limit]
    return jsonify({"runs": items, "total": len(store.list_runs())})


@api.get("/runs/<run_id>")
@require_token
def run_detail(run_id: str):
    """One run: the report as markdown, plus the metrics behind it.

    Markdown rather than HTML on purpose — every mobile toolkit renders it, and
    it keeps presentation out of the API.
    """
    report = store.read_report(run_id)
    if report is None:
        return _error(f"No run with id {run_id!r}", 404, code="not_found")

    snapshot = store.snapshot_for(run_id) or {}
    account = snapshot.get("account") or {}
    derived = snapshot.get("derived") or {}
    return jsonify(
        {
            "id": run_id,
            "collected_at": snapshot.get("collected_at"),
            "report_markdown": report,
            "metrics": {
                "followers": account.get("followers_count"),
                "media_count": account.get("media_count"),
                "posts_analyzed": derived.get("posts_analyzed"),
                "overall": derived.get("overall"),
                "by_format": derived.get("by_format"),
                "cadence": derived.get("cadence"),
                "hashtags": derived.get("hashtags"),
            },
            "trends": snapshot.get("trends"),
            "audience": snapshot.get("audience"),
            "collection_errors": snapshot.get("collection_errors", []),
        }
    )


@api.get("/series")
@require_token
def metric_series():
    """A single metric over time, for charting on the client."""
    metric = request.args.get("metric", "followers")
    if metric not in store.METRICS:
        return _error(
            f"Unknown metric {metric!r}",
            400,
            code="bad_request",
            supported=sorted(store.METRICS),
        )
    rows = store.snapshots()
    spec = store.METRICS[metric]
    return jsonify(
        {
            "metric": metric,
            "label": spec["label"],
            "unit": spec.get("unit", ""),
            "points": store.series(rows, metric),
        }
    )
