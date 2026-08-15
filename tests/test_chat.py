"""Tests for the ask/chat layer. No network and no model calls."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import chat


@pytest.fixture(autouse=True)
def isolated_usage(tmp_path, monkeypatch):
    """Point the daily counter at a temp file so tests never touch real usage."""
    monkeypatch.setattr(chat, "USAGE_PATH", tmp_path / "chat_usage.json")
    yield


# --------------------------------------------------------------------------- #
# Daily cap
# --------------------------------------------------------------------------- #


def test_usage_starts_at_zero():
    assert chat.usage_today() == (0, chat.DAILY_LIMIT)


def test_consume_increments():
    assert chat._consume() == 1
    assert chat._consume() == 2
    assert chat.usage_today()[0] == 2


def test_counter_resets_on_a_new_day():
    chat.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    stale = (date.today() - timedelta(days=1)).isoformat()
    chat.USAGE_PATH.write_text(json.dumps({"date": stale, "count": 99}))
    assert chat.usage_today() == (0, chat.DAILY_LIMIT)
    assert chat._consume() == 1


def test_corrupt_counter_does_not_wedge_the_endpoint():
    chat.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    chat.USAGE_PATH.write_text("{not json")
    assert chat.usage_today() == (0, chat.DAILY_LIMIT)
    assert chat._consume() == 1


def test_limit_is_enforced_before_any_model_call(monkeypatch):
    monkeypatch.setattr(chat, "DAILY_LIMIT", 2)
    chat.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    chat.USAGE_PATH.write_text(json.dumps({"date": date.today().isoformat(), "count": 2}))
    # Fails on the cap, not on a missing provider — so no spend can slip past it.
    with pytest.raises(chat.RateLimited):
        chat.ask("anything")


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_question_rejected(bad):
    with pytest.raises(chat.ChatError):
        chat.ask(bad)


def test_overlong_question_rejected(monkeypatch):
    monkeypatch.setattr(chat, "MAX_QUESTION_CHARS", 10)
    with pytest.raises(chat.ChatError, match="too long"):
        chat.ask("x" * 11)


def test_a_rejected_question_costs_nothing():
    with pytest.raises(chat.ChatError):
        chat.ask("")
    assert chat.usage_today()[0] == 0


# --------------------------------------------------------------------------- #
# Context building
# --------------------------------------------------------------------------- #


def test_context_handles_no_snapshots(monkeypatch):
    monkeypatch.setattr(chat.store, "snapshots", lambda: [])
    monkeypatch.setattr(chat.store, "list_runs", lambda: [])
    assert "No snapshots" in chat.build_context()


def test_context_includes_metrics_and_history(monkeypatch):
    rows = [
        {"collected_at": "2026-08-01T00:00:00", "at": "2026-08-01 00:00",
         "followers": 100, "posts": 5, "engagement": 4.0, "reach": 500},
        {"collected_at": "2026-08-02T00:00:00", "at": "2026-08-02 00:00",
         "followers": 110, "posts": 6, "engagement": 5.5, "reach": 600},
    ]
    monkeypatch.setattr(chat.store, "snapshots", lambda: rows)
    monkeypatch.setattr(chat.store, "list_runs", lambda: [])
    ctx = chat.build_context()
    assert "Followers" in ctx
    assert "110" in ctx
    assert "2026-08-01" in ctx


def test_long_reports_are_truncated(monkeypatch):
    monkeypatch.setattr(chat, "REPORT_EXCERPT_CHARS", 50)
    monkeypatch.setattr(chat.store, "snapshots", lambda: [
        {"collected_at": "2026-08-01T00:00:00", "at": "2026-08-01 00:00",
         "followers": 1, "posts": 1, "engagement": 1.0, "reach": 1},
    ])
    monkeypatch.setattr(chat.store, "list_runs",
                        lambda: [{"id": "2026-08-01_000000", "date": "2026-08-01"}])
    monkeypatch.setattr(chat.store, "read_report", lambda rid: "y" * 5000)
    ctx = chat.build_context()
    assert "truncated" in ctx
    assert len(ctx) < 1500


def test_prompt_carries_question_and_data():
    prompt = chat.build_prompt("did reels win?", "FOLLOWERS: 10")
    assert "did reels win?" in prompt
    assert "FOLLOWERS: 10" in prompt
    # The grounding instruction is what keeps answers tied to real numbers.
    assert "Do not invent" in prompt


# --------------------------------------------------------------------------- #
# Response cleanup
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("```markdown\nhello\n```", "hello"),
        ("```\nhello\n```", "hello"),
        ("plain text", "plain text"),
        ("  padded  ", "padded"),
        # A fence *inside* a longer answer must survive — only a full wrapper goes.
        ("intro\n```\ncode\n```\nouttro", "intro\n```\ncode\n```\nouttro"),
    ],
)
def test_strip_wrapper(raw, expected):
    assert chat._strip_wrapper(raw) == expected
