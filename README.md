# Instagram Analyzer

Automated daily analysis of an Instagram account's performance using the
Instagram Graph API + Claude. Pulls account, post, and audience insights,
computes the aggregates locally, diffs them against the previous run, sends the
lot to Claude, and stores a tactical markdown report per run — no manual
screenshot uploads.

## Quick start

```bash
git clone <your_repo_url> ig-analyzer
cd ig-analyzer
chmod +x setup.sh cron-setup.sh main.py query.py
./setup.sh                 # venv + deps + data dirs + .env scaffold
nano .env                  # add your tokens
source venv/bin/activate
python main.py --dry-run   # verify collection without spending Claude credits
python main.py             # full run
```

Then schedule it:

```bash
./cron-setup.sh daily      # prints a crontab line; paste into `crontab -e`
```

## What you need

- Python 3.9+
- An **Instagram Business or Creator account linked to a Facebook Page**
  (required — insights are not available on personal/Basic-Display accounts).
- A long-lived Graph API token with `instagram_basic`,
  `instagram_manage_insights`, and `pages_read_engagement`.
- An Anthropic API key.

See [PROJECT_GUIDE.md](PROJECT_GUIDE.md) for full token setup, the data flow,
troubleshooting, and how to extend the analysis.

## Running it

```bash
python main.py                    # collect → analyze → store
python main.py --dry-run          # collect + print JSON, no Claude call, no writes
python main.py --days 7 --limit 50
python main.py --reanalyze data/raw/data_20250115_060000.json   # no IG calls
```

`--reanalyze` re-runs Claude against a stored snapshot, so you can iterate on the
prompt or try a different model without touching your Instagram rate limit.

## Reading results

```bash
python query.py --latest     # most recent report
python query.py --list       # all reports
python query.py --days 7     # last 7 days
python query.py --search reels   # grep across every report
python query.py --stats      # follower + engagement history across runs
python query.py --file analysis_060000.md
```

Or the optional web dashboard:

```bash
python dashboard.py          # http://127.0.0.1:5000 (reports + /trends)
```

## What Claude actually sees

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

## Layout

```
main.py          Core pipeline: collect → derive → analyze → store
query.py         CLI to browse stored analyses
dashboard.py     Optional Flask UI
tests/           Unit tests (no network)
setup.sh         One-shot environment setup
cron-setup.sh    Prints a crontab line
data/            Auto-created: logs/, raw/ (JSON), analyses/YYYY-MM-DD/
```

## Cost

Instagram Graph API is free. Claude cost depends on `CLAUDE_MODEL` and
`CLAUDE_EFFORT` in `.env` (default `claude-opus-5` at `high` effort, roughly
$0.10–0.20 per daily run). Lower `CLAUDE_EFFORT` to `medium`, or switch
`CLAUDE_MODEL` to `claude-sonnet-5` or `claude-haiku-4-5`, to cut it — the run
automatically retries without the thinking/effort parameters on models that
don't accept them.
