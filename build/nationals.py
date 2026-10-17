"""Offline views for the championship, retaining each flight's source order."""

from __future__ import annotations

from scraper.store import read_json


def identity(text: str) -> str:
    return " ".join(text.split()).casefold()


def championship_view(cfg: dict, today) -> dict | None:
    championship = read_json("nationals/schedule.json")
    if not championship:
        return None
    season = championship["season"]
    tracked = {identity(p["name"]): p for p in cfg["players"]}
    flights, group = [], []
    for entry in championship["flights"]:
        name = entry["name"]
        board = read_json(f"nationals/{season}/{name.lower()}.json") or {}
        # Never show a previous season or another tournament under this title.
        if (board.get("season"), board.get("flight"), board.get("tid")) != (season, name, entry.get("tid")):
            board = {}
        rows = []
        for original in board.get("rows", []):
            person = tracked.get(identity(original["name"]))
            row = {**original, "tracked": bool(person),
                   "slug": person["slug"] if person else None,
                   "is_dc": identity(original["tour"]) == identity(cfg["tour_name"])}
            rows.append(row)
            if person:
                group.append(row)
        flights.append({**entry, "rows": rows, "dc": [r for r in rows if r["is_dc"]],
                        "source_url": board.get("source_url") or (
                            f"{cfg['base_url']}/livescore/Leaderboard.aspx?t={entry['tid']}"
                            if entry.get("tid") else None),
                        "updated_at": board.get("fetched_at"),
                        "rounds": [r for r in championship["rounds"] if r["flight"] == name]})
    all_rows = [r for f in flights for r in f["rows"]]
    available = sum(bool(f["rows"]) for f in flights)
    complete = available == len(flights) and all(r["status"] == "complete" for r in all_rows)
    active = championship["start_date"] <= today.isoformat() <= championship["end_date"]
    if complete:
        status = "All 54 holes posted"
    elif active:
        status = "Championship underway"
    elif today.isoformat() < championship["start_date"]:
        status = "Upcoming championship"
    else:
        status = "Latest available scores"
    return {**championship, "flights": flights, "group": group,
            "not_on_board": [p for p in cfg["players"] if p["slug"] not in {r["slug"] for r in group}],
            "dc_count": sum(len(f["dc"]) for f in flights), "player_count": len(all_rows),
            "available_flights": available, "status": status, "active": active}


def summary(championship: dict | None, site_url: str, player: dict | None = None):
    if not championship:
        return None
    group = championship["group"]
    if player:
        group = [r for r in group if r["slug"] == player["slug"]]
    return {"name": championship["name"], "season": championship["season"],
            "start_date": championship["start_date"], "end_date": championship["end_date"],
            "url": f"{site_url}/nationals", "scores_url": f"{site_url}/nationals.json",
            "status": championship["status"], "group": group,
            "flights": [{"flight": f["name"], "players": len(f["rows"]),
                         "updated_at": f["updated_at"], "source_url": f["source_url"]}
                        for f in championship["flights"]]}


def digest_lines(championship: dict | None) -> list[str]:
    if not championship:
        return []
    lines = [f"NATIONALS: {championship['name']} - {championship['status']}"]
    for row in championship["group"]:
        rounds = " / ".join(value or "-" for value in row["rounds"])
        lines.append(f"  {row['name']}: {row['position']} in {row['flight']} flight; "
                     f"{row['to_par']}, {row['total']} strokes; R1/R2/R3 {rounds}; {row['progress']}")
    lines.extend(["  Round scores include strokes entered so far; positions are within each flight.",
                  f"  {championship['url']}", ""])
    return lines
