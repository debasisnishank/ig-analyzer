#!/usr/bin/env python3
"""Shared read layer over the files `main.py` writes.

Both readers sit on top of this — the HTML dashboard and the JSON API — so the
two can never drift apart on what a "run" or a "delta" means. Nothing in here
knows about HTML, SVG or Flask; it returns plain data.

Layout on disk:
    data/raw/data_YYYYMMDD_HHMMSS.json      full snapshot
    data/analyses/YYYY-MM-DD/analysis_HHMMSS.md   report

A run is identified across the API by `YYYY-MM-DD_HHMMSS`, which maps to exactly
one report and (usually) one snapshot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
ANALYSES_DIR = DATA_DIR / "analyses"

ANALYSIS_RE = re.compile(r"^analysis_(\d{6})\.md$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")

# Metrics exposed as time series, and how to format each one.
METRICS: dict[str, dict[str, Any]] = {
    "followers": {"label": "Followers", "precision": 0},
    "engagement": {"label": "Engagement rate", "precision": 2, "unit": "%"},
    "reach": {"label": "Reach per post", "precision": 0},
    "posts": {"label": "Posts in window", "precision": 0},
}


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


def run_id_for(date: str, filename: str) -> str | None:
    m = ANALYSIS_RE.match(filename)
    return f"{date}_{m.group(1)}" if m and DATE_RE.match(date) else None


def split_run_id(run_id: str) -> tuple[str, str] | None:
    """`2026-08-11_225933` -> ('2026-08-11', 'analysis_225933.md')."""
    if not RUN_ID_RE.match(run_id):
        return None
    date, stamp = run_id.split("_")
    return date, f"analysis_{stamp}.md"


def report_path(run_id: str) -> Path | None:
    """Resolve a run id to its report file, refusing anything outside the tree."""
    parts = split_run_id(run_id)
    if not parts:
        return None
    date, filename = parts
    root = ANALYSES_DIR.resolve()
    path = (root / date / filename).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def list_runs() -> list[dict[str, str]]:
    """Every stored report, newest first."""
    if not ANALYSES_DIR.exists():
        return []
    runs = []
    for f in sorted(ANALYSES_DIR.glob("*/analysis_*.md"), reverse=True):
        rid = run_id_for(f.parent.name, f.name)
        if not rid:
            continue
        stamp = f.stem.replace("analysis_", "")
        runs.append(
            {
                "id": rid,
                "date": f.parent.name,
                "filename": f.name,
                "time": f"{stamp[:2]}:{stamp[2:4]}",
                "size_bytes": f.stat().st_size,
            }
        )
    return runs


def read_report(run_id: str) -> str | None:
    path = report_path(run_id)
    return path.read_text() if path else None


def snapshot_for(run_id: str) -> dict[str, Any] | None:
    """The raw snapshot written alongside a report, when one exists."""
    parts = split_run_id(run_id)
    if not parts:
        return None
    date, _ = parts
    stamp = run_id.split("_")[1]
    candidate = RAW_DIR / f"data_{date.replace('-', '')}_{stamp}.json"
    if not candidate.is_file():
        return None
    try:
        return json.loads(candidate.read_text())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def snapshots() -> list[dict[str, Any]]:
    """Every stored snapshot, oldest first, reduced to its headline numbers."""
    rows = []
    for path in sorted(RAW_DIR.glob("data_*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        overall = (data.get("derived") or {}).get("overall") or {}
        collected = data.get("collected_at") or ""
        rows.append(
            {
                "collected_at": collected,
                "at": collected[:16].replace("T", " "),
                "followers": (data.get("account") or {}).get("followers_count"),
                "posts": len(data.get("media") or []),
                "engagement": overall.get("avg_engagement_rate"),
                "reach": overall.get("avg_reach"),
            }
        )
    return rows


def series(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """One metric over time, dropping runs where it wasn't collected."""
    return [
        {"at": r["collected_at"], "value": r[metric]}
        for r in rows
        if isinstance(r.get(metric), (int, float))
    ]


def values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    return [r[metric] for r in rows if isinstance(r.get(metric), (int, float))]


def stat(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    """Current value, change since the previous run, and the all-time range.

    `range` is omitted when every run holds the same value — there is no spread
    to place the current reading within, and drawing one would imply a precision
    the data doesn't have.
    """
    spec = METRICS[metric]
    precision = spec["precision"]
    nums = values(rows, metric)
    out: dict[str, Any] = {
        "metric": metric,
        "label": spec["label"],
        "unit": spec.get("unit", ""),
        "value": None,
        "display": "—",
        "delta": None,
    }
    if not nums:
        return out

    current = nums[-1]
    out["value"] = current
    out["display"] = f"{current:,.{precision}f}"

    if len(nums) >= 2:
        change = current - nums[-2]
        if abs(change) > 1e-9:
            out["delta"] = {
                "change": round(change, 4),
                "value": f"{abs(change):,.{precision or 2}f}",
                "direction": "up" if change > 0 else "down",
                "pct": (
                    round((change / abs(nums[-2])) * 100, 2) if nums[-2] else None
                ),
            }

    low, high = min(nums), max(nums)
    if high > low:
        out["range"] = {
            "low": low,
            "high": high,
            "low_display": f"{low:,.{precision}f}",
            "high_display": f"{high:,.{precision}f}",
            "position": round((current - low) / (high - low) * 100, 1),
        }
    out["history"] = nums[-12:]
    return out


def stat_row(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [stat(rows, m) for m in ("followers", "engagement", "reach", "posts")]


def last_run_at(rows: list[dict[str, Any]]) -> str | None:
    return rows[-1]["at"] if rows else None
