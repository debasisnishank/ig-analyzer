#!/usr/bin/env python3
"""Detached worker for a "Sync now" run.

Spawned by sync.start(). Runs main.py exactly as cron does, streams its output to
data/logs/sync.log, and records the outcome in data/sync_status.json so the
dashboard can poll it. Kept separate from the web worker so a 2-minute collection
never blocks a request.
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
LOG_PATH = BASE_DIR / "data" / "logs" / "sync.log"


def _write(**fields) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(STATUS_PATH.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data.update(fields)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATUS_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    _write(state="running", pid=os.getpid(), message="Collecting…", finished_at=None)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "ab") as log:
        log.write(f"\n===== manual sync {_now()} =====\n".encode())
        log.flush()
        proc = subprocess.run(
            [sys.executable, str(BASE_DIR / "main.py")],
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=log,
        )
    if proc.returncode == 0:
        _write(state="done", finished_at=_now(), message="Up to date", returncode=0)
    else:
        _write(
            state="error",
            finished_at=_now(),
            message=f"Sync failed (exit {proc.returncode}). See data/logs/sync.log.",
            returncode=proc.returncode,
        )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
