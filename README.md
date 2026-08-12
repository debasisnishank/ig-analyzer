# Instagram Analyzer

Automated daily analysis of an Instagram account's performance. Pulls account,
post, and audience insights from the Instagram Graph API, computes the
aggregates locally, diffs them against the previous run, sends the lot to an LLM,
and stores a tactical markdown report per run — no manual screenshot uploads.

Two halves that share only the `data/` directory:

| | | |
|---|---|---|
| **Backend** | `main.py` | Batch pipeline. Collects, analyzes, writes to disk. Runs on cron. |
| **Frontend** | `dashboard.py` | Flask app. Mobile-first HTML dashboard **and** the JSON API at `/api/v1`. |

Neither imports the other, so the backend runs headless on a server and the
frontend can be started, stopped, or replaced (by a mobile app) independently.

## Quick start

```bash
git clone <your_repo_url> ig-analyzer
cd ig-analyzer
chmod +x setup.sh cron-setup.sh main.py query.py
./setup.sh                 # venv + deps + data dirs + .env scaffold
nano .env                  # add your tokens
source venv/bin/activate

python main.py --dry-run   # backend: verify collection, no model call, no cost
python main.py             # backend: full run — writes a report
python dashboard.py        # frontend: http://127.0.0.1:5000
```

Then schedule the backend:

```bash
./cron-setup.sh daily      # prints a crontab line; paste into `crontab -e`
```

## What you need

- Python 3.9+
- An **Instagram Business or Creator account** — insights are not available on
  personal accounts.
- A Graph API token. Two routes:
  - **Instagram Login** (`graph.instagram.com`) — no Facebook Page needed,
    token refreshable indefinitely. Permissions: `instagram_business_basic`,
    `instagram_business_manage_insights`.
  - **Facebook Login** (`graph.facebook.com`) — needs the account linked to a
    Facebook Page, but adds post deletion. Permissions: `instagram_basic`,
    `instagram_manage_insights`, `pages_read_engagement`, `pages_show_list`.
- A way to run the analysis: an Anthropic key, any OpenAI-compatible key
  (DeepSeek, OpenRouter, …), or a local coding-agent CLI. See `.env.example`.

See [PROJECT_GUIDE.md](PROJECT_GUIDE.md) for full token setup, the data flow,
troubleshooting, and how to extend the analysis.

## Backend — the analysis pipeline

Collects from Instagram, derives the aggregates, calls the model, writes a report
and a snapshot to `data/`. No server, no port — it runs and exits.

```bash
source venv/bin/activate

python main.py                    # full run: collect → analyze → store
python main.py --dry-run          # collect + print JSON. No model call, nothing written
python main.py --days 7 --limit 50
python main.py --reanalyze data/raw/data_20250115_060000.json
```

`--dry-run` is the one to use when checking credentials — it makes no LLM call,
so it costs nothing. `--reanalyze` re-runs the analysis against a stored snapshot,
so you can iterate on the prompt or swap models without spending Instagram rate
limit.

Schedule it:

```bash
./cron-setup.sh daily      # prints a crontab line; paste into `crontab -e`
```

Read results without a browser:

```bash
python query.py --latest         # most recent report
python query.py --list           # all reports
python query.py --days 7         # last 7 days
python query.py --search reels   # grep across every report
python query.py --stats          # follower + engagement history across runs
python query.py --file analysis_060000.md
```

## Frontend — dashboard and API

One Flask app serves both the HTML views and the JSON API.

```bash
source venv/bin/activate
python dashboard.py              # http://127.0.0.1:5000
```

| Route | What |
|---|---|
| `/` | Latest report, stat tiles, run selector |
| `/trends` | Charts over time + full run table |
| `/analysis/<date>/<file>` | One stored report |
| `/download/<date>/<file>` | Raw JSON behind a report |
| `/api/v1/…` | JSON API — see below |

Binding and port:

```bash
# Default: localhost only
python dashboard.py

# Reachable from your phone on the same wifi
DASHBOARD_HOST=0.0.0.0 DASHBOARD_PORT=5000 python dashboard.py
```

⚠️ `0.0.0.0` exposes reports to everyone on the network, with no authentication
on the HTML views. Fine on home wifi; use an SSH tunnel otherwise:

```bash
ssh -L 5000:127.0.0.1:5000 user@your-server
```

Stop it with `Ctrl+C`, or:

```bash
pkill -f dashboard.py
```

Flask's built-in server is for development. On a server, run it under gunicorn
with a systemd unit and TLS in front — see `PLAN.md` Phase 0.

## What the model actually sees

Raw API output alone makes for vague reports, so each run also computes and sends:

- **Per-format breakdown** — engagement rate, reach, views, saves, shares by
  `REELS` / `FEED` / etc.
- **Timing** — posts bucketed by local weekday and hour (`IG_TIMEZONE`), because
  the API returns UTC and timing advice in UTC is useless.
- **Top and bottom 5 posts** with permalinks and caption previews.
- **Caption shape** — length, hashtag counts, and a split at the median hashtag
  count so "use more hashtags" has to survive contact with the data.
- **Hashtag leaderboard** — average engagement rate per tag used 2+ times.
- **Cadence** — posts per week, average gap, longest silence.
- **Trends** — followers, engagement rate, and reach diffed against the previous
  stored run, so the report can say whether last period's advice worked.

## Tests

```bash
python -m pytest tests/ -q
```

No network or API keys needed — the suite covers the aggregation, trend, prompt,
rendering, and dashboard path-safety logic.

## JSON API

The HTML dashboard and the JSON API are two readers over the same data layer
(`store.py`), so a mobile app gets exactly what the web UI shows. Set `API_TOKEN`
in `.env` to enable it — while it's unset every endpoint returns 503 rather than
serving your metrics unauthenticated.

```bash
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:5000/api/v1/summary
```

| Endpoint | Returns |
|---|---|
| `GET /api/v1/health` | Liveness. No token, no account data. |
| `GET /api/v1/summary` | Latest values, deltas and ranges for all four metrics |
| `GET /api/v1/runs?limit=N` | Runs, newest first |
| `GET /api/v1/runs/<id>` | One run: report markdown, metrics, trends, audience |
| `GET /api/v1/series?metric=followers` | One metric over time, for charting |

Run ids look like `2026-08-11_225933`. Reports come back as **markdown**, not
HTML — every mobile toolkit renders it, and it keeps presentation out of the API.

The API is read-only on purpose: a collection plus analysis takes about two
minutes, so a phone should never be blocked on one. Cron produces runs; the API
serves what is already stored.

## Layout

```
main.py          Core pipeline: collect → derive → analyze → store
store.py         Shared read layer (used by both readers below)
api.py           JSON API at /api/v1 (token auth)
dashboard.py     Flask app: HTML dashboard + mounts the API
query.py         CLI to browse stored analyses
tests/           Unit tests (no network)
setup.sh         One-shot environment setup
cron-setup.sh    Prints a crontab line
data/            Auto-created: logs/, raw/ (JSON), analyses/YYYY-MM-DD/
```

## Cost

Instagram Graph API is free. The analysis step depends on which provider you point at:

| Provider | `.env` | Rough cost per run |
|---|---|---|
| Local coding-agent CLI | `LLM_PROVIDER=cli` | **$0** — uses a CLI subscription. Slower (~100s), needs the CLI logged in |
| DeepSeek / OpenRouter | `LLM_PROVIDER=openai` + `LLM_BASE_URL` | ~$0.01 |
| Claude Haiku | `CLAUDE_MODEL=claude-haiku-4-5` | ~$0.01 |
| Claude Opus | `CLAUDE_MODEL=claude-opus-5` | ~$0.10–0.20 |

A report is roughly 4.5k input / 2k output tokens, so daily runs stay cheap on
any provider. `--dry-run` and `python -m pytest tests/` cost nothing — neither
calls a model.
