# Refresh timer

Cloudflare Cron Triggers call `worker.mjs`, which checks GitHub and dispatches
`.github/workflows/update.yml`. The Worker never contacts the golf site.
GitHub's native schedule is removed so only one timer owns the cadence.

## Cadence (UTC)

- `*/20 10-23 * * *`: freshness-driven `auto`, 42 ticks per day.
- `25 8 * * *`: one `daily` refresh overnight.

During Eastern daylight time that is 6:00 a.m.–7:40 p.m. every 20 minutes,
plus 4:25 a.m. overnight. Standard time is one hour earlier. This preserves
the original UTC schedule.

## Safeguards

- No public HTTP endpoint: workers.dev and preview URLs are disabled; only a
  scheduled handler exists.
- The Worker skips ticks over five minutes old, queued/active updates, and
  updates created within the last 18 minutes. There are no dispatch retries or
  catch-up loops. GitHub check failures stop the dispatch.
- Actions uses the existing `update-mirror` concurrency group. Inside that
  lock, `guard.py` checks the actual Crawl steps of recent runs, skipping a
  crawl started within 18 minutes or finished within 60 seconds. This protects
  against concurrent duplicate dispatches, manual runs and workflow reruns.
  Automated ticks over ten minutes old are skipped even if runner startup
  was delayed. Guard API failures stop the job before crawling.
- The 18-minute duplicate window leaves room for runner-start jitter within
  the 20-minute schedule. Skipped ticks wait for the next scheduled tick.
- All source fetches use the existing shared, sequential Fetcher with a
  minimum ten-second delay. Routine modes allow at most 60 attempts including
  retries; an explicitly requested `full` backfill allows 300. No parallel
  fetching is used. Nationals normally adds five requests, one per flight,
  with occasional extra discovery/regular-tour requests when stale.
- HTTP 403, 429 or 503 stops upstream fetching for the entire run. Other
  network/server failures get at most three paced attempts, then also stop
  further fetching. Missing pages are not retried.
- Cloudflare cannot request `full` mode or force freshness bypasses.
- Automatic refreshes deploy Pages only when content changes. Manual runs
  retain the redeploy escape hatch. Skipped duplicate runs do neither.

## Credential and deployment

Create a GitHub fine-grained token restricted to `millerbennett/bettergolfweekam`
with **Actions: read and write** and the mandatory **Metadata: read** permission.
It cannot edit repository files. Actions write can operate workflows in this
repository; GitHub does not offer a dispatch-only permission.
Store it only as the Worker's encrypted `GITHUB_TOKEN` secret, never in Git,
Worker variables, command-line arguments, or logs. Renew it before expiration.

```powershell
# Deploy disabled first, then enter the secret interactively.
npx wrangler@4.151.1 deploy --config scheduler/wrangler.jsonc --var ENABLED:false
npx wrangler@4.151.1 secret put GITHUB_TOKEN --config scheduler/wrangler.jsonc
# Publish the workflow changes removing GitHub cron before enabling this timer.
npx wrangler@4.151.1 deploy --config scheduler/wrangler.jsonc
```

For an immediate pause, redeploy with `--var ENABLED:false`. To remove the
timer altogether, clear `triggers.crons` and deploy. Do not re-enable GitHub
cron while Cloudflare's timer is enabled. Trigger configuration changes can
take up to 15 minutes to propagate.

## Verification

```powershell
node --test scheduler/worker.test.mjs
python -m pytest tests/ -q
npx wrangler@4.151.1 deploy --config scheduler/wrangler.jsonc --dry-run
```

These tests are offline and cover duplicate/overlap suppression, stale ticks,
API failure handling, retry/request budgets, and source overload responses.
For production, inspect Cloudflare's invocation logs for `dispatched`/skip
outcomes and GitHub Actions runs named `Cloudflare refresh (auto)` or
`Cloudflare refresh (daily)`. Check the Crawl step's request count and the
deploy result. A successful dispatch alone does not prove a score refresh.

The `fetched_at` timestamps in snapshots mean content last changed, not last
checked. Workflow logs and cached `data/meta.json` contain the check time and
request count even when the scores have not changed.
