"""Tests for the pure logic in main.py — no network, no API keys required.

Run:  python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


def make_post(**kwargs):
    base = {
        "id": "1",
        "media_type": "VIDEO",
        "media_product_type": "REELS",
        "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "permalink": "https://instagram.com/p/1",
        "caption": "a ride #bike",
        "like_count": 100,
        "comments_count": 10,
        "reach": 2000,
        "saved": 5,
        "shares": 2,
        "engagement_rate": 5.85,
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------- #
# Timestamp parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    ["2025-06-15T12:00:00+0000", "2025-06-15T12:00:00Z", "2025-06-15T12:00:00+00:00"],
)
def test_parse_ig_timestamp_accepts_api_shapes(value):
    parsed = main.parse_ig_timestamp(value)
    assert parsed is not None
    assert parsed.astimezone(timezone.utc) == NOW


@pytest.mark.parametrize("value", [None, "", "not a date", "15/06/2025"])
def test_parse_ig_timestamp_rejects_garbage(value):
    assert main.parse_ig_timestamp(value) is None


# --------------------------------------------------------------------------- #
# derive_analytics
# --------------------------------------------------------------------------- #


def test_derive_analytics_handles_no_posts():
    derived = main.derive_analytics({"media": []})
    assert derived["posts_analyzed"] == 0
    assert "by_format" not in derived


def test_derive_analytics_groups_by_format_and_time():
    posts = [
        make_post(id="1", media_product_type="REELS", engagement_rate=8.0),
        make_post(id="2", media_product_type="REELS", engagement_rate=4.0),
        make_post(id="3", media_product_type="FEED", engagement_rate=2.0),
    ]
    derived = main.derive_analytics({"media": posts})

    assert derived["posts_analyzed"] == 3
    assert derived["by_format"]["REELS"]["posts"] == 2
    assert derived["by_format"]["REELS"]["avg_engagement_rate"] == 6.0
    assert derived["by_format"]["FEED"]["avg_engagement_rate"] == 2.0
    # Local weekday/hour are stamped onto each post for the timing section.
    assert all("weekday" in p and "hour" in p for p in posts)


def test_derive_analytics_ranks_top_and_bottom():
    posts = [make_post(id=str(i), engagement_rate=float(i)) for i in range(1, 7)]
    derived = main.derive_analytics({"media": posts})
    assert derived["top_posts"][0]["engagement_rate"] == 6.0
    assert derived["bottom_posts"][0]["engagement_rate"] == 1.0


def test_derive_analytics_ignores_posts_without_engagement_rate():
    posts = [make_post(id="1", engagement_rate=None), make_post(id="2")]
    derived = main.derive_analytics({"media": posts})
    assert len(derived["top_posts"]) == 1


def test_derive_analytics_counts_hashtags():
    posts = [
        make_post(id="1", caption="#bike #ride"),
        make_post(id="2", caption="#bike only"),
        make_post(id="3", caption="no tags here"),
    ]
    derived = main.derive_analytics({"media": posts})
    tags = {h["tag"]: h["uses"] for h in derived["hashtags"]}
    # Only tags used at least twice are reported.
    assert tags == {"#bike": 2}
    assert derived["captions"]["avg_hashtags"] == 1.0


def test_derive_analytics_computes_cadence():
    posts = [
        make_post(id="1", timestamp="2025-06-15T12:00:00+0000"),
        make_post(id="2", timestamp="2025-06-08T12:00:00+0000"),
        make_post(id="3", timestamp="2025-06-01T12:00:00+0000"),
    ]
    cadence = main.derive_analytics({"media": posts})["cadence"]
    assert cadence["span_days"] == 14.0
    assert cadence["avg_gap_days"] == 7.0
    assert cadence["longest_gap_days"] == 7.0
    assert cadence["posts_per_week"] == 1.5


def test_derive_analytics_survives_bad_timestamps():
    posts = [make_post(id="1", timestamp="garbage"), make_post(id="2")]
    derived = main.derive_analytics({"media": posts})
    assert derived["posts_analyzed"] == 2
    assert derived["by_format"]["REELS"]["posts"] == 2


# --------------------------------------------------------------------------- #
# compute_trends
# --------------------------------------------------------------------------- #


def snapshot(followers, engagement, collected):
    data = {
        "collected_at": collected,
        "account": {"followers_count": followers, "media_count": 50, "metrics": {}},
        "derived": {"overall": {"avg_engagement_rate": engagement, "avg_reach": 1000}},
    }
    return data


def test_compute_trends_returns_none_without_previous():
    assert main.compute_trends(snapshot(100, 5.0, "2025-06-15T12:00:00+00:00"), None) is None


def test_compute_trends_reports_deltas():
    current = snapshot(1200, 5.0, "2025-06-15T12:00:00+00:00")
    previous = snapshot(1000, 4.0, "2025-06-08T12:00:00+00:00")
    trends = main.compute_trends(current, previous)

    assert trends["followers"]["change"] == 200
    assert trends["followers"]["pct_change"] == 20.0
    assert trends["avg_engagement_rate"]["change"] == 1.0
    assert trends["days_since_previous"] == 7.0


def test_compute_trends_handles_missing_values():
    current = snapshot(None, None, "2025-06-15T12:00:00+00:00")
    previous = snapshot(1000, 4.0, "2025-06-08T12:00:00+00:00")
    trends = main.compute_trends(current, previous)
    assert trends["followers"]["change"] is None
    assert trends["followers"]["pct_change"] is None


def test_compute_trends_avoids_divide_by_zero():
    current = snapshot(10, 1.0, "2025-06-15T12:00:00+00:00")
    previous = snapshot(0, 0.0, "2025-06-08T12:00:00+00:00")
    trends = main.compute_trends(current, previous)
    assert trends["followers"]["change"] == 10
    assert trends["followers"]["pct_change"] is None


# --------------------------------------------------------------------------- #
# Insight request windowing (the API rejects spans over 30 days)
# --------------------------------------------------------------------------- #


def test_windows_single_for_short_lookback():
    windows = main.InstagramAnalyzer(days=7)._windows()
    assert len(windows) == 1
    since, until = windows[0]
    assert 6.9 < (until - since) / 86400 < 7.1


def test_windows_split_for_long_lookback():
    windows = main.InstagramAnalyzer(days=90)._windows()
    assert len(windows) == 3
    assert all(
        (until - since) <= main.MAX_INSIGHTS_WINDOW_DAYS * 86400
        for since, until in windows
    )
    # Contiguous, no gaps.
    for (_, prev_until), (next_since, _) in zip(windows, windows[1:]):
        assert prev_until == next_since


# --------------------------------------------------------------------------- #
# Metric accumulation across both response shapes
# --------------------------------------------------------------------------- #


def test_accumulate_metrics_total_value_shape():
    totals = {}
    main.InstagramAnalyzer._accumulate_metrics(
        {"data": [{"name": "reach", "total_value": {"value": 500}}]}, totals
    )
    assert totals == {"reach": 500}


def test_accumulate_metrics_daily_values_shape():
    totals = {}
    main.InstagramAnalyzer._accumulate_metrics(
        {"data": [{"name": "reach", "values": [{"value": 10}, {"value": 15}]}]}, totals
    )
    assert totals == {"reach": 25}


def test_accumulate_metrics_sums_across_windows():
    totals = {"reach": 100}
    main.InstagramAnalyzer._accumulate_metrics(
        {"data": [{"name": "reach", "total_value": {"value": 50}}]}, totals
    )
    assert totals == {"reach": 150}


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #


def test_render_markdown_includes_trend_table_and_warnings():
    data = {
        "handle": "nsk.rides",
        "days_lookback": 30,
        "account": {"followers_count": 1200},
        "media": [make_post()],
        "derived": {"overall": {"avg_engagement_rate": 5.85}},
        "token": {"days_until_expiry": 3, "expires_at": "2025-06-18T00:00:00+00:00"},
        "trends": {
            "compared_with": "2025-06-08T12:00:00+00:00",
            "followers": {
                "previous": 1000,
                "current": 1200,
                "change": 200,
                "pct_change": 20.0,
            },
        },
        "collection_errors": ["audience.age: 400"],
    }
    out = main.render_markdown(data, "BODY", datetime(2025, 6, 15, 6, 0))

    assert "@nsk.rides" in out
    assert "| Followers | 1000 | 1200 | +200 (+20.0%) |" in out
    assert "IG_TOKEN expires in 3 day(s)" in out
    assert "audience.age: 400" in out
    assert out.rstrip().endswith("BODY")


def test_render_markdown_without_optional_sections():
    data = {
        "handle": "x",
        "days_lookback": 30,
        "account": {},
        "media": [],
        "derived": {},
    }
    out = main.render_markdown(data, "BODY", datetime(2025, 6, 15, 6, 0))
    assert "Since the previous run" not in out
    assert "⚠️" not in out


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def test_prompt_notes_absence_of_previous_run():
    data = {"handle": "x", "media": [], "derived": {"posts_analyzed": 0}}
    assert "no previous run" in main.build_prompt(data)


def test_prompt_flags_window_summed_metrics():
    data = {
        "handle": "x",
        "media": [],
        "derived": {"posts_analyzed": 3},
        "account": {"metrics_are_window_sums": True},
    }
    assert "double-counts" in main.build_prompt(data)


# --------------------------------------------------------------------------- #
# Dashboard path safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "date,filename",
    [
        ("../../etc", "analysis_060000.md"),
        ("2025-06-15", "../../../etc/passwd"),
        ("2025-06-15", "analysis_060000.md.bak"),
        ("2025-6-15", "analysis_060000.md"),
        ("2025-06-15", "notes.md"),
    ],
)
def test_dashboard_rejects_unsafe_paths(date, filename):
    flask = pytest.importorskip("flask")  # noqa: F841
    import dashboard
    from werkzeug.exceptions import HTTPException

    with pytest.raises(HTTPException):
        dashboard._safe_analysis_path(date, filename)


def test_dashboard_escapes_html_in_reports():
    pytest.importorskip("flask")
    import dashboard

    rendered = dashboard._render_md("caption: <script>alert(1)</script>")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
