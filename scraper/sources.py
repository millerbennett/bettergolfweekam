"""One function per page on amateurgolftour.net, each returning plain JSON-able data."""

from __future__ import annotations

import logging
import re
from datetime import date

from .net import Fetcher
from .parse import (
    clean,
    find_table,
    first_link,
    rows_of,
    sectioned_table,
    select_options,
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

def _parse_us_date(text: str, season: int) -> str | None:
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


def fetch_schedule(fetcher: Fetcher, cfg: dict, season: int) -> dict:
    """The season schedule table, one record per playing day."""
    url = tour_url(cfg, "Schedule.aspx")
    page = fetcher.post_form(url, {"season_dd": str(season)}) if season != cfg["season"] \
        else fetcher.get(url)
    table = find_table(page, css_class="schedule-table")
    if not table:
        raise RuntimeError("schedule table not found")

    events = []
    for row in rows_of(table):
        if row.is_header or row.is_section or len(row.cells) < 4:
            continue
        iso = _parse_us_date(row.cells[0], season)
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
# Season points race
# --------------------------------------------------------------------------

def fetch_standings(fetcher: Fetcher, cfg: dict, season: int) -> dict:
    url = tour_url(cfg, "Standings.aspx")
    page = fetcher.post_form(url, {"tournament_dd": str(season)}) if season != cfg["season"] \
        else fetcher.get(url)
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

def fetch_livescore(fetcher: Fetcher, cfg: dict, tid: str) -> dict:
    """Poll the live leaderboard for a tournament.

    The livescore pages share the tour pages' tournament id namespace, so no
    lookup is needed. The dropdown only carries a rolling window of current
    events though, and posting an id outside it trips __EVENTVALIDATION and
    returns HTTP 500 - so check the options before submitting.
    """
    url = leaderboard_url(cfg)
    form = fetcher.form_page(url)
    if tid not in {value for value, _ in select_options(form, "tournaments_dd")}:
        log.info("livescore %s: not in the leaderboard's current window", tid)
        return {"available": False, "live": False, "flights": []}

    page = fetcher.submit(url, form, {"tournaments_dd": tid, "Flights": "0"})
    match = re.search(r'<span id="lblLeaderBoard">(.*?)</span>', page, re.S)
    if not match:
        return {"available": True, "live": False, "flights": []}
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
    count = sum(len(f["rows"]) for f in flights)
    log.info("livescore %s: %d players scoring", tid, count)
    return {
        "available": True,
        "live": count > 0,
        "tournament": title,
        "date": board_date,
        "flights": flights,
    }


# --------------------------------------------------------------------------
# Announcement / info pages
# --------------------------------------------------------------------------

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

    return re.sub(r"""\b(href|src)=(['"])([^'"]*)\2""", fix, fragment, flags=re.I)


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
