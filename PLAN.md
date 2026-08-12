# Product Plan

Working document. Written 2026-08-11, against the state of the repo on that date.

---

## 1. The claim

Every feature below is scored against one sentence. If a feature doesn't serve
it, it doesn't get built.

> **Know what to post next — and whether last week's advice actually worked.**

Two things follow from that wording:

- **"what to post next"** means the output is an instruction, not a dashboard.
  Numbers are input; the deliverable is a decision.
- **"whether it worked"** means the system has to remember what it told you and
  grade itself. This is the only part a competitor can't rebuild in a weekend.

What we are explicitly *not* claiming: that the tool grows followers. It can
identify what correlates with reach and engagement on one account. Growth
depends on content, niche, timing and luck the analyzer never sees. Promising
growth is unsupportable and, in advertising terms, a liability.

---

## 2. Where we are today

Verified working as of 2026-08-11:

| Layer | State |
|---|---|
| Collection | Instagram Graph API via `graph.instagram.com`, 8 posts + insights + demographics, 0 errors |
| Aggregation | `derive_analytics()` — per format / weekday / hour, top+bottom posts, hashtags, cadence |
| Trends | `compute_trends()` — deltas vs the previous stored snapshot |
| Analysis | Provider-agnostic: Anthropic, any OpenAI-compatible endpoint, or a local CLI |
| Storage | Files on disk — `data/raw/*.json`, `data/analyses/<date>/*.md` |
| Web | Mobile-first dashboard, 3 views, tap-to-inspect charts |
| API | `/api/v1` — summary, runs, run detail, series. Bearer token, fails closed |
| Schedule | VPS cron, 00:30 UTC / 06:00 IST |
| Tests | 48, no network or keys required |

**Baseline to measure against** (@nsk.rides, 2026-08-11):

```
followers        154
posts (30d)      8      — 100% Reels, no format variety
avg ER           7.16%  (on reach)
avg reach/post   2,829
30d reach        20,931
30d profile views   108   ← 0.52% of reach. The actual bottleneck.
audience         94% India · 76% age 18–24 · 71% male
```

The profile-view conversion is the single most useful thing the tool has found
so far, and it is the kind of finding no single-post analysis can produce.

### Known gaps

- **The tool cannot see the content.** It reads captions and numbers. It cannot
  say the hook is weak or the first frame is dark.
- **Reports are prose.** Nothing is machine-checkable, so nothing can be graded.
- **Files are the database.** Every API read parses every snapshot; writes are
  not atomic.
- **Nothing serves on the VPS.** Only cron runs. The API is laptop-only.
- **Failure is silent.** If the CLI session or IG token lapses, cron fails and
  the UI shows stale data with no signal.

---

## 3. Features, by tier

### Tier 1 — no product without these

| # | Feature | Why | Effort |
|---|---|---|---|
| 1 | **Frames to the model** — thumbnails of top/bottom posts sent with the metrics | The only capability neither Instagram Insights nor a chat upload has | ~0.5 day |
| 2 | **Structured recommendations** — report emits checkable items | Prerequisite for #3. See §4 | ~1 day |
| 3 | **Did-it-work loop** — each run grades the previous run's advice | The moat; the only part that compounds | ~1 day |
| 4 | **Per-post drill-down** — one post, its frame, its numbers, why it landed | Where the user goes after "your cinematic reels win" | ~1 day |

### Tier 2 — makes it worth opening weekly

| # | Feature | Note |
|---|---|---|
| 5 | Best-time-to-post from own history | Data already collected; needs ~20+ posts to be honest |
| 6 | Alerts — beat/missed baseline, follower drop, cadence broken | The reason an app beats a bookmark |
| 7 | Comment analysis | Needs `instagram_business_manage_comments`. Thin at ~1 comment/post |
| 8 | Next-post brief — hook, format, time, caption angle | Natural output of Tier 1 |

### Tier 3 — later or never

Benchmarks vs similar accounts (Meta barely supports it), story metrics,
multi-account, caption A/B history.

### Not building

| Rejected | Reason |
|---|---|
| Auto-posting / scheduling | Huge scope, extra Meta permissions, crowded market |
| Delete underperformers | Irreversible; the premise ("cleaning the grid helps") is folklore |
| Follower prediction / growth guarantees | Unsupportable; claims liability |
| Generic AI caption generator | Commodity — every competitor ships one |

---

## 4. The one architectural decision

**Prose cannot be graded.** For run N+1 to evaluate run N, recommendations must
be structured data. This constrains everything after it, so it gets decided
first.

The model emits, alongside the markdown report:

```json
{
  "recommendations": [
    {
      "id": "2026-08-11-r1",
      "action": "Post a daylight POV ride reel on Wed around 12:00 IST",
      "rationale": "3 cinematic ride reels average 8.94% ER vs 7.18% overall",
      "metric": "engagement_rate",
      "baseline": 7.18,
      "target": 8.5,
      "horizon_days": 7,
      "evidence_posts": ["Da2pHq0yMsb"]
    }
  ]
}
```

Rules:

- `metric` must be one the collector already stores, so grading needs no new data
- `baseline` is captured at issue time — grading compares against that, not
  against a moving average
- Max 3 recommendations per run. More than that and none get followed
- Stored next to the snapshot as `data/raw/recs_<stamp>.json`

Grading on the next run produces one of: `followed_and_worked`,
`followed_no_change`, `not_followed`, `too_early`. "Not followed" is a real and
common outcome and must be reported honestly rather than quietly dropped —
otherwise the loop flatters itself.

**Implementation note:** request this via structured output (`output_config.format`
on Anthropic, `response_format` on OpenAI-compatible). The CLI provider cannot
guarantee JSON, so for `LLM_PROVIDER=cli` fall back to a fenced ```json block and
parse defensively.

---

## 5. Phases

### Phase 0 — make the current thing real (~1 day)

Not features. Without these there is nothing to build on.

- [ ] gunicorn + systemd unit on the VPS (auto-restart, survives reboot)
- [ ] Caddy + Let's Encrypt on `nsk.vps.cloudonfire.com`; close 5000, open 443
- [ ] Swap files for SQLite — kills O(n) reads and the torn-read race together
- [ ] `last_successful_run` + `stale` flag in `/api/v1/summary`; UI goes loud past 36h
- [ ] Non-root service user
- [ ] git remote; deploy becomes `git pull` instead of rsync

**Done when:** the API answers over HTTPS from a phone, and a failed cron is
visible in the UI without reading logs.

### Phase 1 — the differentiator (~2–3 days)

- [ ] Add `media_url`, `thumbnail_url`, `children{media_url}` to the media fetch
- [ ] `ANALYZE_IMAGES` + `IMAGE_SAMPLE` config; default top 5 + bottom 5
- [ ] Download frames at collection time (URLs are signed and expire in days)
- [ ] Pass frames as image blocks alongside the JSON; log added token cost per run
- [ ] Structured recommendations (§4) with a defensive parser
- [ ] Grading pass: load previous recs, evaluate, include the verdict in the report
- [ ] `/api/v1/runs/<id>/recommendations`

**Done when:** a report references what is visibly on screen ("your top 3 open on
a moving POV shot; your bottom 3 open on a static parked bike") **and** the next
run states whether the previous week's advice was followed and what moved.

**Cost note:** ~1,500 tokens per image. Ten images ≈ $0.05–0.08/run on a mid-tier
model. On the CLI provider it's zero but slower.

### Phase 2 — worth opening weekly (~3–4 days)

- [ ] Per-post drill-down view + `/api/v1/posts/<id>`
- [ ] Best-time-to-post (gated: hide until ≥20 posts, say so explicitly)
- [ ] Alerts + push
- [ ] Next-post brief

### Phase 3 — validate with real users (weeks, mostly not code)

Development-mode apps work for accounts added as Instagram Testers, with **no
App Review**. Meta caps the number — confirm the current limit.

- [ ] Onboard 5–10 real creators as testers, manually
- [ ] Run for 2–4 weeks
- [ ] Measure: do they open it? do they follow a recommendation? would they pay?

**This phase decides whether Phase 4 happens.** If ten people shrug, no amount
of multi-tenancy fixes it. Cost of finding out: near zero.

### Phase 4 — multi-user product (only if Phase 3 says yes)

- [ ] Instagram Business Login (OAuth) replacing hand-generated tokens
- [ ] **Meta App Review** + business verification — 4–8 weeks, expect a rejection
      cycle. Start before building UI
- [ ] Privacy policy, terms, data-deletion callback
- [ ] Users table; per-user encrypted tokens; 60-day refresh worker
- [ ] Per-user job queue replacing the single cron
- [ ] Real LLM API billing — a personal CLI plan cannot serve customers
- [ ] Rate-limit strategy: ~35 Graph calls per analysis, shared per-app budget
- [ ] Billing

---

## 6. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Meta App Review rejection | Blocks all of Phase 4 | Start early; concrete permission justification; screencast the real flow |
| Only Business/Creator accounts have insights | Shrinks addressable market | State it in onboarding; detect and explain rather than failing silently |
| Thin reports for new users | Worst experience at the worst moment | Gate claims on sample size; say "not enough data" explicitly |
| Provider/CLI session expiry | Silent cron failure | Phase 0 staleness flag; keep an API key configured as fallback |
| Category is crowded | Differentiation erodes | Frames + history is the wedge; ship #1–3 before anything cosmetic |
| Holding users' IG tokens | Legal exposure under GDPR / DPDP | Encrypt at rest, documented retention, deletion endpoint — Phase 4 gate |

---

## 7. Open decisions

1. **Personal tool or product?** If personal: do Phase 0 + 1 and stop. Phases 3–4
   only make sense for a product.
2. **Grading horizon** — 7 days is the assumption. With 2 posts/week that is 2
   data points per recommendation. May need 14.
3. **Frames: thumbnails or multiple stills per reel?** Cover frame is cheap and
   probably enough to start. Sampling 3 frames per reel triples image cost.
4. **Does the CLI provider stay?** Free and works today; can't serve customers
   and is the most likely silent failure. Fine for Phases 0–2.

---

## 8. Success criteria

**Phase 1 (the honest test):** a report that says something specific about
on-screen content that the previous prose version could not, *and* a second run
that correctly grades the first. Judged on one account — this one.

**Phase 3:** at least 3 of 10 testers follow a recommendation unprompted, and at
least 1 asks what it would cost. Anything less means the claim in §1 is wrong.
