#!/usr/bin/env python3
"""Free-form questions about the account, answered from stored data.

The daily report answers questions `build_prompt()` decided to ask. This answers
the ones you think of afterwards — "which format held up best on weekends?",
"did the carousel experiment work?" — grounded in the same snapshots.

Nothing here calls Instagram. It reads what cron already collected, so an answer
costs one model call and no API quota.

Provider comes from main.py, so this follows LLM_PROVIDER/LLM_MODEL and works
against Anthropic, any OpenAI-compatible endpoint, or a local CLI without
knowing which is configured.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import store

BASE_DIR = Path(__file__).resolve().parent
USAGE_PATH = BASE_DIR / "data" / "chat_usage.json"

# Answering costs money per question, and the dashboard is reachable from the
# internet. A day cap means a leaked password cannot run up an unbounded bill.
DAILY_LIMIT = int(os.getenv("CHAT_DAILY_LIMIT", "50"))
MAX_QUESTION_CHARS = int(os.getenv("CHAT_MAX_QUESTION_CHARS", "500"))

# How much of the newest report to quote. Reports run ~8 KB; the cap stops a
# long one from crowding out the metrics further down the prompt.
REPORT_EXCERPT_CHARS = 6000


class ChatError(RuntimeError):
    """Anything the caller should see as a clean error rather than a 500."""


class RateLimited(ChatError):
    pass


# --------------------------------------------------------------------------- #
# Daily cap
# --------------------------------------------------------------------------- #


def _read_usage() -> dict[str, Any]:
    try:
        data = json.loads(USAGE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def usage_today() -> tuple[int, int]:
    """(questions asked today, the cap). Cheap enough to call on page load."""
    data = _read_usage()
    if data.get("date") != date.today().isoformat():
        return 0, DAILY_LIMIT
    return int(data.get("count", 0)), DAILY_LIMIT


def _consume() -> int:
    """Count one question, resetting when the date rolls over.

    Written through a temp file and renamed, so a crash mid-write cannot leave
    an unparseable counter that would wedge the endpoint.
    """
    today = date.today().isoformat()
    data = _read_usage()
    count = int(data.get("count", 0)) + 1 if data.get("date") == today else 1
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"date": today, "count": count}))
    tmp.replace(USAGE_PATH)
    return count


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


def build_context() -> str:
    """Everything stored, compressed into something a model can reason over."""
    rows = store.snapshots()
    runs = store.list_runs()
    handle = os.getenv("IG_HANDLE", "your_handle")

    lines = [f"# Account: @{handle}", ""]

    if not rows:
        lines.append("No snapshots have been collected yet.")
        return "\n".join(lines)

    lines.append("## Current metrics")
    for s in store.stat_row(rows):
        bit = f"- {s['label']}: {s['display']}{s.get('unit', '')}"
        delta = s.get("delta")
        if delta:
            arrow = "up" if delta["direction"] == "up" else "down"
            bit += f" ({arrow} {delta['value']} since the previous run)"
        lines.append(bit)

    lines += ["", f"## History — {len(rows)} run(s), oldest first"]
    for metric in store.METRICS:
        pts = store.series(rows, metric)
        if pts:
            rendered = ", ".join(f"{p['at'][:10]}={p['value']}" for p in pts)
            lines.append(f"- {store.METRICS[metric]['label']}: {rendered}")

    # The newest report already contains per-post and per-format breakdowns, so
    # quoting it is cheaper than rebuilding those aggregates here.
    if runs:
        report = store.read_report(runs[0]["id"]) or ""
        if report:
            excerpt = report[:REPORT_EXCERPT_CHARS]
            if len(report) > REPORT_EXCERPT_CHARS:
                excerpt += "\n…(report truncated)"
            lines += ["", f"## Latest report ({runs[0]['date']})", "", excerpt]

    return "\n".join(lines)


def build_prompt(question: str, context: str) -> str:
    return f"""You are analysing one Instagram account. Answer the question using \
only the data below.

Rules:
- Ground every number in the data. Do not invent metrics.
- If the data cannot answer the question, say so plainly and name what would be
  needed. Do not guess.
- Small samples are common here. Call out when a split is too small to conclude
  from rather than presenting it as a finding.
- Be concise and specific. Markdown is fine. No preamble.

<data>
{context}
</data>

Question: {question}"""


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #


def ask(question: str) -> dict[str, Any]:
    """Answer one question. Raises ChatError for anything the user should see."""
    question = (question or "").strip()
    if not question:
        raise ChatError("Ask a question first.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ChatError(
            f"Question is too long ({len(question)} chars, max {MAX_QUESTION_CHARS})."
        )

    used, limit = usage_today()
    if used >= limit:
        raise RateLimited(
            f"Daily question limit reached ({limit}). It resets at midnight UTC."
        )

    if not store.list_runs():
        raise ChatError("No analyses stored yet — run main.py first.")

    # Imported late: main.py pulls in the provider SDKs, and a dashboard that is
    # only serving HTML should not pay that memory cost at worker startup.
    import main

    context = build_context()
    prompt = build_prompt(question, context)

    provider = main.resolve_provider()
    try:
        if provider == "cli":
            answer = main._analyze_cli(prompt)
        elif provider == "anthropic":
            answer = main._analyze_anthropic(prompt)
        else:
            answer = main._analyze_openai_compatible(prompt)
    except Exception as exc:  # surfaced to the caller as a clean message
        raise ChatError(f"The model call failed: {exc}") from exc

    count = _consume()
    return {
        "question": question,
        "answer": _strip_wrapper(answer),
        "model": main.LLM_MODEL,
        "provider": provider,
        "questions_used_today": count,
        "daily_limit": limit,
    }


def _strip_wrapper(text: str) -> str:
    """CLI providers sometimes wrap output in a fenced block; unwrap it."""
    stripped = (text or "").strip()
    match = re.fullmatch(r"```(?:markdown|md)?\n(.*)\n```", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped
