"""National championship discovery and its cumulative, 54-hole leaderboards.

These are different from local 18-hole boards: A1/A2 and B1/B2 are course
groups, while the live app publishes one combined board per competitive flight.
Keep the source's positions and totals; never sum daily boards or rank flights
against each other.
"""

from __future__ import annotations

import re

from . import sources
from .parse import rows_of, select_options

FLIGHTS = ("Champ", "A", "B", "C", "D")
ROUND_NAMES = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}
HEADERS = ("Position", "Player", "R1", "R2", "R3", "Total", "Thru", "Currently")


def discover(fetcher, cfg: dict) -> dict:
    schedule = sources.fetch_schedule(
        fetcher, {**cfg, "tour_path": "tour_championship"}, cfg.get("season")
    )
    rounds = []
    for event in schedule["events"]:
        match = re.fullmatch(
            r"(Champ|[ABCD])([12]?) Flight Day (1|2|3|One|Two|Three)",
            event["name"], re.I,
        )
        if match and event.get("tid"):
            flight, pool, day = match.groups()
            rounds.append({**event, "flight": flight.capitalize(), "pool": pool,
                           "round": ROUND_NAMES[day.lower()]})
    if not rounds:
        raise RuntimeError("no championship rounds found; keeping the previous schedule")
    dates = sorted({e["date"] for e in rounds})
    season = schedule["season"]
    if any(not d.startswith(f"{season}-") for d in dates):
        raise RuntimeError("championship schedule contains another season")

    page = fetcher.get(sources.leaderboard_url(cfg))
    boards = {}
    for tid, title in select_options(page, "tournaments_dd"):
        match = re.fullmatch(rf"{season} (?:Golfweek )?Championship - (Champ|[ABCD]) Flight", title, re.I)
        if match and tid.isdigit():
            boards[match.group(1).capitalize()] = tid
    # The dropdown is authoritative. Before it lists the championship, show
    # the discovered schedule and an honest pending state, without guessing ids.
    return {
        "season": season, "name": f"{season} National Championship",
        "start_date": dates[0], "end_date": dates[-1], "rounds": rounds,
        "source_url": f"{cfg['base_url']}/tour_championship/Schedule.aspx",
        "flights": [{"name": name, "tid": boards.get(name)} for name in FLIGHTS],
    }


def progress(thru: str, rounds: list[str]) -> dict:
    """Thru is TOTAL holes, not holes in today's round. Preserve unknowns."""
    text = thru.strip().upper()
    holes = int(text) if text.isdigit() and 0 <= int(text) <= 54 else None
    if text in ("F", "FIN", "FINAL") and all(r.strip().isdigit() for r in rounds):
        holes = 54
    if holes is None:
        return {"holes": None, "progress": text or "Awaiting scores", "status": "unknown"}
    if holes == 54:
        return {"holes": holes, "progress": "Finished · 54 holes", "status": "complete"}
    if holes == 0:
        return {"holes": holes, "progress": "Not started", "status": "not_started"}
    if holes % 18 == 0:
        return {"holes": holes, "progress": f"R{holes // 18} complete · {holes} holes",
                "status": "between_rounds"}
    return {"holes": holes, "progress": f"R{holes // 18 + 1} · thru {holes % 18} of 18",
            "status": "in_progress"}


def fetch_board(fetcher, cfg: dict, championship: dict, flight: dict) -> dict:
    tid = flight["tid"]
    url = sources.board_url(cfg, tid)
    page = fetcher.get(url)
    match = re.search(r'<span\b[^>]*id=[\'"]lblLeaderBoard[\'"][^>]*>(.*?)</span>', page, re.S | re.I)
    if not match:
        raise RuntimeError(f"{flight['name']} championship leaderboard missing")
    board = match.group(1)
    table_rows = rows_of(board)
    title = next((" ".join(row.cells) for row in table_rows
                  if row.css.lower() == "lbtournamentname"), "")
    if (not re.search(rf"\b{championship['season']}\b", title)
            or not re.search(rf"\b{flight['name']} Flight\b", title, re.I)):
        raise RuntimeError(f"wrong championship season/flight returned for {tid}: {title}")

    headers = None
    players = []
    for row in table_rows:
        if row.css.lower() == "lbheader":
            headers = row.cells
            if tuple(headers) != HEADERS:
                raise RuntimeError(f"championship columns changed: {headers}")
            continue
        if headers is None or row.is_section or not any(row.cells):
            continue
        if len(row.cells) != len(HEADERS):
            if len(row.cells) > 1:
                raise RuntimeError(f"championship player columns changed: {row.cells}")
            continue
        values = dict(zip(headers, row.cells))
        display = values["Player"]
        # Split from the right: tour names can contain hyphens, but the
        # delimiter is space-hyphen-space. Name and home tour both survive.
        identity, sep, player_flight = display.rpartition(" - ")
        name, tour_sep, tour = identity.partition(" - ")
        if not sep or not tour_sep or player_flight.strip() != flight["name"]:
            raise RuntimeError(f"unrecognised championship player: {display}")
        rounds = [values[f"R{n}"] for n in range(1, 4)]
        players.append({
            "name": name.strip(), "tour": tour.strip(), "flight": flight["name"],
            "position": values["Position"], "rounds": rounds, "total": values["Total"],
            "thru": values["Thru"], "to_par": values["Currently"],
            **progress(values["Thru"], rounds),
        })
    if headers is None:
        raise RuntimeError("championship column header missing")
    return {"season": championship["season"], "flight": flight["name"],
            "tid": tid, "source_url": url, "rows": players}
