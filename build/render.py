"""Render data/*.json into a fully static site under public/.

Pre-rendered HTML rather than a client-side app, because the point of this
mirror is that a scheduled ChatGPT check can read it, and those fetches do not
reliably execute JavaScript. Every page ships its content in the markup, and
status.json / digest.txt exist as compact machine-readable summaries.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from scraper.store import DATA, PUBLIC, load_config, read_json

log = logging.getLogger("render")

HERE = Path(__file__).resolve().parent
SITE_URL_DEFAULT = "https://bettergolfweekam.pages.dev"


# --------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------

def pretty_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    # %-d is glibc-only, so build it by hand and keep local/CI output identical.
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def day_num(iso: str) -> str:
    try:
        return str(date.fromisoformat(iso).day)
    except (TypeError, ValueError):
        return "?"


def month_abbr(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%b")
    except (TypeError, ValueError):
        return ""


def slug(text: str) -> str:
    """Column name -> css class suffix, so narrow screens can drop columns."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "x"


def date_range(start: str, end: str) -> str:
    """'2026-08-15','2026-08-16' -> 'Aug 15-16'; spans months if needed."""
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        return day_month(start)
    if a.month == b.month:
        return f"{a.strftime('%b')} {a.day}-{b.day}"
    return f"{a.strftime('%b')} {a.day} - {b.strftime('%b')} {b.day}"


def day_month(iso: str) -> str:
    """'2026-03-22' -> 'Mar 22'. No weekday: it wrapped the column."""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.strftime('%b')} {d.day}"


def short_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return d.strftime("%a %b %d")


def make_days_away(today: date):
    def days_away(iso: str) -> str:
        try:
            delta = (date.fromisoformat(iso) - today).days
        except (TypeError, ValueError):
            return ""
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        if delta == -1:
            return "yesterday"
        return f"in {delta} days" if delta > 0 else f"{-delta} days ago"
    return days_away


def ago(stamp: str) -> str:
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return stamp or ""
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - seen).total_seconds()
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _points(row: dict) -> float | None:
    """Parse a standings Points cell, tolerating thousands separators."""
    try:
        return float(str(row.get("Points", "")).replace(",", "").strip() or 0)
    except ValueError:
        return None


def _first_last(name: str) -> str:
    """'Miller, Bennett' -> 'Bennett Miller' for display."""
    last, _, first = (name or "").partition(", ")
    return f"{first} {last}".strip() if first else last


def _link_rounds(events: list[dict]) -> None:
    """Mark the extra days of a multi-day event as rounds of the first.

    Five events on the DC schedule run over two days. They share a name, sit
    on consecutive dates, and the later day carries no entry fee and no
    register link, because one entry covers both rounds.

    All three signals are required. Dates alone would wrongly merge the
    Old Dominion combo weekend, where three differently-named events run on
    consecutive days and each is separately priced and entered.

    Each day keeps its own page: the tee times, live board, results and skins
    are all per-round. This only changes how they are listed.
    """
    for i, event in enumerate(events):
        event.setdefault("rounds", [])
        event.setdefault("round_of", None)
        event.setdefault("round_no", 1)
        if i == 0:
            continue
        prev = events[i - 1]
        try:
            consecutive = (date.fromisoformat(event["date"])
                           - date.fromisoformat(prev["date"])).days == 1
        except (TypeError, ValueError):
            continue
        priced = (event.get("cost") or "").strip().strip("$").strip()
        if not (consecutive and event["name"] == prev["name"] and not priced):
            continue
        head = prev["round_of"] or prev
        event["round_of"] = head
        event["round_no"] = len(head["rounds"]) + 2
        head["rounds"].append(event)


# Strongest flight first. Anything unrecognised - including a player with no
# standings row yet - sorts after all of these.
FLIGHT_ORDER = {"Champ": 0, "A": 1, "B": 2, "C": 3, "D": 4}


def flight_short(name: str) -> str:
    """'\"B\" Flight (9.0-13.9 Handicap)' -> 'B'."""
    match = re.match(r'\s*"?([A-Za-z]+)"?\s*Flight', name or "")
    if match:
        word = match.group(1)
        return "Champ" if word.lower().startswith("champ") else word.upper()
    return (name or "").split("(")[0].strip()


# --------------------------------------------------------------------------
# Data assembly
# --------------------------------------------------------------------------

class Site:
    def __init__(self, cfg: dict, site_url: str):
        self.cfg = cfg
        self.site_url = site_url.rstrip("/")
        self.tz = ZoneInfo(cfg["timezone"])
        self.now = datetime.now(self.tz)
        self.today = self.now.date()
        self.players = [dict(x) for x in cfg["players"]]
        self.primary = next((x for x in self.players if x.get("primary")), self.players[0])
        # Shared pages mark everyone in the group; a personal page marks its
        # own player more strongly. Ids cover every table except the livescore
        # board and skins, which carry names only.
        self.group_ids = {x["id"] for x in self.players}
        self.group_names = {x["name"] for x in self.players}

        self.schedule = read_json("schedule.json", default={"events": []}) or {"events": []}
        self.standings = read_json("standings.json", default={"flights": []}) or {"flights": []}
        self.changes = read_json("changes.json", default=[]) or []
        self.meta = read_json("meta.json", default={}) or {}

        # The season the mirror labels itself with comes from the crawled
        # schedule, not from config, so a rollover carries through on its own.
        self.season = self.schedule.get("season") or cfg.get("season")
        self.events = [e for e in self.schedule.get("events", []) if e.get("tid")]
        for event in self.events:
            delta = self._delta(event["date"])
            event["is_past"] = delta < 0
            event["is_today"] = delta == 0

        _link_rounds(self.events)
        for event in self.events:
            if not event["rounds"]:
                continue
            # A two-day event is not history until its last round is, and it
            # is "today" on either day - otherwise it would drop into Played
            # on the morning of round two.
            last = event["rounds"][-1]
            event["end_date"] = last["date"]
            event["is_past"] = last["is_past"]
            event["is_today"] = event["is_today"] or any(r["is_today"] for r in event["rounds"])

        self.pairings = {e["tid"]: read_json(f"pairings/{e['tid']}.json") for e in self.events}
        self.rosters = {e["tid"]: read_json(f"roster/{e['tid']}.json") for e in self.events}
        self.skins = {e["tid"]: read_json(f"skins/{e['tid']}.json") for e in self.events}
        self.results = {e["tid"]: read_json(f"results/{e['tid']}.json") for e in self.events}
        self.live = {e["tid"]: read_json(f"live/{e['tid']}.json") for e in self.events}

        self.env = Environment(
            loader=FileSystemLoader(HERE / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters.update(
            pretty_date=pretty_date,
            short_date=short_date,
            days_away=make_days_away(self.today),
            ago=ago,
            day_num=day_num,
            month_abbr=month_abbr,
            day_month=day_month,
            date_range=date_range,
            slug=slug,
            first_last=_first_last,
        )

    def upstream(self, tid: str) -> dict:
        """Deep links straight to the upstream views for one tournament.

        The livescore pages accept `?t=<tid>`, so these skip the dropdowns
        entirely - useful for checking the mirror against the real site.
        """
        base, slug = self.cfg["base_url"], self.cfg["tour_slug"]
        return {
            "leaderboard": f"{base}/livescore/Leaderboard.aspx?t={tid}",
            "skins": f"{base}/livescore/skinsLB.aspx?t={tid}",
            "results": f"{base}/{slug}_tour_pages/results.aspx?id={tid}",
            "roster": f"{base}/{slug}_tour_pages/listing.aspx?id={tid}",
        }

    def _delta(self, iso: str) -> int:
        try:
            return (date.fromisoformat(iso) - self.today).days
        except (TypeError, ValueError):
            return -9999

    # -- derived views ---------------------------------------------------

    @property
    def next_event(self) -> dict | None:
        upcoming = [e for e in self.events if self._delta(e["date"]) >= 0]
        return upcoming[0] if upcoming else None

    @property
    def today_board(self) -> dict | None:
        """Today's livescore board, whatever state it is in."""
        for event in self.events:
            if self._delta(event["date"]) == 0:
                board = self.live.get(event["tid"])
                if board and board.get("live"):
                    return board
        return None

    @property
    def live_now(self) -> dict | None:
        """A board for a round actually being played right now.

        Not merely "the board has rows": that stayed true all evening after
        the last putt. Status comes from the Thru column - someone between 1
        and 17 holes means play is still going.
        """
        board = self.today_board
        return board if board and board.get("status") == "in_progress" else None

    @property
    def latest_board(self) -> dict | None:
        """The most recent livescore board, live or just finished.

        Official results can lag the round by days, so this is what the mirror
        can say about an event you have already played.
        """
        played = [
            (e["date"], self.live[e["tid"]]) for e in self.events
            if self._delta(e["date"]) <= 0 and (self.live.get(e["tid"]) or {}).get("live")
        ]
        return max(played, key=lambda p: p[0])[1] if played else None

    def my_standing(self, player: dict) -> dict | None:
        for flight in self.standings.get("flights", []):
            for row in flight.get("rows", []):
                if row.get("ID") == player["id"]:
                    return {**row, "flight": flight["name"], "flight_short": flight_short(flight["name"])}
        return None

    def my_gap(self, player: dict) -> str:
        """How far ahead of / behind the next player in my flight I am.

        Points arrive as upstream text, so every conversion is guarded - a
        render crash here would take down the whole mirror over a cosmetic line.
        """
        for flight in self.standings.get("flights", []):
            rows = flight.get("rows", [])
            index = next((i for i, r in enumerate(rows) if r.get("ID") == player["id"]), None)
            if index is None:
                continue
            mine = _points(rows[index])
            label = flight_short(flight["name"])
            if mine is None:
                return ""
            if index > 0:
                above = rows[index - 1]
                other = _points(above)
                if other is None:
                    return ""
                return f"{other - mine:,.0f} points behind {above.get('Name')} in {label}."
            if len(rows) > 1:
                below = rows[1]
                other = _points(below)
                if other is None:
                    return ""
                return (f"Leading {label} by {mine - other:,.0f} points "
                        f"over {below.get('Name')}.")
        return ""

    def my_pairing(self, player: dict, tid: str | None) -> tuple[dict | None, list]:
        data = self.pairings.get(tid) if tid else None
        if not data or not data.get("published"):
            return None, []
        mine = next((p for p in data["players"] if p.get("player_id") == player["id"]), None)
        if not mine:
            return None, []
        group = next(
            (g["players"] for g in data["groups"]
             if g["group"] == mine["group"] and g["tee_time"] == mine["tee_time"]),
            [],
        )
        return mine, group

    def my_roster_status(self, player: dict, tid: str | None) -> str:
        """registered / waiting / absent / unknown for the configured player.

        "unknown" is deliberately distinct from "absent": with no roster
        snapshot yet, saying "you are not signed up" would be asserting a
        falsehood about the thing the reader most needs to trust.
        """
        data = self.rosters.get(tid) if tid else None
        if not data or not data.get("available"):
            return "unknown"
        if any(p.get("player_id") == player["id"] for p in data.get("registered", [])):
            return "registered"
        if any(p.get("player_id") == player["id"] for p in data.get("waiting", [])):
            return "waiting"
        return "absent"

    def me_on_board(self, player: dict, board: dict | None) -> dict | None:
        """Find the configured player on a livescore board.

        Matched by name: the board carries no player ids.
        """
        if not board:
            return None
        for flight in board.get("flights", []):
            for row in flight.get("rows", []):
                if row.get("name") == player["name"]:
                    # Board sections are named "B Flight Leaderboard"; the
                    # bare flight letter is what reads well in a summary.
                    return {**row, "flight": flight_short(flight["name"])}
        return None

    def my_live(self, player: dict) -> dict | None:
        return self.me_on_board(player, self.live_now)

    def my_result(self, player: dict, tid: str) -> dict | None:
        data = self.results.get(tid)
        if not data or not data.get("posted"):
            return None
        for flight in data.get("flights", []):
            for row in flight.get("rows", []):
                if row.get("ID") == player["id"]:
                    return {**row, "flight": flight["name"], "flight_short": flight_short(flight["name"])}
        return None

    def played_results(self, player: dict | None = None) -> list[dict]:
        out = []
        for event in self.events:
            data = self.results.get(event["tid"])
            if data and data.get("posted"):
                out.append({"event": event,
                            "me": self.my_result(player, event["tid"]) if player else None})
        out.sort(key=lambda r: r["event"]["date"], reverse=True)
        return out

    def event_status(self, event: dict) -> tuple[str, str]:
        """(label, kind) for the status pill. kind drives its colour.

        For a two-day event this reads across both rounds, so the pill tracks
        whichever round is furthest along.
        """
        for extra in reversed(event.get("rounds", [])):
            if (self.results.get(extra["tid"]) or {}).get("posted")                or (self.live.get(extra["tid"]) or {}).get("live"):
                event = extra
                break
        tid = event["tid"]
        if event.get("is_today") and (self.live.get(tid) or {}).get("live"):
            return "Live", "live"
        if (self.results.get(tid) or {}).get("posted"):
            return "Results", "done"
        if (self.live.get(tid) or {}).get("live"):
            return "Scores", "done"
        if (self.pairings.get(tid) or {}).get("published"):
            return "Tee times", "ready"
        if (self.rosters.get(tid) or {}).get("sold_out"):
            return "Sold out", "full"
        if event.get("registration_open"):
            return "Register", "open"
        return "Scheduled", "idle"

    def group_summary(self) -> list[dict]:
        """One row per configured player, ordered by season points.

        Deliberately cheap: everything here already exists in the snapshots.

        Grouped by flight (Champ down to D), then by points within each -
        flights are handicap bands, so comparing points across them is not
        like-for-like. Anyone without a standings row yet sorts last rather
        than as zero.
        """
        board = self.today_board
        rows = []
        for player in self.players:
            standing = self.my_standing(player)
            today = self.me_on_board(player, board)
            rows.append({
                "slug": player["slug"],
                "name": player["name"],
                "display": _first_last(player["name"]),
                "primary": bool(player.get("primary")),
                "flight": standing["flight_short"] if standing else None,
                "position": standing.get("Position") if standing else None,
                "points": standing.get("Points") if standing else None,
                "events": standing.get("Tournaments") if standing else None,
                "today": today and {"total": today["total"], "thru": today["thru"],
                                    "to_par": today["to_par"], "position": today["position"]},
            })
        def by_flight_then_points(row: dict) -> tuple:
            rank = FLIGHT_ORDER.get(row["flight"], len(FLIGHT_ORDER))
            points = _points({"Points": row["points"]}) if row["points"] else None
            # Ascending flight rank, then points descending, then name so ties
            # are stable rather than dependent on config order.
            return (rank, -(points or 0), row["display"])

        rows.sort(key=by_flight_then_points)
        return rows

    def player_schedule(self, player: dict) -> list[dict]:
        """The season from one player's point of view.

        Combines three snapshots per event: results say whether they played,
        rosters say whether they are entered, pairings give a tee time. Where
        no roster has been crawled yet the status is `unknown` rather than
        "not entered" - claiming they aren't signed up from missing data would
        be asserting a falsehood about the thing they'd check this for.
        """
        out = []
        for event in self.events:
            days = [event, *event["rounds"]]

            # Registration lives on round one - a later round's roster is
            # always empty, which read as "not entered" for an event the
            # player had in fact entered.
            roster = self.rosters.get(event["tid"])
            # Points and earnings land on the final round, so that is the
            # event's outcome. Earlier rounds post a score but zero points.
            result = next((self.my_result(player, d["tid"]) for d in reversed(days)
                           if self.my_result(player, d["tid"])), None)
            posted = any((self.results.get(d["tid"]) or {}).get("posted") for d in days)

            status = "unknown"
            if result:
                status = "played"
            elif posted:
                status = "missed"
            elif roster and roster.get("available"):
                status = self.my_roster_status(player, event["tid"])

            # A tee time is only worth showing while it is still ahead of you;
            # for a two-day event, the next round that has one.
            tee = None
            for day in days:
                if day["is_past"]:
                    continue
                found, _ = self.my_pairing(player, day["tid"])
                if found:
                    tee = found
                    break
            out.append({**event, "status": status, "result": result,
                        "tee_time": tee["tee_time"] if tee else None})
        return out

    def player_timeline(self, player: dict) -> dict:
        """A player's season split by what they'd look for first.

        Same priority as the schedule page: today, then what's coming, then
        history newest-first.
        """
        rows = [e for e in self.player_schedule(player) if not e["round_of"]]
        upcoming = [e for e in rows if not e["is_past"] and not e["is_today"]]
        return {
            "today": [e for e in rows if e["is_today"]],
            # The first upcoming event is already shown in full above, so the
            # list picks up after it rather than repeating it.
            "upcoming": upcoming[1:],
            "played": list(reversed([e for e in rows if e["is_past"]])),
        }

    def render_player(self, player: dict, rel: str, base: str) -> None:
        """A player's dashboard plus their own digest.txt and status.json."""
        next_event = self.next_event
        tee_time, group = self.my_pairing(player, next_event["tid"] if next_event else None)
        standing = self.my_standing(player)
        self._render(
            "player.html", rel, nav="now", me=player,
            today_board=self.today_board,
            me_today=self.me_on_board(player, self.today_board),
            next_event=next_event,
            next_pairings=self.pairings.get(next_event["tid"]) if next_event else None,
            next_roster=self.rosters.get(next_event["tid"]) if next_event else None,
            my_roster_status=self.my_roster_status(player, next_event["tid"]) if next_event else "unknown",
            timeline=self.player_timeline(player),
            my_tee_time=tee_time, my_group=group,
            me_standing=standing, me_gap=self.my_gap(player),
        )
        prefix = f"{base}/" if base else ""
        self._write(f"{prefix}status.json", json.dumps(self.status(player), indent=2) + "\n")
        self._write(f"{prefix}digest.txt", self.digest(player))

    # -- writing ---------------------------------------------------------

    def _write(self, rel: str, text: str) -> None:
        path = PUBLIC / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    NAV_FOR = {
        "index.html": "now", "schedule.html": "schedule", "standings.html": "points",
        "me.html": "players", "feed.html": "updates", "info.html": "info",
    }

    def _render(self, template: str, rel: str, **ctx) -> None:
        depth = rel.count("/")
        prefix = "../" * depth

        def url(target: str) -> str:
            """Link to a page by its file name, extension stripped.

            Cloudflare Pages canonicalises `/schedule.html` to `/schedule` with
            a 308, so linking to the .html name would put a redirect on every
            click and every scheduled fetch. Files are still written as .html;
            only the hrefs are canonical.
            """
            target = target.lstrip("/").removesuffix(".html")
            if target == "index":
                return prefix or "./"
            # A directory index: Pages serves it at the directory itself and
            # 308s anything else, so `p/x/index` must become `p/x/`.
            if target.endswith("/index"):
                return prefix + target[: -len("index")]
            return prefix + target

        self._write(
            rel,
            self.env.get_template(template).render(
                cfg=self.cfg,
                season=self.season,
                date_range=date_range,
                group_ids=self.group_ids,
                group_names=self.group_names,
                players=self.players,
                nav=ctx.pop("nav", None) or self.NAV_FOR.get(rel),
                rel=prefix,
                url=url,
                generated_iso=self.now.isoformat(timespec="seconds"),
                generated_human=self.now.strftime("%b %d, %Y at %I:%M %p %Z"),
                **ctx,
            ),
        )

    def build(self) -> None:
        if PUBLIC.exists():
            shutil.rmtree(PUBLIC)
        PUBLIC.mkdir(parents=True)
        shutil.copytree(HERE / "static", PUBLIC, dirs_exist_ok=True)

        next_event = self.next_event

        # ---- shared pages: one copy, marking everyone in the group --------
        self._render(
            "index.html", "index.html",
            today_board=self.today_board,
            next_event=next_event,
            next_pairings=self.pairings.get(next_event["tid"]) if next_event else None,
            next_roster=self.rosters.get(next_event["tid"]) if next_event else None,
            group=self.group_summary(),
            changes=self.changes,
            recent_results=self.played_results()[:5],
        )

        for event in self.events:
            event["status"], event["status_kind"] = self.event_status(event)
            event["entered"] = [
                x["name"] for x in self.players
                if self.my_roster_status(x, event["tid"]) in ("registered", "waiting")
            ]

        # Ordered by what you'd look for: in progress, then what's coming,
        # then history newest-first - rather than one flat chronological list.
        self._render(
            "schedule.html", "schedule.html",
            today=[e for e in self.events if e["is_today"] and not e["round_of"]],
            upcoming=[e for e in self.events
                      if not e["is_past"] and not e["is_today"] and not e["round_of"]],
            past=list(reversed([e for e in self.events
                                if e["is_past"] and not e["round_of"]])),
            events=self.events,
        )

        columns = [c for c in self.standings.get("columns", []) if c != "Detail"]
        self._render("standings.html", "standings.html",
                     standings=self.standings, columns=columns)

        self._render("feed.html", "feed.html", changes=self.changes)

        # ---- per player: three small artifacts each -----------------------
        for player in self.players:
            self.render_player(player, f"p/{player['slug']}/index.html",
                               f"p/{player['slug']}")
            if player is self.primary:
                # Keep the original URLs working: an existing scheduled check
                # is pointed at /digest.txt, and /me is a bookmark.
                self.render_player(player, "me.html", "")
        self._render("404.html", "404.html")

        for event in self.events:
            tid = event["tid"]
            pairings = self.pairings.get(tid)
            if pairings and pairings.get("groups"):
                for group in pairings["groups"]:
                    group["has_me"] = any(
                        p.get("player_id") in self.group_ids for p in group["players"]
                    )
            results = self.results.get(tid) or {}
            if results.get("columns"):
                results = {**results, "columns": [c for c in results["columns"] if c != "Detail"]}
            head = event["round_of"] or event
            siblings = [head, *head["rounds"]] if head["rounds"] else []
            self._render("event.html", f"t/{tid}.html", nav="schedule",
                         event=event, siblings=siblings, head=head,
                         pairings=pairings,
                         results=results, live=self.live.get(tid),
                         roster=self.rosters.get(tid),
                         skins=self.skins.get(tid),
                         entered=event.get("entered", []))

        index = read_json("content_index.json") or {}
        pages = []
        for entry in index.get("pages") or self.cfg.get("content_pages", []):
            page = read_json(f"content/{entry['id']}.json")
            if not page:
                continue
            page.setdefault("nav_title", entry["title"])
            pages.append(page)
            self._render("info_page.html", f"info/{page['id']}.html", nav="info", page=page)
        self._render("info.html", "info.html", pages=pages)

        # Root status.json / digest.txt belong to the primary player and are
        # written by render_player, so an existing scheduled check keeps working.
        self._write("feed.xml", self.rss())
        self._write("robots.txt", "User-agent: *\nAllow: /\n")
        log.info("rendered %d pages into %s", len(list(PUBLIC.rglob("*.html"))), PUBLIC)

    # -- machine-readable summaries --------------------------------------

    def status(self, player: dict) -> dict:
        """Compact JSON snapshot, shaped for a scheduled ChatGPT check."""
        next_event = self.next_event
        my_tee_time, my_group = self.my_pairing(player, next_event["tid"] if next_event else None)
        me_standing = self.my_standing(player)
        board = self.live_now
        me_live = self.my_live(player)
        last = self.played_results(player)[:1]

        payload = {
            "generated_at": self.now.isoformat(timespec="seconds"),
            "tour": self.cfg["tour_name"],
            "season": self.season,
            "site": self.site_url,
            "player": {"id": player["id"], "name": player["name"], "slug": player["slug"]},
            "live": None,
            "next_event": None,
            "my_standing": None,
            "last_result": None,
            "last_round": None,
            "recent_changes": self.changes[:10],
            "source_last_checked": self.meta.get("last_run"),
        }

        if board:
            payload["live"] = {
                "event": board.get("event", {}).get("name"),
                "course": board.get("event", {}).get("course"),
                "status": board.get("status"),
                "still_on_course": board.get("still_out"),
                "players": board.get("players"),
                "me": me_live and {
                    "position": me_live["position"], "total": me_live["total"],
                    "thru": me_live["thru"], "to_par": me_live["to_par"],
                    "flight": me_live["flight"],
                },
                "leaders": [
                    {"flight": f["name"], "leader": f["rows"][0]["name"],
                     "total": f["rows"][0]["total"], "thru": f["rows"][0]["thru"]}
                    for f in board.get("flights", []) if f.get("rows")
                ],
            }

        if next_event:
            payload["next_event"] = {
                "tid": next_event["tid"],
                "name": next_event["name"],
                "course": next_event["course"],
                "date": next_event["date"],
                "days_away": self._delta(next_event["date"]),
                "start": f"{next_event['start_time']} {next_event['start_type']}".strip(),
                "cost": next_event["cost"],
                "is_major": next_event["is_major"],
                "url": f"{self.site_url}/t/{next_event['tid']}",
                "upstream": self.upstream(next_event["tid"]),
                "tee_times_posted": bool(
                    (self.pairings.get(next_event["tid"]) or {}).get("published")
                ),
                "my_registration": self.my_roster_status(player, next_event["tid"]),
                "field": (lambda r: r and {
                    "filled": r.get("filled_slots"),
                    "total": r.get("total_slots"),
                    "open": r.get("open_slots"),
                    "waiting": r.get("total_waiting"),
                    "sold_out": r.get("sold_out"),
                })(self.rosters.get(next_event["tid"])),
                "my_tee_time": my_tee_time and {
                    "time": my_tee_time["tee_time"],
                    "starting_hole": my_tee_time["starting_hole"],
                    "group": my_tee_time["group"].strip(),
                    "playing_with": [
                        p["name"] for p in my_group if p["player_id"] != player["id"]
                    ],
                },
            }

        if me_standing:
            payload["my_standing"] = {
                "flight": me_standing["flight_short"],
                "position": me_standing.get("Position"),
                "points": me_standing.get("Points"),
                "events_played": me_standing.get("Tournaments"),
                "handicap": me_standing.get("Handicap"),
                "note": self.my_gap(player),
            }

        board = self.latest_board
        if board and not self.live_now:
            me = self.me_on_board(player, board)
            posted = (self.results.get(board.get("tid")) or {}).get("posted")
            if not posted:
                payload["last_round"] = {
                    "event": board.get("event", {}).get("name"),
                    "date": board.get("event", {}).get("date"),
                    "official": False,
                    "me": me and {"position": me["position"], "total": me["total"],
                                  "to_par": me["to_par"], "flight": me["flight"]},
                    "upstream": self.upstream(board.get("tid")),
                }

        if last:
            entry = last[0]
            payload["last_result"] = {
                "event": entry["event"]["name"],
                "date": entry["event"]["date"],
                "me": entry["me"] and {
                    "position": entry["me"].get("Position"),
                    "score": entry["me"].get("Score"),
                    "points": entry["me"].get("Points"),
                    "flight": entry["me"]["flight_short"],
                },
            }
        return payload

    def digest(self, player: dict) -> str:
        """Plain-text briefing - the cheapest thing for an assistant to read."""
        s = self.status(player)
        lines = [
            f"{self.cfg['site_title']} - {_first_last(player['name'])} - status as of "
            f"{self.now.strftime('%b %d, %Y %I:%M %p %Z')}",
            "",
        ]

        if s["live"]:
            lines.append(f"LIVE NOW: {s['live']['event']} at {s['live']['course']}")
            lines.append(f"  {s['live']['still_on_course']} of {s['live']['players']} "
                         f"still on the course.")
            if s["live"]["me"]:
                me = s["live"]["me"]
                lines.append(f"  You: {me['total']} ({me['to_par']}) thru {me['thru']}, "
                             f"position {me['position']} in {me['flight']}")
            for leader in s["live"]["leaders"]:
                lines.append(f"  {leader['flight']}: {leader['leader']} "
                             f"{leader['total']} thru {leader['thru']}")
            lines.append("")

        event = s["next_event"]
        if event:
            lines.append(f"NEXT EVENT: {event['name']}{' (MAJOR)' if event['is_major'] else ''}")
            lines.append(f"  {event['course']}")
            lines.append(f"  {pretty_date(event['date'])} - {event['start']} "
                         f"({event['days_away']} days away)")
            lines.append(f"  Entry {event['cost']}")
            registration = {
                "registered": "  YOU ARE REGISTERED (paid).",
                "waiting": "  YOU ARE ON THE WAITING LIST (signed up, not yet paid).",
                "absent": "  YOU ARE NOT SIGNED UP for this event.",
            }.get(event["my_registration"])
            if registration:
                lines.append(registration)
            if event["field"]:
                field = event["field"]
                lines.append(f"  Field: {field['filled']}/{field['total']} filled, "
                             f"{field['open']} open, {field['waiting']} waiting"
                             f"{' - SOLD OUT' if field['sold_out'] else ''}")
            if event["my_tee_time"]:
                tee = event["my_tee_time"]
                lines.append(f"  YOUR TEE TIME: {tee['time']} off hole "
                             f"{tee['starting_hole']} ({tee['group']})")
                if tee["playing_with"]:
                    lines.append(f"  Playing with: {', '.join(tee['playing_with'])}")
            elif event["tee_times_posted"]:
                lines.append("  Tee times are posted but you are not on the sheet.")
            else:
                lines.append("  TEE TIMES: not posted yet.")
            lines.append(f"  {event['url']}")
            lines.append("")

        if s["my_standing"]:
            st = s["my_standing"]
            lines.append(f"POINTS RACE: #{st['position']} in {st['flight']} with "
                         f"{st['points']} points from {st['events_played']} events "
                         f"(index {st['handicap']}).")
            if st["note"]:
                lines.append(f"  {st['note']}")
            lines.append("")

        if s["last_round"] and s["last_round"]["me"]:
            rnd, me = s["last_round"], s["last_round"]["me"]
            lines.append(f"LAST ROUND (unofficial - from the livescore board): "
                         f"{rnd['event']} ({rnd['date']})")
            lines.append(f"  You: {me['total']} ({me['to_par']}), "
                         f"position {me['position']} in {me['flight']}.")
            lines.append("  Official results not posted yet.")
            lines.append("")

        if s["last_result"] and s["last_result"]["me"]:
            last, me = s["last_result"], s["last_result"]["me"]
            lines.append(f"LAST RESULT: {last['event']} ({last['date']}) - "
                         f"finished {me['position']} in {me['flight']}, "
                         f"{me['score']} for {me['points']} points.")
            lines.append("")

        if s["recent_changes"]:
            lines.append("RECENT CHANGES:")
            for change in s["recent_changes"]:
                stamp = change["ts"][:16].replace("T", " ")
                detail = f" - {change['detail']}" if change["detail"] else ""
                lines.append(f"  [{stamp}Z] {change['title']}{detail}")
            lines.append("")

        lines.append(f"Full mirror: {self.site_url}/")
        return "\n".join(lines) + "\n"

    def rss(self) -> str:
        items = []
        for change in self.changes[:50]:
            try:
                stamp = datetime.fromisoformat(change["ts"])
            except ValueError:
                stamp = self.now
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            path = change.get("url", "").lstrip("/").removesuffix(".html")
            link = f"{self.site_url}/{path}" if path else self.site_url
            title = f"[{change['kind']}] {change['title']}"
            items.append(
                "    <item>\n"
                f"      <title>{xml_escape(title)}</title>\n"
                f"      <link>{xml_escape(link)}</link>\n"
                f"      <guid isPermaLink=\"false\">{xml_escape(change['ts'] + change['title'])}</guid>\n"
                f"      <pubDate>{stamp.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>\n"
                f"      <description>{xml_escape(change.get('detail') or change['title'])}</description>\n"
                "    </item>"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0">\n  <channel>\n'
            f"    <title>{xml_escape(self.cfg['site_title'])} updates</title>\n"
            f"    <link>{xml_escape(self.site_url)}/</link>\n"
            "    <description>Tee times, results and points race changes on the "
            f"{xml_escape(self.cfg['tour_name'])} Golfweek Amateur Tour.</description>\n"
            + "\n".join(items)
            + "\n  </channel>\n</rss>\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render data/ into public/")
    parser.add_argument("--site-url", default=SITE_URL_DEFAULT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s",
                        stream=sys.stdout)
    if not (DATA / "schedule.json").exists():
        log.error("no data/schedule.json - run `python -m scraper.crawl --mode full` first")
        return 1
    Site(load_config(), args.site_url).build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
