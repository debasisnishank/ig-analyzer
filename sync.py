#!/usr/bin/env python3
"""On-demand data collection ("Sync now").

The dashboard is read-only and cron produces the daily snapshot. This lets an
operator trigger an extra collection from the UI without waiting for cron.

A full run (Instagram pull + model analysis) takes ~2 minutes, far too long to
hold an HTTP request open, so ``start()`` spawns a detached runner and returns
immediately. The page then polls ``status()``.

Guards, because each run spends Instagram API calls and a model call:
  * only one run at a time (a live pid blocks a second start),
  * a cooldown between manual runs.

State lives in ``data/sync_status.json``. Cron does not touch it, so the cooldown
only limits manual syncs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATUS_PATH = BASE_DIR / "data" / "sync_status.json"
RUNNER = BASE_DIR / "sync_runner.py"

COOLDOWN_SECONDS = int(os.getenv("SYNC_COOLDOWN", "300"))   # 5 min between manual syncs
STALE_SECONDS = int(os.getenv("SYNC_STALE", "900"))         # a "running" run older than
#                                                             this with a dead pid is stuck


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _read() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(**fields) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _read()
    data.update(fields)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATUS_PATH)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def status() -> dict:
    """Reconciled status: state in idle | running | done | error, plus a message.

    A run marked 'running' whose process has died and is older than STALE_SECONDS
    is reported as an error, so a crashed runner never wedges the button forever.
    """
    s = _read()
    state = s.get("state", "idle")
    message = s.get("message", "")

    if state == "running" and not _alive(s.get("pid")):
        started = _parse(s.get("started_at"))
        age = (_now() - started).total_seconds() if started else STALE_SECONDS + 1
        if age > STALE_SECONDS:
            state, message = "error", "Sync stopped unexpectedly. Check data/logs/sync.log."

    return {
        "state": state,
        "message": message,
        "started_at": s.get("started_at"),
        "finished_at": s.get("finished_at"),
    }


def can_start() -> tuple[bool, str]:
    s = status()
    if s["state"] == "running":
        return False, "A sync is already running."
    started = _parse(s.get("started_at"))
    if started:
        wait = COOLDOWN_SECONDS - (_now() - started).total_seconds()
        if wait > 0:
            mins = int(wait // 60) + 1
            return False, f"Just synced — try again in about {mins} min."
    return True, ""


def start() -> tuple[bool, str]:
    """Kick off a background collection if allowed. Returns (started, message)."""
    ok, why = can_start()
    if not ok:
        return False, why
    _write(
        state="running",
        pid=None,
        started_at=_now().isoformat(),
        finished_at=None,
        message="Collecting…",
    )
    # Detached so it outlives this request; same interpreter as the web worker
    # (the venv), which the runner reuses for main.py.
    subprocess.Popen(
        [sys.executable, str(RUNNER)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, "Collecting… (~2 min)"
