#!/usr/bin/env python3
"""Live configuration overlay.

A small set of values (AI token, model, dashboard password) can be changed from
the admin page at runtime. Those overrides are written to a git-ignored
``data/config.json`` and read back on top of the environment, so a change takes
effect without editing ``.env`` or restarting the service.

Precedence:  config.json  >  environment (.env)  >  built-in default.

``data/`` is git-ignored and excluded from the deploy rsync, so config.json —
like ``.env`` — lives only on the server and is never committed or overwritten
by a deploy.

The dashboard password is stored as a hash, never in plaintext. The AI token is
a secret at rest (same as ``.env``); it is never echoed back to the browser.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "data" / "config.json"

# Keys that must never be sent back to the browser (write-only in the UI).
SECRET_KEYS = frozenset({"LLM_API_KEY", "DASHBOARD_PASSWORD_HASH"})

_lock = threading.Lock()


def _read() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default: str | None = None) -> str | None:
    """config.json override if set and non-empty, else the environment."""
    value = _read().get(key)
    if value is None or value == "":
        return os.getenv(key, default)
    return value


def _write(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(CONFIG_PATH)
    try:
        CONFIG_PATH.chmod(0o600)  # secrets → owner-only
    except OSError:
        pass


def update(values: dict[str, Any]) -> None:
    """Merge non-None values into config.json atomically. None means 'leave as is'."""
    with _lock:
        data = _read()
        for key, value in values.items():
            if value is None:
                continue
            data[key] = value
        _write(data)


def has_override(key: str) -> bool:
    """True when config.json holds a non-empty value for this key."""
    value = _read().get(key)
    return value is not None and value != ""


# --------------------------------------------------------------------------- #
# Dashboard password (stored hashed, verified live)
# --------------------------------------------------------------------------- #


def login_enabled() -> bool:
    """Login is on if a password is configured anywhere (config or env)."""
    return has_override("DASHBOARD_PASSWORD_HASH") or bool(
        os.getenv("DASHBOARD_PASSWORD")
    )


def verify_password(supplied: str) -> bool:
    """Check against the hashed override if present, else the env plaintext."""
    stored_hash = _read().get("DASHBOARD_PASSWORD_HASH")
    if stored_hash:
        from werkzeug.security import check_password_hash

        return check_password_hash(stored_hash, supplied)
    env_pw = os.getenv("DASHBOARD_PASSWORD", "")
    return bool(env_pw) and hmac.compare_digest(supplied, env_pw)


def set_password(new_password: str) -> None:
    from werkzeug.security import generate_password_hash

    update({"DASHBOARD_PASSWORD_HASH": generate_password_hash(new_password)})


# --------------------------------------------------------------------------- #
# LLM settings (read by main.py's analyze path each call)
# --------------------------------------------------------------------------- #


def llm_overrides() -> dict[str, str]:
    """The LLM keys that main.py should overlay onto its module globals."""
    out: dict[str, str] = {}
    for key in ("LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER"):
        if has_override(key):
            out[key] = _read()[key]
    return out
