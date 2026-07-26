"""One function per page on amateurgolftour.net, each returning plain JSON-able data."""

from __future__ import annotations

import logging
import re
from datetime import date

from .net import Fetcher
from .parse import (
    clean,
    find_all_tables,
    find_table,
    first_link,
    rows_of,
    sectioned_table,
    select_options,
    selected_option,
    zip_record,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

def tour_url(cfg: dict, page: str) -> str:
    return f"{cfg['base_url']}/{cfg['tour_slug']}_tour_pages/{page}"


def leaderboard_url(cfg: dict) -> str:
    return f"{cfg['base_url']}/livescore/Leaderboard.aspx"


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------

def _parse_us_date(text: str) -> str | None:
    """'7/25/26' or '07/25/2026' -> ISO '2026-07-25'."""
    match = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", text or "")
    if not match:
        return None
    month, day, year = (int(g) for g in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def fetch_schedule(fetcher: Fetcher, cfg: dict, season: int | None = None) -> dict:
    """The season schedule table, one record per playing day.

    With `season=None` this follows whatever season the site itself has
    selected, so a January rollover needs no edit here. The detected season
    comes back in the payload and is what the rest of the mirror labels
    itself with.
    """
    url = tour_url(cfg, "Schedule.aspx")
    page = fetcher.get(url)
    current = selected_option(page, "season_dd")
    if season is not None and str(season) != current:
        page = fetcher.post_form(url, {"season_dd": str(season)})
        current = str(season)

    season = int(current) if current and current.isdigit() else season
    if season is None:
        raise RuntimeError("could not determine the current season")

    table = find_table(page, css_class="schedule-table")
    if not table:
        raise RuntimeError("schedule table not found")

    events = []
    for row in rows_of(table):
        if row.is_header or row.is_section or len(row.cells) < 4:
            continue
        iso = _parse_us_date(row.cells[0])
        if not iso:
            continue
        # "DC Metro Masters\n@ Twin Lakes GC (Oaks)"
        name_block = row.cells[1].split("\n")
        name = name_block[0].strip()
        course = name_block[1].lstrip("@ ").strip() if len(name_block) > 1 else ""
        # "9:00\nStraight Tee"
        start_block = row.cells[2].split("\n")

        # The Roster / Results links carry the tournament id used by every other
        # page. Match only those two: the course Website link in the same row can
        # also carry an unrelated `?id=` (a course id on the course's own site).
        tid = None
        for raw in row.raw_cells:
            href = first_link(raw) or ""
            match = re.search(r"(?:listing|results)\.aspx\?id=(\d+)", href, re.I)
            if match:
                tid = match.group(1)
                break

        events.append(
            {
                "tid": tid,
                "date": iso,
                "name": name,
                "course": course,
                "start_time": start_block[0].strip() if start_block else "",
                "start_type": start_block[1].strip() if len(start_block) > 1 else "",
                "cost": row.cells[3].strip() if len(row.cells) > 3 else "",
                "is_major": "major" in row.css,
                "course_url": first_link(row.raw_cells[4]) if len(row.raw_cells) > 4 else None,
                "register_url": first_link(row.raw_cells[5]) if len(row.raw_cells) > 5 else None,
                "registration_open": bool(len(row.raw_cells) > 5 and first_link(row.raw_cells[5])),
            }
        )

    log.info("schedule: %d events for %s", len(events), season)
    return {"season": season, "events": events}


# --------------------------------------------------------------------------
# Tee times / pairings  (POST only - Pairings.aspx?id= is not honoured)
# --------------------------------------------------------------------------

def fetch_pairings(fetcher: Fetcher, cfg: dict, tid: str) -> dict:
    """Tee times for one tournament.

    Pairings.aspx?id= is not honoured (unlike results.aspx), so this has to go
    through the dropdown as a form POST. Ids absent from that dropdown - combo
    events hosted by a neighbouring tour, for instance - fail __EVENTVALIDATION
    with an HTTP 500, so check the options first.
    """
    url = tour_url(cfg, "Pairings.aspx")
    form = fetcher.form_page(url, cache=True)
    if tid not in {value for value, _ in select_options(form, "tournament_dd")}:
        log.info("pairings %s: not offered on this tour's dropdown", tid)
        return {"tid": tid, "available": False, "published": False, "players": [], "groups": []}

    page = fetcher.submit(url, form, {"tournament_dd": tid})
    table = find_table(page, table_id="reports_grid")
    if not table:
        return {"tid": tid, "available": True, "published": False, "players": [], "groups": []}

    headers: list[str] = []
    players = []
    for row in rows_of(table):
        if row.is_header:
            headers = row.cells
            continue
        # An unpublished sheet still renders the grid, as a lone
        # `<td colspan="7">Empty</td>` row and no header row at all. Requiring
        # headers keeps that from being parsed as one blank player, which would
        # otherwise read downstream as "tee times are posted".
        if not headers or row.is_section or not row.cells or not any(row.cells):
            continue
        rec = zip_record(headers, row.cells)
        if not rec.get("ID", "").strip():
            continue
        # "44286 - Ayubi, Fred (Champ)" -> "Ayubi, Fred"
        display = rec.get("Name", "")
        name = re.sub(r"^\d+\s*-\s*", "", display)
        name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
        players.append(
            {
                "player_id": rec.get("ID", "").strip(),
                "name": name,
                "tour": rec.get("Tour", "").strip(),
                "flight": rec.get("Flight", "").strip("() "),
                "starting_hole": rec.get("Starting Hole", "").strip(),
                "group": rec.get("Group", "").strip(),
                "tee_time": rec.get("Tee Time", "").strip(),
            }
        )

    groups: dict[tuple[str, str], dict] = {}
    for player in players:
        key = (player["group"], player["tee_time"])
        group = groups.setdefault(
            key,
            {
                "group": player["group"],
                "tee_time": player["tee_time"],
                "starting_hole": player["starting_hole"],
                "players": [],
            },
        )
        group["players"].append(player)

    def sort_key(group: dict) -> tuple:
        number = re.search(r"(\d+)", group["group"])
        return (_time_key(group["tee_time"]), int(number.group(1)) if number else 0)

    log.info("pairings %s: %d players in %d groups", tid, len(players), len(groups))
    return {
        "tid": tid,
        "available": True,
        "published": bool(players),
        "players": players,
        "groups": sorted(groups.values(), key=sort_key),
    }


def _time_key(text: str) -> tuple[int, int]:
    match = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AP]M)?", text or "", re.I)
    if not match:
        return (99, 99)
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = (match.group(3) or "").upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return (hour, minute)


# --------------------------------------------------------------------------
# Final results  (plain GET works here)
# --------------------------------------------------------------------------

def fetch_results(fetcher: Fetcher, cfg: dict, tid: str) -> dict:
    page = fetcher.get(tour_url(cfg, f"results.aspx?id={tid}"))
    table = find_table(page, css_class="schedule-table")
    if not table:
        return {"tid": tid, "posted": False, "columns": [], "flights": []}

    headers, records = sectioned_table(table)
    flights: list[dict] = []
    for rec in records:
        if not flights or flights[-1]["name"] != rec["section"]:
            flights.append({"name": rec["section"], "rows": []})
        flights[-1]["rows"].append(zip_record(headers, rec["values"]))

    posted = any(f["rows"] for f in flights)
    log.info("results %s: posted=%s, %d flights", tid, posted, len(flights))
    return {"tid": tid, "posted": posted, "columns": headers, "flights": flights}


# --------------------------------------------------------------------------
# Roster / field  (plain GET, like results)
# --------------------------------------------------------------------------

FLIGHTS = ("Champ", "A", "B", "C", "D")


def _flight_summary(table_html: str) -> dict:
    """Read one of the roster page's two wide summary tables.

    Both have the same shape: an outer header row using colspan, a sub-header
    row naming the flights, then a single row of values. Columns are located by
    finding the flight labels rather than by fixed index, so an added or
    reordered flight does not silently shift every number by one.
    """
    rows = rows_of(table_html)
    header, sub, values = "", None, None
    for i, row in enumerate(rows):
        positions = {j: c for j, c in enumerate(row.cells) if c in FLIGHTS}
        if len(positions) >= 3 and i + 1 < len(rows):
            header = " ".join(rows[i - 1].cells) if i else ""
            sub, values = positions, rows[i + 1].cells
            break
    if not sub or not values:
        return {}

    first, last = min(sub), max(sub)

    def at(index: int) -> str:
        return values[index] if 0 <= index < len(values) else ""

    return {
        "header": header,
        "by_flight": {label: at(index) for index, label in sub.items()},
        "leading": at(first - 1) if first else "",
        "total": at(last + 1),
        "trailing": at(last + 2),
    }


def _player_rows(table_html: str) -> list[dict]:
    headers, records = sectioned_table(table_html)
    players = []
    for rec in records:
        row = zip_record(headers, rec["values"])
        if not row.get("ID", "").strip():
            continue
        players.append(
            {
                "player_id": row.get("ID", "").strip(),
                "name": row.get("Player", "").strip(),
                "flight": row.get("Flight", "").strip(),
                "tour": row.get("Home Tour", "").strip(),
                "paid_member": row.get("Paid Member", "").strip(),
                "paid_tournament": row.get("Paid Tournament", "").strip(),
            }
        )
    return players


def fetch_roster(fetcher: Fetcher, cfg: dict, tid: str) -> dict:
    """Who is signed up for a tournament, plus how much room is left.

    The page stacks four tables: capacity by flight, the confirmed field, a
    waiting-list summary, and the waiting list itself. "Waiting" here means
    signed up but not yet paid - every waiting row has Paid Tournament = No.
    """
    page = fetcher.get(tour_url(cfg, f"listing.aspx?id={tid}"))
    tables = find_all_tables(page, css_class="schedule-table")
    if not tables:
        return {"tid": tid, "available": False, "registered": [], "waiting": []}

    capacity, waiting_summary = {}, {}
    player_tables = []
    for table in tables:
        rows = rows_of(table)
        if not rows:
            continue
        heading = " ".join(rows[0].cells)
        if "Player" in heading and "Flight" in heading:
            player_tables.append(table)
        elif "Waiting" in heading:
            waiting_summary = _flight_summary(table)
        elif "Slots" in heading:
            capacity = _flight_summary(table)

    registered = _player_rows(player_tables[0]) if player_tables else []
    waiting = _player_rows(player_tables[1]) if len(player_tables) > 1 else []

    total_slots = _as_int(capacity.get("leading"))
    open_slots = _as_int(capacity.get("trailing"))
    filled = _as_int(capacity.get("total"))

    log.info("roster %s: %s registered, %s waiting, %s open of %s",
             tid, len(registered), len(waiting), open_slots, total_slots)
    return {
        "tid": tid,
        "available": True,
        "total_slots": total_slots,
        "filled_slots": filled if filled is not None else len(registered),
        "open_slots": open_slots,
        "sold_out": open_slots == 0 if open_slots is not None else None,
        "filled_by_flight": capacity.get("by_flight", {}),
        "waiting_by_flight": waiting_summary.get("by_flight", {}),
        "total_waiting": _as_int(waiting_summary.get("total")),
        "registered": registered,
        "waiting": waiting,
    }


def _as_int(text: str | None) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Season points race
# --------------------------------------------------------------------------

def fetch_standings(fetcher: Fetcher, cfg: dict, season: int | None = None) -> dict:
    """The season points race. `season=None` follows the site's own selection."""
    url = tour_url(cfg, "Standings.aspx")
    page = fetcher.get(url)
    current = selected_option(page, "tournament_dd")
    if season is not None and str(season) != current:
        page = fetcher.post_form(url, {"tournament_dd": str(season)})
        current = str(season)
    season = int(current) if current and current.isdigit() else season

    table = find_table(page, css_class="schedule-table")
    if not table:
        raise RuntimeError("standings table not found")

    headers, records = sectioned_table(table)
    flights: list[dict] = []
    for rec in records:
        if not flights or flights[-1]["name"] != rec["section"]:
            flights.append({"name": rec["section"], "rows": []})
        flights[-1]["rows"].append(zip_record(headers, rec["values"]))

    total = sum(len(f["rows"]) for f in flights)
    log.info("standings %s: %d players across %d flights", season, total, len(flights))
    return {"season": season, "columns": headers, "flights": flights}


# --------------------------------------------------------------------------
# Livescore leaderboard
# --------------------------------------------------------------------------

def board_url(cfg: dict, tid: str) -> str:
    """Deep link to the upstream leaderboard for one tournament."""
    return f"{leaderboard_url(cfg)}?t={tid}"


def fetch_livescore(fetcher: Fetcher, cfg: dict, tid: str) -> dict:
    """Poll the live leaderboard for a tournament.

    `?t=<tid>` selects the tournament directly, which beats driving the
    dropdown three ways: one request instead of two, no __EVENTVALIDATION
    window to trip (posting an archived id returns HTTP 500), and it keeps
    working for events that have aged out of the dropdown entirely.

    The livescore app shares the tour pages' tournament id namespace, so the
    id from a `results.aspx?id=` link works here unchanged.
    """
    page = fetcher.get(board_url(cfg, tid))
    match = re.search(r'<span id="lblLeaderBoard">(.*?)</span>', page, re.S)
    if not match:
        return {"available": False, "live": False, "flights": []}
    board = match.group(1)

    title, board_date = "", ""
    name_row = re.search(r"""<td[^>]*class=['"]lbTournamentName['"][^>]*>(.*?)</td>""", board, re.S | re.I)
    if name_row:
        parts = clean(name_row.group(1)).split("\n")
        title = parts[0] if parts else ""
        board_date = parts[1] if len(parts) > 1 else ""

    flights: list[dict] = []
    for row in rows_of(board):
        if "lbTournamentName" in row.css or "lbHeader" in row.css:
            continue
        if "lbHoleHeader" in row.css or row.is_section:
            if row.cells and row.cells[0]:
                flights.append({"name": row.cells[0], "rows": []})
            continue
        if "cutline" in " ".join(row.raw_cells) or len(row.cells) < 4:
            continue
        # Position | Player | (blank) | Total | Thru | Currently
        display = row.cells[1]
        name = display.split(" - ")[0].strip()
        if not flights:
            flights.append({"name": "Leaderboard", "rows": []})
        flights[-1]["rows"].append(
            {
                "position": row.cells[0],
                "name": name,
                "display": display,
                "total": row.cells[3],
                "thru": row.cells[4] if len(row.cells) > 4 else "",
                "to_par": row.cells[5] if len(row.cells) > 5 else "",
            }
        )

    flights = [f for f in flights if f["rows"]]
    rows = [r for f in flights for r in f["rows"]]

    # "Has rows" is not the same as "is being played". Read the Thru column:
    # a player between 1 and 17 holes is still out there, 18 (or F) is done.
    # Without this the board reads as live all evening after the last putt,
    # and reads as nothing at all after the horn but before the first score.
    out, done = 0, 0
    for row in rows:
        holes = _thru(row.get("thru"))
        if holes is None:
            continue
        if holes >= 18:
            done += 1
        elif holes > 0:
            out += 1

    if not rows:
        status = "not_started"
    elif out:
        status = "in_progress"
    elif done:
        status = "complete"
    else:
        status = "in_progress"     # scores exist but Thru is unreadable

    log.info("livescore %s: %d players, %d still out, %d finished (%s)",
             tid, len(rows), out, done, status)
    return {
        "available": True,
        "live": bool(rows),          # the board has something to show
        "status": status,            # not_started | in_progress | complete
        "players": len(rows),
        "still_out": out,
        "finished": done,
        "tournament": title,
        "date": board_date,
        "flights": flights,
    }


def _thru(value: str | None) -> int | None:
    """Holes completed. The board uses a number, or F/- for a finished card."""
    text = (value or "").strip().upper()
    if text in ("F", "FIN", "FINAL"):
        return 18
    match = re.match(r"(\d{1,2})", text)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# Skins / CTP cash games
# --------------------------------------------------------------------------

def skins_url(cfg: dict, tid: str | None = None) -> str:
    url = f"{cfg['base_url']}/livescore/skinsLB.aspx"
    return f"{url}?t={tid}" if tid else url


def _parse_game(table_html: str) -> dict | None:
    """One cash-game table: a title, up to 18 hole rows, then a pot summary.

    Skins tables and the CTP table share this shape; CTP simply has no holes.
    """
    rows = rows_of(table_html)
    if not rows:
        return None

    title, holes, summary = "", [], {}
    for row in rows:
        cells = row.cells
        if not any(cells):
            continue
        if row.is_section and not title:
            title = cells[0]
            continue
        if cells[0].strip() == "Hole":
            continue  # column header
        if cells[0].strip().isdigit() and len(cells) >= 4:
            hole = {
                "hole": cells[0].strip(),
                "player": cells[1].strip(),
                "score": cells[2].strip(),
                "type": cells[3].strip(),
                "paid_out": cells[4].strip() if len(cells) > 4 else "",
            }
            if any(hole[k] for k in ("player", "type")):
                holes.append(hole)
            continue
        # "... | Total Skins Pot: | $300"
        for i, cell in enumerate(cells[:-1]):
            if cell.strip().endswith(":"):
                summary[cell.strip().rstrip(":")] = cells[i + 1].strip()

    if not title or (not holes and not summary):
        return None
    return {"title": title, "holes": holes, "summary": summary}


def fetch_skins(fetcher: Fetcher, cfg: dict, tid: str) -> dict:
    """Skins and closest-to-the-pin results for a tournament.

    Public, despite being reachable only via the livescore area: no login is
    needed here or on the leaderboard. Each event runs several games at once -
    a Super Skins across all flights, one per flight, and a CTP pot.

    Selected by `?t=<tid>` for the same reasons as the leaderboard. Skins data
    outlives the live board: an archived event still returns its games after
    the leaderboard itself has been emptied.
    """
    page = fetcher.get(skins_url(cfg, tid))
    match = re.search(r'<span id="lblLeaderBoard">(.*?)</span>', page, re.S)
    if not match:
        return {"tid": tid, "available": False, "games": []}

    tables = find_all_tables(match.group(1))
    games = [g for g in (_parse_game(t) for t in tables) if g]
    log.info("skins %s: %d games", tid, len(games))
    return {"tid": tid, "available": True, "games": games}


# --------------------------------------------------------------------------
# Announcement / info pages
# --------------------------------------------------------------------------

def discover_content_pages(fetcher: Fetcher, cfg: dict) -> list[dict]:
    """Find the tour's info pages from its own home page nav.

    These ids are season-specific in places ("2026 Hole-N-One Challenge"), so a
    hardcoded list goes stale every January and quietly mirrors dead pages.
    Reading them off the nav each day keeps the set current by itself.
    """
    page = fetcher.get(tour_url(cfg, "default.aspx"))
    slug = cfg["tour_slug"]
    found: dict[int, str] = {}
    for match in re.finditer(
        r"""<a[^>]*href=['"][^'"]*%s_tour_pages/readContent\.aspx\?id=(\d+)['"][^>]*>(.*?)</a>"""
        % re.escape(slug),
        page, re.S | re.I,
    ):
        page_id, title = int(match.group(1)), clean(match.group(2))
        # The nav repeats some links as bare images; keep the labelled one.
        if title and (page_id not in found or not found[page_id]):
            found[page_id] = title

    pages = [{"id": pid, "title": title} for pid, title in sorted(found.items()) if title]
    log.info("discovered %d content pages from the tour home page", len(pages))
    return pages


def _absolutise(fragment: str, page_base: str) -> str:
    """Point href/src in mirrored markup back at the origin.

    Relative links like `Pairings.aspx` or `../images/x.jpg` would otherwise
    404 against our own domain once the fragment is re-hosted.
    """
    origin = re.match(r"(https?://[^/]+)", page_base).group(1)

    def fix(match: re.Match) -> str:
        attr, quote, url = match.group(1), match.group(2), match.group(3)
        if re.match(r"(https?:|mailto:|tel:|data:|#|//)", url, re.I):
            return match.group(0)
        target = origin + url if url.startswith("/") else page_base + url
        return f"{attr}={quote}{target}{quote}"

    fragment = re.sub(r"""\b(href|src)=(['"])([^'"]*)\2""", fix, fragment, flags=re.I)
    return _open_offsite(fragment)


def _open_offsite(fragment: str) -> str:
    """Make links in mirrored markup open in a new tab.

    Everything in here points back at amateurgolftour.net, so following one
    in place would silently navigate the reader out of the mirror.
    """
    def mark(match: re.Match) -> str:
        tag = match.group(0)
        if "target=" in tag.lower():
            return tag
        return tag[:-1].rstrip() + ' target="_blank" rel="noopener">'

    return re.sub(r"<a\b[^>]*href=[^>]*>", mark, fragment, flags=re.I)


def fetch_content(fetcher: Fetcher, cfg: dict, page_id: int) -> dict:
    page = fetcher.get(tour_url(cfg, f"readContent.aspx?id={page_id}"))
    match = re.search(
        r'<div class="col-md-9 ml-auto">(.*?)(?=<div class="section-content">|<footer)', page, re.S
    )
    body = match.group(1) if match else ""
    # These pages inline their own <style>/<script>; drop both before mirroring
    # so upstream CSS cannot leak out and restyle our own page.
    body = re.sub(r"<(style|script)\b.*?</\1>", "", body, flags=re.S | re.I)
    body = _absolutise(body, f"{cfg['base_url']}/{cfg['tour_slug']}_tour_pages/")
    text = clean(body)
    title = ""
    heading = re.search(r"<h[12][^>]*>(.*?)</h[12]>", body, re.S | re.I)
    if heading:
        title = clean(heading.group(1))
    return {"id": page_id, "title": title, "html": body.strip(), "text": text}
