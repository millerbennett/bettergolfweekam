# Architecture & handoff notes

Everything needed to pick this project up cold. `README.md` is the short
version for someone using it; this is the long version for someone changing it.

Written 2026-07-26, last updated for the multi-player change.

---

## 1. What this is

A read-only mirror of one tour's pages on
[amateurgolftour.net](https://amateurgolftour.net/dc_tour_pages/default.aspx),
rendered as a static site.

Three jobs, in priority order:

1. **Tell a player their tee time** without navigating an ASP.NET dropdown on
   a phone.
2. **Be readable by a scheduled assistant** (ChatGPT tasks), which is why every
   page is pre-rendered server-side and why `digest.txt` exists.
3. **Cost nothing** — inside the GitHub Actions and Cloudflare Pages free tiers.

Non-goals: writing anything back upstream, replacing the tour's site, serving
players outside the configured tour.

---

## 2. The upstream site

An ASP.NET WebForms app. Understanding its quirks is most of understanding
this project — each of these cost real debugging time and is encoded in
`scraper/`.

### 2.1 Page inventory

| Page | Access | Notes |
|---|---|---|
| `{slug}_tour_pages/Schedule.aspx` | GET | Season in `season_dd`, current one marked `selected` |
| `{slug}_tour_pages/Standings.aspx` | GET | Season in `tournament_dd` (confusingly named) |
| `{slug}_tour_pages/Pairings.aspx` | **POST only** | `?id=` is ignored — must drive the dropdown |
| `{slug}_tour_pages/results.aspx?id=` | GET | Works by querystring |
| `{slug}_tour_pages/listing.aspx?id=` | GET | Roster: 4 stacked tables |
| `{slug}_tour_pages/readContent.aspx?id=` | GET | Announcement/info pages |
| `livescore/Leaderboard.aspx?t=` | GET | **Undocumented `?t=` param** |
| `livescore/skinsLB.aspx?t=` | GET | Same |
| `livescore/Score.aspx` | — | **Write interface. Never touched.** |

### 2.2 Quirks, and why the code looks the way it does

**Tournament ids are one namespace.** The id in a `results.aspx?id=` link is
the same id the livescore app uses. An early version had a date+course join to
map between them; it was unnecessary and was deleted.

**`?t=` beats the dropdown POST.** Undocumented, but on both livescore pages it
selects a tournament directly. Three advantages over the POST: one request
instead of two (no GET for `__VIEWSTATE` first), no `__EVENTVALIDATION` window
to trip, and it still resolves events that aged out of the dropdown — the March
kick-off still returns its skins games long after its live board was emptied.

**`__EVENTVALIDATION` returns HTTP 500, not a friendly error.** Posting a
dropdown value that isn't in the page's own rendered option list fails hard.
This still applies to `Pairings.aspx`, which has no `?id=` escape hatch, so
`fetch_pairings` checks the options before submitting. Combo events hosted by a
neighbouring tour appear on the DC schedule but not the DC pairings dropdown,
and are skipped rather than 500'd.

**An unpublished pairing sheet still renders a grid.** It comes back as a lone
`<td colspan="7">Empty</td>` with no header row. Parsed naively that becomes one
blank player, which reads downstream as *"tee times are posted"* — a false alert
weeks early. `fetch_pairings` requires a header row and a non-empty ID.

**The schedule's course link can carry its own `?id=`.** The first row's course
website is `supersite.asp?id=988`. Scanning the row for any `id=` grabs the
course id instead of the tournament id, so the match is restricted to
`listing|results\.aspx\?id=`.

**The livescore area's "login" is not needed to read anything.** It takes a
golfer id and no password. Both the leaderboard and skins board are public;
`Card.aspx` just bounces to the scorekeeper home. The login exists to *enter*
scores. See §11 for the security note.

**`robots.txt` asks for `Crawl-Delay: 10`.** Honoured globally in
`scraper/net.py`. This is the single biggest constraint on crawl design.

### 2.3 Table shapes

Three recurring shapes, all handled in `scraper/parse.py`:

- `<table class='schedule-table'>` — schedule, results, standings. Flight
  headings are `<tr class='schedule-table-regional'><td colspan=N>`. Majors get
  `schedule-table-major` on the row.
- `<table id="reports_grid">` — pairings.
- `<span id="lblLeaderBoard">` — livescore and skins, with `lbHoleHeader`
  marking flight sections and `cutline` rows as separators.

Regex extraction is used rather than a DOM parser: the markup is flat and
regular, the failure modes are more predictable, and it keeps CI dependencies
to `requests` + `jinja2`.

---

## 3. Data flow

```
 amateurgolftour.net
        │  scraper/net.py      rate-limited session, __VIEWSTATE helper
        │  scraper/parse.py    table → rows
        │  scraper/sources.py  one function per page → plain dicts
        ▼
    data/*.json               committed snapshots — the mirror's source of truth
        │  build/render.py     + jinja templates
        ▼
      public/                 static site → Cloudflare Pages (direct upload)
```

`scraper/crawl.py` orchestrates: it decides *what is stale* and calls only
those `sources` functions. `build/render.py` never touches the network — it
reads `data/` only, which is why the render smoke test in CI works offline.

---

## 4. Repo layout

```
config.json              tour, players[], freshness thresholds
scraper/
  net.py       Fetcher: 10s crawl delay, retries, __VIEWSTATE/form helpers,
               per-run form-page cache
  parse.py     clean/rows_of/find_table/find_all_tables/sectioned_table,
               select_options, selected_option
  sources.py   fetch_schedule, fetch_pairings, fetch_results, fetch_roster,
               fetch_standings, fetch_livescore, fetch_skins, fetch_content,
               discover_content_pages
  store.py     read/write snapshots, freshness state, change feed
  crawl.py     Crawler: the planner + change detection
build/
  render.py    Site: derived views, page rendering, digest/status/RSS
  templates/   jinja2 (base, index, schedule, standings, players, player,
               feed, event, info, info_page, 404)
  static/      style.css, _headers
data/          committed JSON snapshots (see §6)
tests/         fixtures/ = real captured pages; 112 tests
public/        generated, gitignored
```

---

## 5. Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `tour_slug` | URL segment: `dc` → `/dc_tour_pages/` |
| `tour_name` | Exact upstream spelling, e.g. `"Washington, DC Metro"` — used to match the tour on cross-tour boards |
| `tour_short`, `site_title` | Display only |
| `season` | **`null` = follow the site.** A number pins an old season. |
| `timezone` | Drives "today"; must be the tour's local zone |
| `base_url` | Origin |
| `players[]` | One entry per tracked player: `{slug, id, name, primary?}` |
| `players[].id` | Golfer id — matches roster/results/standings/pairings |
| `players[].name` | `"Last, First"`, **exact**. The live board and skins have no ids, so this is the only way to find someone there. |
| `players[].slug` | URL segment: `/p/<slug>` |
| `players[].primary` | Whose view is served at the root `/digest.txt`, `/status.json`, `/me` |
| `crawl_delay_seconds` | 10, per upstream robots.txt. Don't lower it. |
| `content_pages` | Fallback list only — pages are normally auto-discovered |

### Freshness thresholds

Everything the planner uses. All hours unless named otherwise.

| Key | Value | What it controls |
|---|---|---|
| `schedule_hours` | 12 | Schedule refresh |
| `standings_hours` | 12 | Points race refresh |
| `content_hours` | 24 | Info pages + content discovery |
| `pairings_window_days` | 5 | How far ahead to watch for tee times |
| `pairings_minutes` | 45 | Poll interval inside that window, until published |
| `results_window_days` | 10 | How long after an event to look for official results |
| `results_recent_hours` | 3 | Poll interval until results post |
| `roster_window_days` | 45 | How far ahead to track the field |
| `roster_hours` | 6 | Roster poll interval |
| `live_retain_days` | 4 | Keep polling the board after the round |
| `live_after_hours` | 3 | Board poll interval once the day is over |
| `live_window_hours` | 14 | *Currently unused — left in place, harmless* |

---

## 6. Data files

All under `data/`. Committed **except** the two marked gitignored.

| File | Shape |
|---|---|
| `schedule.json` | `{season, events[], fetched_at}` — event = `{tid, date, name, course, start_time, start_type, cost, is_major, course_url, register_url, registration_open}` |
| `standings.json` | `{season, columns[], flights[{name, rows[]}]}` — rows keyed by column name |
| `pairings/<tid>.json` | `{tid, available, published, players[], groups[], event}` |
| `results/<tid>.json` | `{tid, posted, columns[], flights[], event}` |
| `roster/<tid>.json` | `{tid, available, total_slots, filled_slots, open_slots, sold_out, filled_by_flight, waiting_by_flight, total_waiting, registered[], waiting[], event}` |
| `live/<tid>.json` | `{available, live, status, players, still_out, finished, tournament, date, flights[], tid, event}` |
| `skins/<tid>.json` | `{tid, available, games[{title, holes[], summary{}}], event}` |
| `content/<id>.json` | `{id, title, nav_title, html, text}` |
| `content_index.json` | `{pages[{id, title}]}` — discovered nav |
| `changes.json` | Append-front feed, capped at 300 |
| `state.json` | **gitignored** — freshness stamps, persisted via Actions cache |
| `meta.json` | **gitignored** — last run diagnostics |
| `heartbeat.json` | `{week}` — see §9 |

### The snapshot-write rule (important)

`store.save_snapshot` **leaves the file byte-for-byte alone when content is
unchanged.** This is load-bearing, not an optimisation: the workflow gates both
its git commit and its Cloudflare deploy on `git diff` over `data/`. An earlier
version bumped `fetched_at` on every poll, so every cron run produced a commit
and a deploy whether or not the tour had published anything.

Consequence: **`fetched_at` means "when this content last changed"**, not "when
we last looked". Last-looked lives in `state.json`.

---

## 7. The crawl planner (`scraper/crawl.py`)

`--mode auto` is what cron runs. It reads the schedule and `state.json`, then
fetches only what's due. On a quiet day it makes **zero requests**.

Modes:

| Mode | Behaviour |
|---|---|
| `auto` | Freshness-driven. Default. |
| `daily` | Forces schedule, standings, content. |
| `live` | Only today's board + skins. |
| `full` | Backfills every event's pairings, plus rosters ahead / results behind. ~10 min. |

`--force` ignores freshness stamps.

Two safety behaviours worth knowing:

- **`attempt()` isolates each unit.** One flaky page fails alone; the rest of
  the run still commits, and `state.json` is still written.
- **An empty parse cannot blank the mirror.** If the schedule or standings come
  back with zero rows when rows were previously known, the crawl *raises*
  rather than overwriting. A redesign becomes a failed run, not a site that
  quietly renders nothing.

### How "live" is decided

Not by the clock, and not by "the board has rows" — that was the first
implementation and it was wrong in both directions. Status comes from the
board's **Thru** column:

| Status | Means |
|---|---|
| `not_started` | Board exists, no scores |
| `in_progress` | Someone between 1 and 17 holes |
| `complete` | Everyone thru 18 (or `F`) |

Only `in_progress` on today's date renders as "Playing now".

**Caveat:** this reflects *scores entered*, not reality. One player per group
keeps score, so a group that hasn't posted looks further back than it is, and a
round flips to `complete` when the last scorekeeper submits.

---

## 8. Rendering (`build/render.py`)

`Site` loads `data/`, exposes derived views (`next_event`, `live_now`,
`today_board`, `latest_board`, `my_standing`, `my_pairing`, `my_roster_status`,
`me_on_board`, …), then renders every page.

### Routes

| Route | Source |
|---|---|
| `/` | index.html — today's board, next up, your season, feed, recent results |
| `/schedule` | Grouped: happening today → coming up → played (newest first) |
| `/standings` | Points race, your row highlighted |
| `/p/` | Group overview — one row per player |
| `/p/<slug>` | A player's dashboard: standing, next event, and a season timeline showing where they stand on every event (played / entered / waitlist / not entered) |
| `/p/<slug>/digest.txt`, `/p/<slug>/status.json` | That player's machine-readable view |
| `/me` | Alias for the primary player, kept for old bookmarks |
| `/feed` | Change feed |
| `/info`, `/info/<id>` | Mirrored announcement pages |
| `/t/<tid>` | Event: field, tee times, live board, skins, results |
| `/status.json`, `/digest.txt`, `/feed.xml` | Machine-readable |
| `/404.html`, `/robots.txt`, `/_headers` | Infra |

### Two rendering rules that are easy to break

**Links omit `.html`.** Cloudflare Pages 308-redirects `/schedule.html` →
`/schedule`. Linking to the file name puts a redirect on every click and every
scheduled fetch. Files are still written as `.html`; only hrefs are canonical.
The `url()` helper in `_render` handles this — use it.

**Schedule rows hold two independent links** (open the event, or register), so
the row cannot be one `<a>`. The title stretches its hit area over the row via
`::after`; the button layers above it with `z-index`.

### `unknown` vs `absent`

`my_roster_status` returns `registered` / `waiting` / `absent` / **`unknown`**.
The last is deliberate: with no roster snapshot, saying *"you are not signed
up"* asserts a falsehood about the thing the reader most needs to trust.
`digest.txt` omits the line entirely when unknown. Preserve this distinction.

---

## 9. CI/CD

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push / PR | 112 tests + render smoke test |
| `update.yml` | cron `*/20 10-23 * * *` and `25 8 * * *`; dispatch | Crawl → commit → render → deploy (gated) |
| `deploy.yml` | push touching `build/**` or `config.json` | Render + deploy (no crawling) |

**Secrets:** `CLOUDFLARE_API_TOKEN` (permission: *Account → Cloudflare Pages →
Edit*, nothing else), `CLOUDFLARE_ACCOUNT_ID`.
**Variables (optional):** `SITE_URL`, `CF_PAGES_PROJECT` — both have working
defaults.
**Repo setting:** Actions → Workflow permissions → **Read and write**, or the
crawl can't commit.

Cloudflare project is **Direct Upload**, not the Git integration — CI uploads
the rendered `public/` itself, so Cloudflare never builds and data commits
don't trigger builds.

### The heartbeat

GitHub disables scheduled workflows after **60 days of repository inactivity**.
Nothing upstream changes between late September and March, and unchanged
snapshots are deliberately left alone (§6) — so without intervention the repo
would go silent over the winter and the cron would switch itself off before the
next season. `data/heartbeat.json` carries an ISO week stamp, so it moves once
a week (~52 commits/year) and is excluded from the deploy gate.

### Free-tier budget

- **Actions**: unlimited (public repo). ~45 runs/day, most exiting in seconds.
- **Cloudflare Pages**: 500 deploys/month. Deploys only happen on real content
  change: roughly 60/month baseline plus ~30 per event day. Comfortable.
- **Upstream politeness**: 10s between every request, globally.

---

## 10. Local development

```bash
pip install -r requirements-dev.txt

python -m scraper.crawl --mode auto      # or daily / live / full, plus --force
python -m build.render                   # writes public/
python -m http.server -d public 8000

python -m pytest tests/ -q               # 112 tests, no network
python -m pyflakes scraper/ build/ tests/
```

`tzdata` is in requirements because Windows has no IANA database; harmless on
Linux.

### Tests

- `test_parsers.py` — parsers against **real captured pages** in
  `tests/fixtures/`. These assert real values, so an upstream redesign fails CI
  instead of silently producing empty tables.
- `test_render.py` — renders a synthetic `data/` dir into a temp dir, with two
  real players configured so the shared-vs-personal split is covered.
- `test_store.py` — the snapshot-write rule.

Two fixtures are synthetic and labelled as such: `leaderboard_live.html` (the
real board with `Thru` rewound, since no genuine mid-round capture exists) and
`pairings_empty.html` / `roster_empty.html` (real, but of unpublished events).

**If upstream changes markup:** re-capture the fixture, fix the parser, update
the asserted values. Don't loosen an assertion to make it pass — the point is
that it *should* fail.

---

## 11. Security & etiquette notes

- **The golfer id is a passwordless credential.** `livescore/Default.aspx`
  accepts an id and no password, and ids are printed in the public standings
  table. Anyone who can read that page can log in as any player and enter
  scores. This project never touches `Score.aspx` and depends on nothing behind
  the login. Worth raising with the tour director; worth remembering before
  putting ids in URLs if this ever serves more than one player.
- **Mirrored HTML is sanitised**: inline `<script>`/`<style>` stripped, relative
  links rewritten to the origin, `target="_blank" rel="noopener"` added.
- **Nothing here is secret** — all mirrored data is already public. The repo is
  public so Actions minutes are unlimited.

---

## 11b. Multi-player model

Six players are tracked. The split that keeps this cheap:

- **Shared pages, rendered once**: `/`, `/schedule`, `/standings`, `/feed`,
  `/t/<tid>` × 21, `/info/*`. These highlight *everyone* in the group via
  `group_ids` (id match) and `group_names` (name match, for the livescore board
  and skins where ids don't exist).
- **Per player, 3 small artifacts**: `/p/<slug>/index.html`, `digest.txt`,
  `status.json`. So seven players = 39 shared + 21 personal + 1 group index,
  not 39 × 7.

The group table is ordered by flight (Champ down to D) then points within
each — flights are handicap bands, so a cross-flight points ranking would not
be a real contest. A ★ marks a player leading their flight outright, which is
distinct from merely being first in this group. Each flight has its own colour
token (`--f-champ` … `--f-d`); all ten badge combinations were checked against
WCAG AA, worst case 5.54:1.

Why not a copy of every page per player: 21 event pages × N people is a lot of
duplication for cosmetic highlighting, and it destroys shareable URLs — "look
at this event page" would have N different answers.

**The change feed is shared, so entries name the player** ("Ben Devine won a
skin") rather than saying "you". Personal facts are deliberately *not*
change-detected in the crawler any more — the renderer states them fresh from
the snapshots on every build. That means adding a player needs no crawler
change at all, and `crawl.py` stays O(1) in player count for requests.

A test asserts no first-person feed text survives in `crawl.py`.

## 12. Design decisions worth not re-litigating

| Decision | Why |
|---|---|
| Pre-rendered HTML, no SPA | ChatGPT's fetcher doesn't reliably run JS. This is the whole point. |
| Direct Upload, not Pages Git | Render happens in CI we control; data commits don't trigger builds. |
| `state.json` gitignored + cached | Committing it meant ~40 commits/day from cron alone. |
| Regex, not a DOM parser | Flat markup, predictable failures, fewer deps. |
| Season auto-detected | Otherwise this needs an edit every January. |
| Content pages discovered | Some ids are season-specific (`2026 Hole-N-One Challenge`). |
| Fixtures assert real values | A redesign must fail loudly, not render an empty site. |
| Shared pages + small per-player artifacts | Per-player copies of everything destroy shareable URLs for cosmetic gain. |
| Personal facts computed at render, not crawl | Keeps the crawler O(1) in player count and the feed unambiguous. |

---

## 13. Hard-won bugs (don't reintroduce)

1. **Blank tee times.** Empty pairing grid parsed as one blank player →
   reported "tee times posted" weeks early.
2. **Wrong tournament id.** Course website link's `?id=` shadowed the real tid.
3. **Deploy on every run.** `fetched_at` bumped unconditionally → git diff on
   every poll → commit + deploy for nothing.
4. **False liveness.** "Board has rows" ≠ playing.
5. **A literal `0x08` byte in a regex.** A `\b` word boundary passed through a
   non-raw Python string during an edit and was written to disk as a backspace
   character — invisible in an editor, and the pattern silently matched
   nothing. Found only by dumping the compiled pattern. If a regex
   inexplicably matches nothing, check for control bytes.
6. **A CSS hex escape ate the next character.** `content: " \2605"` for a star
   rendered as `°5` — the escape consumed digits greedily. Literal characters
   are used instead; this project has now been bitten three times by escapes
   passing through a layer that rewrote them.
7. **Contrast measured against the wrong backdrop.** Cards use a gradient, so
   hand-checking palette pairs against `--surface` was wrong. Measure by
   rendering twice (once with glyphs transparent) and sampling real pixels.

---

## 14. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Site frozen, workflows green | Deploy step skipped — nothing changed, which is correct |
| Deploy fails, everything else passes | Cloudflare token expired or wrong permission |
| Crawl commits but nothing pushes | Actions workflow permissions not "Read and write" |
| Cron stopped firing | 60-day inactivity — check the heartbeat is committing |
| CI red on parser tests | Upstream markup changed; re-capture fixtures |
| Crawl raises "refusing to overwrite" | Empty-parse guard did its job. Investigate upstream. |
| One player never highlights | Their `name` doesn't match the upstream spelling exactly |
| Everything empty after a January | Season detection failed; check `season_dd` still marks `selected` |

---

## 15. Known gaps / possible future work

- **Live board matches players by name**, not id — the one place ids don't
  exist. Duplicate names would double-highlight, and a misspelled config name
  fails silently rather than loudly.
- **No cross-tour support.** Every player must be on the configured tour.
  A buddy on Richmond or Tidewater needs a second crawl, which is the one
  change here that costs real time rather than milliseconds.
- **`live_window_hours`** in config is unused.
- **No historical season browsing** — old seasons are fetchable by pinning
  `season`, but the site only renders the current one. Old event files linger
  harmlessly in `data/`.
- **Skins/CTP aren't surfaced** in `digest.txt`, only on the event page.
