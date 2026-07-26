# bettergolfweekam

A personal, read-only mirror of the [Washington DC Metro Golfweek Amateur Tour](https://amateurgolftour.net/dc_tour_pages/default.aspx)
pages — schedule, tee times, live scores, results and the points race — rendered
as a fast static site so you don't have to hunt through ASP.NET dropdowns to
find out when you tee off.

GitHub Actions crawls the upstream site on a schedule and commits JSON snapshots
to `data/`. When that data actually moves, the site is re-rendered and pushed to
Cloudflare Pages. Everything stays inside both free tiers.

## Why it exists

The upstream site is an ASP.NET WebForms app where each view (tee times,
results, leaderboard) is a `<select>` that posts back the whole page. Finding
your Saturday tee time means loading a page, picking the right entry out of an
18-item dropdown, waiting for a postback, then scanning a 50-row table for your
name. This mirror turns that into one URL that always shows the next thing you
care about.

It also exists so a scheduled assistant check (ChatGPT tasks, a cron job, an RSS
reader) can watch the tour for you — see [For scheduled checks](#for-scheduled-checks).

## What it mirrors

| Upstream page | Method | Lands in |
|---|---|---|
| `Schedule.aspx` | GET | `data/schedule.json` |
| `Pairings.aspx` | POST (`tournament_dd`) | `data/pairings/<tid>.json` |
| `results.aspx?id=` | GET | `data/results/<tid>.json` |
| `listing.aspx?id=` | GET | `data/roster/<tid>.json` |
| `Standings.aspx` | GET | `data/standings.json` |
| `livescore/Leaderboard.aspx` | POST (`tournaments_dd`) | `data/live/<tid>.json` |
| `readContent.aspx?id=` | GET | `data/content/<id>.json` |

Two things worth knowing about the upstream site, both handled in `scraper/`:

- **Tournament ids are shared across tour pages and the livescore app.** The id
  in a `results.aspx?id=` link is the same id the leaderboard's dropdown uses.
  No cross-referencing needed.
- **The leaderboard dropdown only carries a rolling window of current events.**
  Posting an id outside that window trips ASP.NET's `__EVENTVALIDATION` and
  returns HTTP 500, so `fetch_livescore` checks the options before submitting.

`robots.txt` on the origin asks for `Crawl-Delay: 10`, and the scraper honours it
globally, so crawls are paced in minutes rather than seconds.

## Layout

```
config.json              tour, season, your player id, freshness thresholds
scraper/
  net.py                 rate-limited session + WebForms __VIEWSTATE helper
  parse.py               table extraction
  sources.py             one function per upstream page
  store.py               JSON snapshots, freshness stamps, change feed
  crawl.py               orchestrator; decides what is stale and fetches it
build/
  render.py              data/*.json -> public/
  templates/, static/
data/                    committed JSON snapshots (the mirror's source of truth)
public/                  generated site (gitignored; built in CI)
```

## Running locally

```bash
pip install -r requirements.txt

python -m scraper.crawl --mode full     # first run: backfill the whole season
python -m build.render                  # writes public/
python -m http.server -d public 8000
```

Modes:

| Mode | Does |
|---|---|
| `auto` | What cron runs. Fetches only what `data/state.json` says is stale — usually 0–1 requests. |
| `daily` | Forces schedule, standings and announcement pages. |
| `live` | Only polls the leaderboard for events happening today. |
| `full` | Backfills every event's pairings, plus rosters for upcoming events and results for past ones. ~10 minutes at the 10s crawl delay. |

Add `--force` to ignore freshness stamps.

## How the schedule stays cheap

`--mode auto` reads `config.json` freshness thresholds and the schedule, then
fetches only what's due:

- **Tee times** — polled every 45 minutes in the 5 days before an event, then
  left alone once they're published. This is the case the mirror exists for.
- **Rosters** — who's in the field and how much room is left, every 6 hours for
  events up to 45 days out. Wider than the tee-time window, since that's when
  you'd still decide to enter.
- **Live scores** — polled on event days only.
- **Results** — polled every 3 hours for 10 days after an event, until posted.
- **Schedule / standings** — every 12 hours. **Announcements** — every 24 hours.

So a run on a quiet Tuesday makes no upstream requests at all and exits in
seconds. Deploys are gated on `git diff` over `data/`, which keeps Cloudflare
Pages well under its 500-deploys/month free limit even during a tournament.

`data/state.json` (the freshness stamps) and `data/meta.json` are gitignored and
persisted through the Actions cache instead. Committing them would mean roughly
40 commits a day from the cron alone; keeping them out means a commit appears
only when mirrored content genuinely moved. A cache miss costs one slightly
larger crawl, nothing worse.

## Setup

### 1. GitHub

Push to a **public** repo — Actions minutes are unlimited there, and nothing in
this project is secret. Then add under *Settings → Secrets and variables →
Actions*:

| Kind | Name | Value |
|---|---|---|
| Secret | `CLOUDFLARE_API_TOKEN` | Token with the **Cloudflare Pages: Edit** permission |
| Secret | `CLOUDFLARE_ACCOUNT_ID` | From the Cloudflare dashboard sidebar |
| Variable | `SITE_URL` | e.g. `https://bettergolfweekam.pages.dev` |
| Variable | `CF_PAGES_PROJECT` | Your Pages project name |

Also enable *Settings → Actions → General → Workflow permissions →
**Read and write***, so the workflow can commit refreshed data.

### 2. Cloudflare Pages

Create a Pages project with **Direct Upload** (not the Git integration — CI
uploads the rendered `public/` itself, so Cloudflare never needs to build
anything, and data commits don't trigger builds):

```bash
npx wrangler pages project create bettergolfweekam --production-branch main
```

### 3. Seed the data

Run the *Update mirror* workflow manually once with mode `full` to backfill the
season, or run it locally and commit `data/`.

### 4. Point it at you

Edit `config.json`:

```json
"player": { "id": "51002", "name": "Miller, Bennett", "flight": "B" }
```

The `name` must match the upstream `"Last, First"` spelling exactly — the live
leaderboard identifies players by name, not id. Your id appears in the
[standings table](https://amateurgolftour.net/dc_tour_pages/Standings.aspx).

To mirror a different tour, change `tour_slug` and `tour_name` and refresh
`content_pages` with that tour's `readContent.aspx` ids.

## For scheduled checks

Three endpoints exist specifically so an assistant or script can watch the tour
without parsing HTML:

- **`/digest.txt`** — a plain-text briefing: next event, whether you're
  registered, how full the field is, whether tee times are out, your tee time
  and playing partners, your points position, last result, recent changes.
  Cheapest thing to read.
- **`/status.json`** — the same information structured.
- **`/feed.xml`** — RSS of every change the mirror has noticed.

A ChatGPT scheduled task prompt that works well:

> Fetch https://YOUR-SITE/digest.txt. If tee times for the next event have been
> posted since your last check, tell me my tee time, starting hole and playing
> partners. If a round is in progress, give me my score and position. Otherwise
> reply with one line confirming nothing changed.

Because pages are pre-rendered server-side, the HTML pages work for this too —
there's no client-side JavaScript that a fetch-only checker would miss.

## Notes

- This is an unofficial personal mirror, not affiliated with or endorsed by the
  Golfweek Amateur Tour. Upstream is authoritative; always confirm tee times
  there before driving to a course.
- Mirrored announcement pages have their inline `<script>`/`<style>` stripped and
  relative links rewritten back to the origin.
- On the roster, "waiting" means signed up but not yet paid — every waiting row
  carries `Paid Tournament: No`. Registration state is reported as
  `registered` / `waiting` / `absent` / **`unknown`**; the last is deliberately
  distinct, since claiming "you are not signed up" from a missing snapshot
  would assert a falsehood about the thing you most need to trust.
- Page links omit `.html`: Cloudflare Pages 308s to the extensionless form, so
  linking to the file name would put a redirect on every click and every
  scheduled fetch.
- Scheduled workflows on GitHub are disabled after 60 days without repo
  activity. The data commits keep this one alive on their own.
