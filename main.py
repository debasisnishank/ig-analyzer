#!/usr/bin/env python3
"""Instagram Analyzer — automated daily analysis of an Instagram account via
the Instagram Graph API + Claude.

Pipeline: collect account/media/audience insights -> derive local aggregates ->
diff against the previous run -> send to Claude -> store a tactical markdown
report plus the raw JSON for re-analysis.

Notes on the API:
  * Insights require the Instagram Graph API with a Business/Creator account
    linked to a Facebook Page. The old "Basic Display" API is shut down and
    never returned insights, so it will not work here.
  * The Graph API surface changes often (metrics get deprecated). Every fetch is
    therefore defensive: a failed metric is recorded as an error and the run
    continues rather than crashing.
  * Account insights reject `since`/`until` ranges longer than 30 days, so the
    lookback window is requested in <=30-day chunks and summed.

Usage:
    python main.py                     # full run
    python main.py --dry-run           # collect + derive, print JSON, no Claude
    python main.py --days 14 --limit 50
    python main.py --reanalyze data/raw/data_20250115_060000.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover - surfaced clearly at runtime
    Anthropic = None  # type: ignore

try:  # Only needed when pointing at an OpenAI-compatible provider.
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

try:  # zoneinfo is stdlib on 3.9+; fall back to UTC on older interpreters.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
ANALYSES_DIR = DATA_DIR / "analyses"
LOG_DIR = DATA_DIR / "logs"

for _d in (RAW_DIR, ANALYSES_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

IG_TOKEN = os.getenv("IG_TOKEN", "")
IG_USER_ID = os.getenv("IG_USER_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ACCOUNT_HANDLE = os.getenv("IG_HANDLE", "your_handle")

# graph.facebook.com is the host that serves insights. If you set up "Instagram
# API with Instagram Login", switch this to https://graph.instagram.com.
API_BASE = os.getenv("IG_API_BASE", "https://graph.facebook.com/v21.0").rstrip("/")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DAYS_LOOKBACK = int(os.getenv("DAYS_LOOKBACK", "30"))
MEDIA_LIMIT = int(os.getenv("MEDIA_LIMIT", "30"))
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "high")

# Provider selection. Defaults keep the original Anthropic behaviour; set
# LLM_MODEL + LLM_BASE_URL to point at any OpenAI-compatible endpoint instead
# (DeepSeek, OpenRouter, Together, a local server, a gateway).
LLM_MODEL = os.getenv("LLM_MODEL", CLAUDE_MODEL)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "") or ANTHROPIC_API_KEY
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").lower()  # auto|anthropic|openai|cli

# provider=cli shells out to a local coding-agent CLI instead of calling an API.
# Useful when you have a CLI subscription but no API entitlement.
LLM_CLI = os.getenv("LLM_CLI", "cmdc")
LLM_CLI_TIMEOUT = int(os.getenv("LLM_CLI_TIMEOUT", "600"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))

# Posting-time advice is only meaningful in the audience's local time; the API
# returns UTC.
IG_TIMEZONE = os.getenv("IG_TIMEZONE", "UTC")

# The Graph API caps a single insights request at a 30-day span.
MAX_INSIGHTS_WINDOW_DAYS = 30

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "analyzer.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ig-analyzer")


def _local_tz():
    """The configured display timezone, falling back to UTC if unavailable."""
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(IG_TIMEZONE)
    except Exception:  # noqa: BLE001 - bad tz name / missing tzdata
        log.warning("Unknown IG_TIMEZONE %r — falling back to UTC.", IG_TIMEZONE)
        return timezone.utc


LOCAL_TZ = _local_tz()

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Small helpers (pure — unit tested in tests/)
# --------------------------------------------------------------------------- #


def parse_ig_timestamp(value: str | None) -> datetime | None:
    """Parse the Graph API's `2025-01-15T12:34:56+0000` timestamps."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.fmean(clean), 2) if clean else None


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / abs(previous) * 100, 2)


def _delta(current: float | None, previous: float | None) -> dict[str, Any]:
    change = None
    if isinstance(current, (int, float)) and isinstance(previous, (int, float)):
        change = round(current - previous, 2)
    return {
        "current": current,
        "previous": previous,
        "change": change,
        "pct_change": _pct_change(current, previous),
    }


def derive_analytics(data: dict[str, Any]) -> dict[str, Any]:
    """Aggregate the raw post list into the shapes the report actually needs.

    Claude can add these up itself, but doing it here makes the numbers exact,
    keeps the prompt smaller, and means the same figures land in the markdown
    header regardless of what the model writes.
    """
    posts = [p for p in data.get("media", []) if isinstance(p, dict)]
    out: dict[str, Any] = {"timezone": str(LOCAL_TZ), "posts_analyzed": len(posts)}
    if not posts:
        return out

    by_format: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_weekday: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_hour: dict[int, list[dict[str, Any]]] = defaultdict(list)
    timestamps: list[datetime] = []

    for post in posts:
        fmt = post.get("media_product_type") or post.get("media_type") or "UNKNOWN"
        by_format[fmt].append(post)
        when = parse_ig_timestamp(post.get("timestamp"))
        if when is None:
            continue
        local = when.astimezone(LOCAL_TZ)
        timestamps.append(local)
        # Stamped onto the post so the model sees local time, not UTC.
        post["local_time"] = local.strftime("%Y-%m-%d %H:%M")
        post["weekday"] = WEEKDAYS[local.weekday()]
        post["hour"] = local.hour
        by_weekday[post["weekday"]].append(post)
        by_hour[local.hour].append(post)

    def summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "posts": len(group),
            "avg_engagement_rate": _mean([p.get("engagement_rate") for p in group]),
            "avg_reach": _mean([p.get("reach") for p in group]),
            "avg_views": _mean([p.get("views") for p in group]),
            "avg_likes": _mean([p.get("like_count") for p in group]),
            "avg_comments": _mean([p.get("comments_count") for p in group]),
            "avg_saved": _mean([p.get("saved") for p in group]),
            "avg_shares": _mean([p.get("shares") for p in group]),
        }

    out["by_format"] = {k: summarize(v) for k, v in sorted(by_format.items())}
    out["by_weekday"] = {
        day: summarize(by_weekday[day]) for day in WEEKDAYS if day in by_weekday
    }
    out["by_hour"] = {str(h): summarize(by_hour[h]) for h in sorted(by_hour)}

    ranked = sorted(
        (p for p in posts if p.get("engagement_rate") is not None),
        key=lambda p: p["engagement_rate"],
        reverse=True,
    )

    def brief(post: dict[str, Any]) -> dict[str, Any]:
        return {
            "permalink": post.get("permalink"),
            "format": post.get("media_product_type") or post.get("media_type"),
            "local_time": post.get("local_time"),
            "weekday": post.get("weekday"),
            "engagement_rate": post.get("engagement_rate"),
            "reach": post.get("reach"),
            "caption_preview": (post.get("caption") or "")[:160],
        }

    out["top_posts"] = [brief(p) for p in ranked[:5]]
    out["bottom_posts"] = [brief(p) for p in ranked[-5:][::-1]]

    # Caption shape — length and hashtag count, split at the median so the model
    # can see whether either actually tracks engagement.
    captions = [(p, p.get("caption") or "") for p in posts]
    out["captions"] = {
        "avg_length": _mean([len(c) for _, c in captions]),
        "avg_hashtags": _mean([len(re.findall(r"#\w+", c)) for _, c in captions]),
        "posts_without_caption": sum(1 for _, c in captions if not c.strip()),
    }
    rated = [(p, c) for p, c in captions if p.get("engagement_rate") is not None]
    if len(rated) >= 4:
        counts = sorted(len(re.findall(r"#\w+", c)) for _, c in rated)
        median = counts[len(counts) // 2]
        low = [p for p, c in rated if len(re.findall(r"#\w+", c)) <= median]
        high = [p for p, c in rated if len(re.findall(r"#\w+", c)) > median]
        out["captions"]["hashtag_split"] = {
            "median_hashtags": median,
            "at_or_below_median": summarize(low),
            "above_median": summarize(high),
        }

    top_tags: dict[str, list[float]] = defaultdict(list)
    for post, caption in rated:
        for tag in {t.lower() for t in re.findall(r"#\w+", caption)}:
            top_tags[tag].append(post["engagement_rate"])
    out["hashtags"] = sorted(
        (
            {"tag": tag, "uses": len(rates), "avg_engagement_rate": _mean(rates)}
            for tag, rates in top_tags.items()
            if len(rates) >= 2
        ),
        key=lambda h: (h["uses"], h["avg_engagement_rate"] or 0),
        reverse=True,
    )[:15]

    # Cadence — how often posts actually go out, and the longest silence.
    if len(timestamps) >= 2:
        timestamps.sort()
        gaps = [
            (b - a).total_seconds() / 86400
            for a, b in zip(timestamps, timestamps[1:])
        ]
        span_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400
        out["cadence"] = {
            "first_post": timestamps[0].strftime("%Y-%m-%d %H:%M"),
            "last_post": timestamps[-1].strftime("%Y-%m-%d %H:%M"),
            "span_days": round(span_days, 1),
            "posts_per_week": (
                round(len(timestamps) / (span_days / 7), 2) if span_days >= 1 else None
            ),
            "avg_gap_days": _mean(gaps),
            "longest_gap_days": round(max(gaps), 2),
        }

    out["overall"] = summarize(posts)
    return out


def compute_trends(
    current: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Diff this run's headline numbers against the previous stored snapshot."""
    if not previous:
        return None

    prev_account = previous.get("account", {}) or {}
    cur_account = current.get("account", {}) or {}
    prev_derived = previous.get("derived", {}) or {}
    cur_derived = current.get("derived", {}) or {}

    trends: dict[str, Any] = {
        "compared_with": previous.get("collected_at"),
        "followers": _delta(
            cur_account.get("followers_count"), prev_account.get("followers_count")
        ),
        "media_count": _delta(
            cur_account.get("media_count"), prev_account.get("media_count")
        ),
        "avg_engagement_rate": _delta(
            (cur_derived.get("overall") or {}).get("avg_engagement_rate"),
            (prev_derived.get("overall") or {}).get("avg_engagement_rate"),
        ),
        "avg_reach_per_post": _delta(
            (cur_derived.get("overall") or {}).get("avg_reach"),
            (prev_derived.get("overall") or {}).get("avg_reach"),
        ),
    }

    prev_start = parse_ig_timestamp(previous.get("collected_at"))
    cur_start = parse_ig_timestamp(current.get("collected_at"))
    if prev_start and cur_start:
        trends["days_since_previous"] = round(
            (cur_start - prev_start).total_seconds() / 86400, 2
        )

    metrics = {}
    prev_metrics = prev_account.get("metrics", {}) or {}
    for name, value in (cur_account.get("metrics", {}) or {}).items():
        metrics[name] = _delta(value, prev_metrics.get(name))
    if metrics:
        trends["account_metrics"] = metrics

    return trends


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #


class InstagramAnalyzer:
    """Collects Instagram insights and turns them into a Claude-authored report."""

    # Graph API error codes that mean "slow down", not "you did it wrong".
    RETRYABLE_ERROR_CODES = {4, 17, 32, 613}

    def __init__(self, days: int | None = None, limit: int | None = None) -> None:
        self.token = IG_TOKEN
        self.user_id = IG_USER_ID
        self.days = days or DAYS_LOOKBACK
        self.limit = limit or MEDIA_LIMIT
        self.session = requests.Session()
        self.errors: list[str] = []
        # media_product_type -> metric names known to work for it
        self._metric_support: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ #
    # Low-level HTTP helper
    # ------------------------------------------------------------------ #

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a Graph API endpoint, retrying throttles and 5xx.

        Raises requests.HTTPError once the retries are exhausted.
        """
        params = dict(params or {})
        params["access_token"] = self.token
        url = f"{API_BASE}/{path.lstrip('/')}"

        last_error: Exception | None = None
        for attempt in range(HTTP_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if resp.status_code == 200:
                    return resp.json()

                # Surface the Graph API error body — it's usually specific.
                code = None
                try:
                    error = resp.json().get("error", {})
                    detail = error.get("message", resp.text)
                    code = error.get("code")
                except ValueError:
                    detail = resp.text
                last_error = requests.HTTPError(f"{resp.status_code}: {detail}")

                throttled = resp.status_code == 429 or code in self.RETRYABLE_ERROR_CODES
                if not (throttled or resp.status_code >= 500):
                    raise last_error

            if attempt < HTTP_RETRIES:
                backoff = 2**attempt
                log.warning(
                    "  ~ %s (attempt %d/%d) — retrying in %ds",
                    self._redact(last_error),
                    attempt + 1,
                    HTTP_RETRIES + 1,
                    backoff,
                )
                time.sleep(backoff)

        raise last_error if last_error else requests.HTTPError("request failed")

    def _redact(self, text: Any) -> str:
        """Strip the access token out of anything logged, stored or displayed.

        Connection errors from `requests` embed the full request URL, and the
        token rides in the query string — so an unredacted error message ends up
        in analyzer.log, the raw JSON snapshot, and the report itself.
        """
        out = str(text)
        if self.token:
            out = out.replace(self.token, "***REDACTED***")
        return out

    def _record_error(self, where: str, exc: Exception) -> None:
        msg = f"{where}: {self._redact(exc)}"
        self.errors.append(msg)
        log.warning("  ! %s", msg)

    # ------------------------------------------------------------------ #
    # 0. Token health
    # ------------------------------------------------------------------ #

    def check_token(self) -> dict[str, Any]:
        """Report token expiry so a silent 60-day expiry doesn't kill the cron.

        Best-effort: `debug_token` needs an app or user token with the right
        scope, so a failure here is informational, not fatal.
        """
        try:
            data = self._get(
                "debug_token", {"input_token": self.token}
            ).get("data", {})
        except Exception as exc:  # noqa: BLE001
            log.debug("Token introspection unavailable: %s", exc)
            return {}

        expires_at = data.get("expires_at")
        info: dict[str, Any] = {
            "is_valid": data.get("is_valid"),
            "scopes": data.get("scopes"),
        }
        if expires_at:
            expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
            days_left = (expiry - datetime.now(timezone.utc)).days
            info["expires_at"] = expiry.isoformat()
            info["days_until_expiry"] = days_left
            if days_left <= 7:
                log.warning(
                    "IG_TOKEN expires in %d day(s) (%s) — re-exchange it soon.",
                    days_left,
                    expiry.date(),
                )
        if data.get("is_valid") is False:
            log.error("IG_TOKEN is reported invalid by the Graph API.")
        return info

    # ------------------------------------------------------------------ #
    # 1. Account insights
    # ------------------------------------------------------------------ #

    def fetch_account_insights(self) -> dict[str, Any]:
        """Account-level metrics over the lookback window.

        Metric names have churned a lot: `impressions` was deprecated in 2024, so
        we request the current set and fall back gracefully when a metric is
        rejected for this account.
        """
        log.info("Fetching account insights (%d-day window)...", self.days)
        out: dict[str, Any] = {"followers_count": None, "metrics": {}}

        # Static profile fields (not time-series).
        try:
            profile = self._get(
                self.user_id,
                {"fields": "followers_count,follows_count,media_count,username"},
            )
            out["followers_count"] = profile.get("followers_count")
            out["follows_count"] = profile.get("follows_count")
            out["media_count"] = profile.get("media_count")
            out["username"] = profile.get("username")
        except Exception as exc:  # noqa: BLE001
            self._record_error("account.profile", exc)

        metrics = ["reach", "profile_views", "accounts_engaged", "total_interactions"]
        totals: dict[str, float] = {}
        failed: set[str] = set()

        # Requested per <=30-day window and summed: the API rejects longer spans.
        for since, until in self._windows():
            try:
                data = self._get(
                    f"{self.user_id}/insights",
                    {
                        "metric": ",".join(m for m in metrics if m not in failed),
                        "period": "day",
                        "metric_type": "total_value",
                        "since": since,
                        "until": until,
                    },
                )
                self._accumulate_metrics(data, totals)
            except Exception as exc:  # noqa: BLE001
                self._record_error("account.insights", exc)
                # Retry one-by-one so a single bad metric doesn't lose the rest.
                for metric in metrics:
                    if metric in failed:
                        continue
                    try:
                        data = self._get(
                            f"{self.user_id}/insights",
                            {
                                "metric": metric,
                                "period": "day",
                                "metric_type": "total_value",
                                "since": since,
                                "until": until,
                            },
                        )
                        self._accumulate_metrics(data, totals)
                    except Exception as inner:  # noqa: BLE001
                        self._record_error(f"account.insights.{metric}", inner)
                        failed.add(metric)

        # Summed across windows, so `reach` is the sum of per-window reach rather
        # than a deduplicated figure. Flagged in the prompt.
        out["metrics"] = {k: round(v, 2) for k, v in totals.items()}
        out["metrics_are_window_sums"] = self.days > MAX_INSIGHTS_WINDOW_DAYS
        return out

    @staticmethod
    def _accumulate_metrics(payload: dict[str, Any], totals: dict[str, float]) -> None:
        """Add one insights response into the running totals, either shape."""
        for item in payload.get("data", []):
            name = item.get("name")
            if not name:
                continue
            total = (item.get("total_value") or {}).get("value")
            if total is None:
                # Older shape: list of daily values.
                values = item.get("values", [])
                total = sum(v.get("value", 0) or 0 for v in values) if values else None
            if total is not None:
                totals[name] = totals.get(name, 0) + total

    def _windows(self) -> list[tuple[int, int]]:
        """Split the lookback into (since, until) unix pairs of <=30 days."""
        now = int(datetime.now(timezone.utc).timestamp())
        start = now - self.days * 86400
        step = MAX_INSIGHTS_WINDOW_DAYS * 86400
        windows = []
        cursor = start
        while cursor < now:
            windows.append((cursor, min(cursor + step, now)))
            cursor += step
        return windows or [(start, now)]

    # ------------------------------------------------------------------ #
    # 2. Media (post-level) data
    # ------------------------------------------------------------------ #

    # Requested together first; on rejection each is retried alone so one
    # deprecated name doesn't cost the rest.
    MEDIA_METRICS = ["reach", "views", "saved", "shares", "total_interactions"]

    def fetch_media(self) -> list[dict[str, Any]]:
        """Recent posts with engagement, enriched with per-post insights and an
        engagement-rate estimate. Paginates and stops at the lookback cutoff."""
        log.info(
            "Fetching media (up to %d posts, last %d days)...", self.limit, self.days
        )
        posts: list[dict[str, Any]] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days)
        fields = (
            "id,caption,media_type,media_product_type,timestamp,permalink,"
            "like_count,comments_count"
        )
        params: dict[str, Any] = {"fields": fields, "limit": min(self.limit, 50)}
        reached_cutoff = False

        while len(posts) < self.limit and not reached_cutoff:
            try:
                data = self._get(f"{self.user_id}/media", params)
            except Exception as exc:  # noqa: BLE001
                self._record_error("media.list", exc)
                break

            batch = data.get("data", [])
            for item in batch:
                when = parse_ig_timestamp(item.get("timestamp"))
                if when and when < cutoff:
                    # The feed is newest-first, so everything after this is older.
                    reached_cutoff = True
                    break
                if len(posts) >= self.limit:
                    break
                posts.append(self._build_post(item))

            # Paginate with the `after` cursor rather than `paging.next` — the
            # latter is a fully-signed URL that _get would mangle.
            after = ((data.get("paging") or {}).get("cursors") or {}).get("after")
            if reached_cutoff or not batch or not after or len(posts) >= self.limit:
                break
            params = dict(params, after=after)

        followers = None  # resolved lazily, only if some post lacks reach

        # Engagement rate needs a denominator; resolve followers once if any post
        # is missing reach.
        if any(p.get("engagement_rate") is None for p in posts):
            followers = self._followers_count()
            for post in posts:
                if post.get("engagement_rate") is None and followers:
                    post["engagement_rate"] = round(
                        post["_engagement"] / followers * 100, 2
                    )
                    post["engagement_rate_basis"] = "followers"
        for post in posts:
            post.pop("_engagement", None)

        log.info("  Retrieved %d posts.", len(posts))
        return posts

    def _build_post(self, item: dict[str, Any]) -> dict[str, Any]:
        post = {
            "id": item.get("id"),
            "media_type": item.get("media_type"),
            "media_product_type": item.get("media_product_type"),
            "timestamp": item.get("timestamp"),
            "permalink": item.get("permalink"),
            "caption": (item.get("caption") or "")[:500],
            "like_count": item.get("like_count", 0) or 0,
            "comments_count": item.get("comments_count", 0) or 0,
        }
        media_kind = post["media_product_type"] or post["media_type"] or "UNKNOWN"
        post.update(self._fetch_media_insights(item.get("id"), media_kind))

        # Engagement counts every interaction the API gave us, not just likes.
        engagement = (
            post["like_count"]
            + post["comments_count"]
            + (post.get("saved") or 0)
            + (post.get("shares") or 0)
        )
        post["engagement"] = engagement
        post["_engagement"] = engagement

        # Rate against reach when we have it — that's the honest denominator.
        # Posts with no reach fall back to followers once, after the loop.
        denom = post.get("reach")
        post["engagement_rate"] = (
            round(engagement / denom * 100, 2) if denom else None
        )
        if denom:
            post["engagement_rate_basis"] = "reach"
        return post

    def _fetch_media_insights(
        self, media_id: str | None, media_kind: str
    ) -> dict[str, Any]:
        """Per-post insights.

        Which metrics exist depends on the media type (a carousel has no
        `views`), and probing metric-by-metric on every post would multiply the
        request count. So the supported set is discovered once per media kind
        and reused for every later post of that kind — one request each.
        """
        if not media_id:
            return {}

        known = self._metric_support.get(media_kind)
        if known is not None:
            if not known:
                return {}
            try:
                return self._read_insights(media_id, known)
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"media.insights.{media_id}", exc)
                return {}

        try:
            out = self._read_insights(media_id, self.MEDIA_METRICS)
            self._metric_support[media_kind] = list(self.MEDIA_METRICS)
            return out
        except Exception:  # noqa: BLE001 - probe individually below
            pass

        out = {}
        supported: list[str] = []
        for metric in self.MEDIA_METRICS:
            try:
                values = self._read_insights(media_id, [metric])
            except Exception as exc:  # noqa: BLE001
                # Expected: metrics differ by media type. Logged at debug so the
                # user-facing error list stays meaningful.
                log.debug("media.insights.%s.%s: %s", media_kind, metric, exc)
                continue
            supported.append(metric)
            out.update(values)

        self._metric_support[media_kind] = supported
        log.info("  %s supports: %s", media_kind, ", ".join(supported) or "no insights")
        return out

    def _read_insights(self, media_id: str, metrics: list[str]) -> dict[str, Any]:
        payload = self._get(f"{media_id}/insights", {"metric": ",".join(metrics)})
        out: dict[str, Any] = {}
        for item in payload.get("data", []):
            name = item.get("name")
            value = (item.get("total_value") or {}).get("value")
            if value is None:
                values = item.get("values", [])
                value = values[0].get("value") if values else None
            if name:
                out[name] = value
        return out

    # ------------------------------------------------------------------ #
    # 3. Audience demographics
    # ------------------------------------------------------------------ #

    def fetch_audience_insights(self) -> dict[str, Any]:
        """Follower demographics by country, age and gender.

        Requires 100+ followers; otherwise the API refuses and we return a
        warning instead of failing the run.
        """
        log.info("Fetching audience demographics...")
        out: dict[str, Any] = {}
        breakdowns = {"country": "country", "age": "age", "gender": "gender"}
        for label, breakdown in breakdowns.items():
            try:
                data = self._get(
                    f"{self.user_id}/insights",
                    {
                        "metric": "follower_demographics",
                        "period": "lifetime",
                        "metric_type": "total_value",
                        "breakdown": breakdown,
                    },
                )
                results = []
                for item in data.get("data", []):
                    tv = item.get("total_value", {})
                    for entry in tv.get("breakdowns", [{}])[0].get("results", []):
                        results.append(
                            {
                                "key": "/".join(entry.get("dimension_values", [])),
                                "value": entry.get("value"),
                            }
                        )
                out[label] = sorted(
                    results, key=lambda r: r["value"] or 0, reverse=True
                )
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"audience.{label}", exc)
                out[label] = {"warning": f"unavailable ({exc})"}
        return out

    # ------------------------------------------------------------------ #
    # 4. Aggregate
    # ------------------------------------------------------------------ #

    def collect_data(self) -> dict[str, Any]:
        log.info("Collecting all data...")
        data = {
            "handle": ACCOUNT_HANDLE,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "days_lookback": self.days,
            "token": self.check_token(),
            "account": self.fetch_account_insights(),
            "media": self.fetch_media(),
            "audience": self.fetch_audience_insights(),
        }
        data["derived"] = derive_analytics(data)
        trends = compute_trends(data, load_previous_snapshot())
        if trends:
            data["trends"] = trends
            log.info(
                "  Comparing against the run from %s.", trends.get("compared_with")
            )
        if self.errors:
            data["collection_errors"] = list(self.errors)
        return data

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _followers_count(self) -> int | None:
        try:
            profile = self._get(self.user_id, {"fields": "followers_count"})
            return profile.get("followers_count")
        except Exception as exc:  # noqa: BLE001
            self._record_error("followers_count", exc)
            return None

    def missing_config(self, require_claude: bool = True) -> list[str]:
        required = [("IG_TOKEN", self.token), ("IG_USER_ID", self.user_id)]
        if require_claude:
            required.append(("ANTHROPIC_API_KEY / LLM_API_KEY", LLM_API_KEY))
        return [name for name, val in required if not val]


# --------------------------------------------------------------------------- #
# 5. Claude analysis
# --------------------------------------------------------------------------- #


def build_prompt(data: dict[str, Any]) -> str:
    derived = data.get("derived", {})
    trends = data.get("trends")
    return (
        f"You are a growth strategist analyzing the Instagram account "
        f"@{data.get('handle')}. Using ONLY the data below, produce a tactical, "
        f"actionable report. No generic advice — every claim must cite the "
        f"actual numbers.\n\n"
        f"Structure the report with these sections:\n"
        f"1. **Top 3 Content Gaps** — specific content types/formats missing or "
        f"underused, inferred from what's posted vs. what performs.\n"
        f"2. **Engagement Patterns** — what's working (formats, timing, caption "
        f"style, topics) backed by the metrics.\n"
        f"3. **Posting Strategy** — concrete recommendations on timing, format "
        f"mix, and captions.\n"
        f"4. **30-Day Growth Roadmap** — a week-by-week list of specific "
        f"actions.\n\n"
        f"Reading the data:\n"
        f"- `derived` holds pre-computed aggregates; times in it are local "
        f"({derived.get('timezone', 'UTC')}), while `media[].timestamp` is UTC. "
        f"Use the local values for any timing advice.\n"
        f"- `engagement_rate` is engagement over reach where reach was "
        f"available, otherwise over followers — check `engagement_rate_basis` "
        f"before comparing posts.\n"
        + (
            "- `account.metrics` are summed across 30-day request windows, so "
            "`reach` double-counts people reached in more than one window.\n"
            if data.get("account", {}).get("metrics_are_window_sums")
            else ""
        )
        + (
            "- `trends` compares this run with the previous stored run. Call out "
            "what moved and say whether the recommendations from that period "
            "appear to be working.\n"
            if trends
            else "- There is no previous run to compare against yet.\n"
        )
        + f"- Sample size is {derived.get('posts_analyzed', 0)} posts; say so "
        f"plainly where a split is too small to conclude from, rather than "
        f"asserting a pattern.\n\n"
        f"If demographics or some metrics are missing, note it briefly and work "
        f"with what's available.\n\n"
        f"Data:\n```json\n{json.dumps(data, indent=2, default=str)}\n```\n"
    )


def resolve_provider() -> str:
    """Which API dialect to speak: 'anthropic' or 'openai'.

    `openai` covers any OpenAI-compatible endpoint — DeepSeek's own API,
    OpenRouter, Together, a local server, or a gateway like Command Code. Auto
    means: Claude model ids talk the Anthropic dialect, everything else doesn't.
    """
    if LLM_PROVIDER in ("anthropic", "openai", "cli"):
        return LLM_PROVIDER
    return "anthropic" if LLM_MODEL.startswith("claude") else "openai"


def analyze_with_claude(data: dict[str, Any]) -> str:
    """Send the dataset for analysis and return the report markdown.

    Named for its original single-provider life; it now dispatches on the
    configured provider.
    """
    provider = resolve_provider()
    log.info("Sending data to %s (%s)...", provider, LLM_MODEL)
    prompt = build_prompt(data)
    if provider == "cli":
        return _analyze_cli(prompt)
    if not LLM_API_KEY:
        raise RuntimeError(
            "No API key set. Put ANTHROPIC_API_KEY (or LLM_API_KEY) in .env"
        )
    if provider == "anthropic":
        return _analyze_anthropic(prompt)
    return _analyze_openai_compatible(prompt)


def _analyze_cli(prompt: str) -> str:
    """Pipe the prompt through a local coding-agent CLI in non-interactive mode.

    For CLI subscriptions that don't include API access. The prompt goes in on
    stdin (too large for argv on some systems) and the report comes back on
    stdout. Runs in a scratch directory so the agent can't wander into this
    repo looking for context it doesn't need.
    """
    cmd = [
        LLM_CLI,
        "-p",
        "-m",
        LLM_MODEL,
        "--no-session",
        "--skip-onboarding",
        "--max-turns",
        "1",
    ]
    if shutil.which(LLM_CLI) is None:
        raise RuntimeError(
            f"{LLM_CLI!r} not found on PATH. Install it, or set LLM_PROVIDER to "
            "'anthropic'/'openai' with an API key."
        )

    log.info("  Running %s (this is slower than an API call)...", " ".join(cmd[:4]))
    with tempfile.TemporaryDirectory(prefix="ig-analyzer-cli-") as workdir:
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=LLM_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{LLM_CLI} did not finish within {LLM_CLI_TIMEOUT}s. Raise "
                "LLM_CLI_TIMEOUT or use a faster model."
            ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"{LLM_CLI} exited {proc.returncode}: {detail}")

    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError(f"{LLM_CLI} returned no output — nothing to save.")
    return text


def _analyze_anthropic(prompt: str) -> str:
    if Anthropic is None:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    if not LLM_BASE_URL and not LLM_API_KEY.startswith("sk-ant-"):
        # Caught before the request so a wrong-service key doesn't look like an
        # outage. Anthropic keys are `sk-ant-api03-...`; a Claude Code or
        # claude.ai subscription is not an API credential.
        raise RuntimeError(
            "The API key does not look like an Anthropic API key "
            f"(starts with {LLM_API_KEY[:5]!r}, expected 'sk-ant-'). Create one "
            "at https://console.anthropic.com/settings/keys — a Claude Code / "
            "claude.ai subscription is billed separately and does not grant API "
            "access. To use a non-Anthropic model instead, set LLM_MODEL and "
            "LLM_BASE_URL (see .env.example)."
        )

    client = Anthropic(
        api_key=LLM_API_KEY, **({"base_url": LLM_BASE_URL} if LLM_BASE_URL else {})
    )
    kwargs: dict[str, Any] = {
        "model": LLM_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": CLAUDE_EFFORT},
    }

    try:
        message = client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        _raise_known_api_error(exc)
        # Older/smaller models reject `thinking` and `effort`; retry plainly so
        # switching LLM_MODEL to e.g. a Haiku model still works.
        if "thinking" in str(exc) or "effort" in str(exc) or "output_config" in str(exc):
            log.warning("Model rejected thinking/effort — retrying without them.")
            kwargs.pop("thinking", None)
            kwargs.pop("output_config", None)
            try:
                message = client.messages.create(**kwargs)
            except Exception as inner:  # noqa: BLE001
                raise RuntimeError(f"API call failed: {inner}") from inner
        else:
            raise RuntimeError(f"API call failed: {exc}") from exc

    if getattr(message, "stop_reason", None) == "refusal":
        raise RuntimeError("Claude declined to answer this request (stop_reason=refusal)")

    text = "".join(
        block.text
        for block in message.content
        if getattr(block, "type", "") == "text"
    ).strip()
    if getattr(message, "stop_reason", None) == "max_tokens":
        log.warning("Report hit MAX_TOKENS (%d) — it may be truncated.", MAX_TOKENS)
    usage = getattr(message, "usage", None)
    if usage is not None:
        log.info(
            "  Tokens: %s in / %s out",
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )
    return text


def _analyze_openai_compatible(prompt: str) -> str:
    """Call any OpenAI-compatible /chat/completions endpoint.

    Covers DeepSeek's own API, OpenRouter, Together, a local llama.cpp server,
    or a gateway. Requires LLM_BASE_URL unless you're hitting OpenAI itself.
    """
    if OpenAI is None:
        raise RuntimeError("openai package not installed. Run: pip install openai")

    client = OpenAI(
        api_key=LLM_API_KEY, **({"base_url": LLM_BASE_URL} if LLM_BASE_URL else {})
    )
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        _raise_known_api_error(exc)
        raise RuntimeError(f"API call failed: {exc}") from exc

    choice = (completion.choices or [None])[0]
    if choice is None:
        raise RuntimeError("Provider returned no choices — nothing to save.")
    if choice.finish_reason == "length":
        log.warning("Report hit MAX_TOKENS (%d) — it may be truncated.", MAX_TOKENS)
    usage = getattr(completion, "usage", None)
    if usage is not None:
        log.info(
            "  Tokens: %s in / %s out",
            getattr(usage, "prompt_tokens", "?"),
            getattr(usage, "completion_tokens", "?"),
        )
    return (choice.message.content or "").strip()


def _raise_known_api_error(exc: Exception) -> None:
    """Convert recognizable provider failures into one actionable line.

    Matches on class name and message text rather than importing each SDK's
    exception types, so it works across providers. Returns without raising when
    the error isn't one it recognizes.
    """
    name = type(exc).__name__
    text = str(exc)
    if name == "AuthenticationError" or "401" in text:
        raise RuntimeError(
            f"The provider rejected the API key (401) for {LLM_MODEL!r}. "
            "Check ANTHROPIC_API_KEY / LLM_API_KEY in .env."
        ) from exc
    if name == "PermissionDeniedError" or "403" in text:
        detail = " " + text.split(":", 1)[-1].strip() if ":" in text else ""
        raise RuntimeError(
            f"The API key is valid but not entitled to use {LLM_MODEL!r} (403)."
            f"{detail}"
        ) from exc
    if name in ("RateLimitError", "InternalServerError") or "429" in text:
        raise RuntimeError(f"Provider unavailable right now ({name}). Retry later.") from exc


# --------------------------------------------------------------------------- #
# 6. Persist
# --------------------------------------------------------------------------- #


def load_previous_snapshot() -> dict[str, Any] | None:
    """The most recent stored raw snapshot, for trend comparison."""
    files = sorted(RAW_DIR.glob("data_*.json"))
    for path in reversed(files):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("Skipping unreadable snapshot %s: %s", path.name, exc)
    return None


def save_results(data: dict[str, Any], analysis: str) -> dict[str, str]:
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    date_folder = now.strftime("%Y-%m-%d")
    time_only = now.strftime("%H%M%S")

    raw_path = RAW_DIR / f"data_{stamp}.json"
    raw_path.write_text(json.dumps(data, indent=2, default=str))

    day_dir = ANALYSES_DIR / date_folder
    day_dir.mkdir(parents=True, exist_ok=True)
    md_path = day_dir / f"analysis_{time_only}.md"
    md_path.write_text(render_markdown(data, analysis, now))

    log.info("  Raw data -> %s", raw_path)
    log.info("  Analysis -> %s", md_path)
    return {"raw": str(raw_path), "analysis": str(md_path)}


def render_markdown(data: dict[str, Any], analysis: str, when: datetime) -> str:
    acct = data.get("account", {})
    derived = data.get("derived", {})
    overall = derived.get("overall", {}) or {}
    header = [
        f"# Instagram Analysis — @{data.get('handle')}",
        "",
        f"**Generated:** {when.strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Lookback:** {data.get('days_lookback')} days  ",
        f"**Followers:** {acct.get('followers_count')}  ",
        f"**Posts analyzed:** {len(data.get('media', []))}  ",
        f"**Avg engagement rate:** {overall.get('avg_engagement_rate')}%  ",
        f"**Model:** {LLM_MODEL}",
        "",
    ]

    trends = data.get("trends")
    if trends:
        header.extend(
            [
                f"### Since the previous run ({trends.get('compared_with')})",
                "",
                "| Metric | Previous | Now | Change |",
                "| --- | --- | --- | --- |",
            ]
        )
        rows = [
            ("Followers", trends.get("followers")),
            ("Posts published", trends.get("media_count")),
            ("Avg engagement rate", trends.get("avg_engagement_rate")),
            ("Avg reach / post", trends.get("avg_reach_per_post")),
        ]
        for label, entry in rows:
            if not entry:
                continue
            change = entry.get("change")
            pct = entry.get("pct_change")
            change_text = "—"
            if change is not None:
                sign = "+" if change > 0 else ""
                change_text = f"{sign}{change}"
                if pct is not None:
                    change_text += f" ({sign}{pct}%)"
            header.append(
                f"| {label} | {entry.get('previous')} | {entry.get('current')} "
                f"| {change_text} |"
            )
        header.append("")

    token = data.get("token") or {}
    if isinstance(token.get("days_until_expiry"), int) and token["days_until_expiry"] <= 14:
        header.append(
            f"> ⏳ IG_TOKEN expires in {token['days_until_expiry']} day(s) "
            f"({token.get('expires_at')}) — re-exchange it."
        )
        header.append("")

    if data.get("collection_errors"):
        header.append("> ⚠️ Some data could not be collected:")
        header.extend(f">   - {e}" for e in data["collection_errors"])
        header.append("")

    header.extend(["---", "", analysis, ""])
    return "\n".join(header)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(args: argparse.Namespace) -> bool:
    log.info("=" * 60)
    log.info("Instagram Analyzer run started for @%s", ACCOUNT_HANDLE)

    if args.reanalyze:
        return _run_reanalysis(Path(args.reanalyze))

    analyzer = InstagramAnalyzer(days=args.days, limit=args.limit)
    missing = analyzer.missing_config(require_claude=not args.dry_run)
    if missing:
        log.error("Missing required config: %s. Check your .env.", ", ".join(missing))
        return False

    try:
        data = analyzer.collect_data()
    except Exception as exc:  # noqa: BLE001
        log.exception("Collection failed: %s", exc)
        return False

    if not data.get("media") and not data.get("account", {}).get("metrics"):
        log.error(
            "No usable Instagram data collected. Verify the account is a "
            "Business/Creator account and the token has insights permissions."
        )
        # Still continue: Claude can note the gaps. But warn loudly.

    if args.dry_run:
        print(json.dumps(data, indent=2, default=str))
        log.info("Dry run — no Claude call, nothing written.")
        return True

    try:
        analysis = analyze_with_claude(data)
        paths = save_results(data, analysis)
    except RuntimeError as exc:
        # Already a human-readable diagnosis — a traceback adds nothing.
        log.error("%s", exc)
        _offer_snapshot_fallback(data)
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("Run failed: %s", exc)
        return False

    log.info("Run complete.")
    log.info("  %s", paths["analysis"])
    print("\n" + "=" * 60)
    print(analysis)
    print("=" * 60)
    print(f"\nSaved: {paths['analysis']}")
    return True


def _offer_snapshot_fallback(data: dict[str, Any]) -> None:
    """Keep the collected data when only the Claude step failed.

    The Instagram half of the run is the slow, rate-limited half; losing it
    because of an API-key problem would mean re-collecting for nothing.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = RAW_DIR / f"data_{stamp}.json"
    raw_path.write_text(json.dumps(data, indent=2, default=str))
    log.info("Collected data was still saved -> %s", raw_path)
    log.info("Re-run the analysis once the key works, without re-fetching:")
    log.info("    python main.py --reanalyze %s", raw_path)


def _run_reanalysis(path: Path) -> bool:
    """Re-run Claude against a stored snapshot — no Instagram calls, no quota."""
    if not path.exists():
        log.error("Snapshot not found: %s", path)
        return False
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        log.error("Snapshot is not valid JSON (%s): %s", path, exc)
        return False

    log.info("Re-analyzing stored snapshot %s", path.name)
    # Older snapshots predate the derived block; compute it on the fly.
    if "derived" not in data:
        data["derived"] = derive_analytics(data)
    try:
        analysis = analyze_with_claude(data)
        paths = save_results(data, analysis)
    except Exception as exc:  # noqa: BLE001
        log.exception("Re-analysis failed: %s", exc)
        return False

    print("\n" + "=" * 60)
    print(analysis)
    print("=" * 60)
    print(f"\nSaved: {paths['analysis']}")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Instagram insights and generate a Claude report."
    )
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help=f"lookback window in days (default {DAYS_LOOKBACK})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=f"max posts to analyze (default {MEDIA_LIMIT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and print the dataset without calling Claude or writing files",
    )
    parser.add_argument(
        "--reanalyze",
        metavar="PATH",
        help="re-run the analysis on a stored data/raw/*.json snapshot",
    )
    return parser.parse_args(argv)


def main() -> int:
    return 0 if run(parse_args()) else 1


if __name__ == "__main__":
    sys.exit(main())
