# Instagram Analyzer — Complete Guide

Automated daily analysis of an Instagram account using the Instagram Graph API +
Claude. Replaces manual weekly review: runs on your VPS on a schedule, stores
every analysis by date, and keeps the raw data for re-analysis.

---

## What it does

Each run:

1. Connects to the Instagram Graph API
2. Pulls account metrics over the lookback window (default 30 days)
3. Retrieves recent posts with engagement data (reach, views, saves, shares)
4. Gets audience demographics (when available)
5. Computes aggregates locally — by format, weekday, hour, hashtag, cadence
6. Diffs the headline numbers against the previous stored run
7. Sends the complete dataset to Claude
8. Claude produces a tactical report
9. Stores the report and raw data, organized by date

**Output per run:**

- Top 3 content gaps
- Engagement patterns (what works)
- Posting strategy (timing, format, captions)
- 30-day growth roadmap

---

## ⚠️ Important: which Instagram API

The insights this tool needs are only available through the **Instagram Graph
API**, which requires:

- an Instagram **Business or Creator** account,
- linked to a **Facebook Page**,
- with tokens generated from **developers.facebook.com** (not
  `instagram.com/apps`).

The old **Instagram Basic Display API was shut down in December 2024** and never
returned insights anyway. If you see empty metrics, this is almost always the
cause: the account isn't a linked Business/Creator account, or the token lacks
insights permissions.

---

## Setup

### Prerequisites

- VPS or machine with Python 3.9+ (`zoneinfo`)
- Instagram Business/Creator account linked to a Facebook Page
- Anthropic API key

### Step 1 — Get your tokens

**Instagram Graph API token**

1. Go to <https://developers.facebook.com> and create (or open) an app.
2. Add the **Instagram Graph API** product and link your Facebook Page +
   Instagram Business account.
3. In the Graph API Explorer, request the permissions:
   `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement`,
   `pages_show_list`.
4. Generate a token, then exchange it for a **long-lived** token (60 days):

   ```bash
   curl "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_LIVED_TOKEN"
   ```

   Put the result in `.env` as `IG_TOKEN`. Re-run periodically to refresh, or
   automate refresh with a scheduled job.

**Your Instagram Business account ID (numeric)**

```bash
# 1. Find your Page(s):
curl "https://graph.facebook.com/v21.0/me/accounts?access_token=$IG_TOKEN"
# 2. Get the linked IG business account for a page:
curl "https://graph.facebook.com/v21.0/PAGE_ID?fields=instagram_business_account&access_token=$IG_TOKEN"
```

Copy `instagram_business_account.id` into `.env` as `IG_USER_ID`.

**Claude API key**

Create one at <https://console.anthropic.com> and set `ANTHROPIC_API_KEY`.

### Step 2 — Deploy

```bash
ssh user@your_vps_ip
git clone <your_repo_url> ig-analyzer
cd ig-analyzer
chmod +x setup.sh cron-setup.sh main.py query.py
./setup.sh
```

### Step 3 — Configure

```bash
nano .env
```

```
IG_TOKEN=your_long_lived_token
IG_USER_ID=your_numeric_business_account_id
IG_HANDLE=your_handle
IG_API_BASE=https://graph.facebook.com/v21.0
IG_TIMEZONE=Asia/Kolkata
ANTHROPIC_API_KEY=your_api_key
CLAUDE_MODEL=claude-opus-5
CLAUDE_EFFORT=high
MAX_TOKENS=8000
LOG_LEVEL=INFO
DAYS_LOOKBACK=30
MEDIA_LIMIT=30
HTTP_RETRIES=3
```

`IG_TIMEZONE` matters: the Graph API reports post times in UTC, and any
"post at 18:00" recommendation is wrong unless it's stated in your audience's
local time. Set it to the IANA zone your audience actually lives in.

### Step 4 — Test

```bash
source venv/bin/activate
python main.py --dry-run     # collection only — no Claude call, no files written
python main.py               # the real thing
```

Start with `--dry-run`: it prints the full collected dataset as JSON so you can
confirm the token and permissions work before spending Claude credits. Then the
full run shows data-collection status, the Claude analysis, and the paths where
results were saved. If any metric can't be collected, the run continues and the
report notes what was missing.

### Step 5 — Schedule

```bash
./cron-setup.sh daily     # or: hourly | weekly
crontab -e                # paste the printed line
```

Example (daily at 6 AM):

```
0 6 * * * cd /home/user/ig-analyzer && /home/user/ig-analyzer/venv/bin/python main.py >> data/logs/cron.log 2>&1
```

---

## Accessing results

### CLI

```bash
python query.py --latest              # most recent
python query.py --list                # all
python query.py --days 7              # last 7 days
python query.py --search reels        # grep across every report
python query.py --stats               # follower + engagement history per run
python query.py --file analysis_060000.md
```

### File system

```
data/analyses/2025-01-15/analysis_060000.md
data/raw/data_20250115_060000.json
data/logs/analyzer.log
data/logs/cron.log
```

### Web dashboard (optional)

```bash
source venv/bin/activate
python dashboard.py
# visit http://127.0.0.1:5000
```

The dashboard binds to `127.0.0.1` by default. If you expose it publicly, put it
behind a reverse proxy with authentication and/or restrict the port with your
firewall — reports contain private account data.

---

## Project structure

```
ig-analyzer/
├── main.py              # Core pipeline
├── query.py            # CLI for browsing analyses
├── dashboard.py        # Optional Flask UI
├── setup.sh            # Environment setup
├── cron-setup.sh       # Prints a crontab line
├── tests/              # Unit tests (no network, no keys)
├── requirements.txt
├── .env.example        # Config template
├── .env                # Your tokens (gitignored)
├── .gitignore
├── templates/          # Dashboard HTML
└── data/
    ├── logs/           # analyzer.log + cron.log
    ├── analyses/       # Reports by date: YYYY-MM-DD/analysis_HHMMSS.md
    └── raw/            # Raw JSON: data_YYYYMMDD_HHMMSS.json
```

---

## How `main.py` works

`InstagramAnalyzer` methods:

- `check_token()` — introspects the token and warns when it's within a week of
  expiring, so a 60-day expiry doesn't silently kill the cron job.
- `fetch_account_insights()` — followers/media counts plus time-series metrics
  (`reach`, `profile_views`, `accounts_engaged`, `total_interactions`). Retries
  metrics individually if a batch request is rejected, since the Graph API
  deprecates metric names periodically (`impressions` was removed in 2024).
  The lookback is requested in <=30-day chunks and summed, because the API
  rejects longer spans.
- `fetch_media()` — recent posts with likes/comments plus per-post `reach`,
  `views`, `saved`, `shares`, and a computed engagement rate. Paginates with the
  `after` cursor and stops at the lookback cutoff. Which insight metrics exist
  depends on the media type, so the supported set is probed once per type and
  reused — one request per post thereafter instead of five.
- `fetch_audience_insights()` — follower demographics by country/age/gender
  (needs 100+ followers; otherwise records a warning and continues).
- `collect_data()` — aggregates the above, derives local analytics, attaches the
  trend diff, and captures any collection errors.

Module-level functions (pure, and unit-tested in `tests/`):

- `derive_analytics(data)` — per-format / weekday / hour breakdowns, top and
  bottom posts, caption and hashtag stats, posting cadence.
- `compute_trends(current, previous)` — deltas against the previous snapshot.
- `build_prompt(data)` — the tactical prompt, including how to read the derived
  block and which caveats apply to this particular run.
- `analyze_with_claude(data)` — calls Claude with adaptive thinking and the
  configured effort, retrying without those parameters on models that reject
  them, and raises on a refusal instead of returning an empty report.
- `save_results()` / `render_markdown()` — writes raw JSON and a markdown report
  with a trend table in the header.
- `run()` — orchestrates the pipeline, validates config, logs each step.

Every fetch is defensive: a failure is logged into `collection_errors` and the
run continues, so a single deprecated metric never sinks the whole analysis.
Throttled (HTTP 429, Graph error codes 4/17/32) and 5xx responses are retried
with exponential backoff up to `HTTP_RETRIES` times.

### Re-analyzing without touching Instagram

Every run stores its full dataset in `data/raw/`. To try a different prompt,
model, or effort level against data you already have:

```bash
python main.py --reanalyze data/raw/data_20250115_060000.json
```

This calls Claude only — no Graph API requests, no rate-limit cost.

---

## Data flow

```
Instagram Graph API
      │  fetch_account_insights / fetch_media / fetch_audience_insights
      ▼
  collect_data()
      │  derive_analytics()      local aggregates (format/time/hashtag/cadence)
      │  compute_trends()  ◄──── previous data/raw/*.json snapshot
      ▼
  analyze_with_claude()  ──►  Claude
      │
      ▼
  save_results()  ──►  data/raw/data_TIMESTAMP.json
                  └─►  data/analyses/YYYY-MM-DD/analysis_TIMESTAMP.md
      │
      ▼
  query.py  /  dashboard.py
```

---

## Troubleshooting

**Empty metrics / "No usable Instagram data"**
- Confirm the account is a Business/Creator account linked to a Facebook Page.
- Confirm `IG_USER_ID` is the numeric *instagram_business_account* id.
- Confirm the token has `instagram_manage_insights`.
- Test directly:
  ```bash
  curl "https://graph.facebook.com/v21.0/$IG_USER_ID/media?fields=id,caption,like_count&access_token=$IG_TOKEN"
  ```

**Demographics unavailable** — normal below ~100 followers; the report notes it.

**Reach looks too high over long lookbacks** — with `DAYS_LOOKBACK` above 30 the
account metrics are summed across 30-day request windows, so `reach` counts
someone reached in two windows twice. The prompt tells Claude this; the raw JSON
flags it as `account.metrics_are_window_sums`.

**"Claude API call failed"** — check `ANTHROPIC_API_KEY`, remaining credits, and
network access from the VPS.

**Token expired** — long-lived tokens last ~60 days. Each run introspects the
token and logs a warning (plus a banner in the report) once it's within a week of
expiry. Re-exchange it (see Step 1) or automate refresh.

**Rate limited (error code 4, 17 or 32)** — the run backs off and retries
`HTTP_RETRIES` times. If it persists, lower `MEDIA_LIMIT` or run less often;
per-post insight calls are the bulk of the request volume.

**Timing advice looks wrong** — set `IG_TIMEZONE` to your audience's IANA zone.
It defaults to UTC, and the report's weekday/hour buckets follow it.

**Cron not running**
```bash
systemctl status cron        # or: crond
crontab -l
tail -f data/logs/cron.log
```

---

## Cost

- Instagram Graph API: free.
- Claude: depends on `CLAUDE_MODEL` and `CLAUDE_EFFORT`. The default
  (`claude-opus-5` at `high`) is roughly $0.10–0.20/run; `claude-sonnet-5`
  ≈ $0.03–0.05/run; `claude-haiku-4-5` ≈ $0.01/run. Lowering `CLAUDE_EFFORT` to
  `medium` cuts thinking tokens on models that support it. Daily on Sonnet
  ≈ $1–1.50/month.
- `--dry-run` and `python -m pytest tests/` cost nothing — neither calls Claude.
- VPS: your existing cost.

---

## Security

- `.env` holds secrets and is gitignored — never commit it.
- Rotate the IG token and API key periodically.
- Keep the dashboard off the public internet unless it's behind auth.
- Reports and raw JSON contain private account data; `data/` is gitignored.

---

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -q
```

The suite covers the pure logic — timestamp parsing, the derived aggregates,
trend deltas, insight windowing, report rendering, and the dashboard's path and
HTML-escaping guards. It needs no network access and no API keys, so it is safe
to run in CI or a pre-commit hook.

---

## Extending

Edit `build_prompt()` in `main.py` to change what Claude focuses on, and
`derive_analytics()` to change what it gets to reason over — a metric computed
there is exact, whereas one Claude has to derive from the raw post list may not
be. Iterate against stored data with `--reanalyze` instead of burning Graph API
quota.

Natural additions: competitor comparison, Slack notifications, a weekly digest
that synthesizes the last 7 daily reports, and story-level metrics (`replies`,
`navigation`) for accounts that post stories.
