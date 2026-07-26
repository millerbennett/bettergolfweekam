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
        self.player_id = cfg["player"]["id"]

        self.schedule = read_json("schedule.json", default={"events": []}) or {"events": []}
        self.standings = read_json("standings.json", default={"flights": []}) or {"flights": []}
        self.changes = read_json("changes.json", default=[]) or []
        self.meta = read_json("meta.json", default={}) or {}

        self.events = [e for e in self.schedule.get("events", []) if e.get("tid")]
        for event in self.events:
            delta = self._delta(event["date"])
            event["is_past"] = delta < 0
            event["is_today"] = delta == 0

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
    def live_now(self) -> dict | None:
        """A board for an event being played right now."""
        for event in self.events:
            if self._delta(event["date"]) != 0:
                continue
            board = self.live.get(event["tid"])
            if board and board.get("live"):
                return board
        return None

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

    def my_standing(self) -> dict | None:
        for flight in self.standings.get("flights", []):
            for row in flight.get("rows", []):
                if row.get("ID") == self.player_id:
                    return {**row, "flight": flight["name"], "flight_short": flight_short(flight["name"])}
        return None

    def my_gap(self) -> str:
        """How far ahead of / behind the next player in my flight I am.

        Points arrive as upstream text, so every conversion is guarded - a
        render crash here would take down the whole mirror over a cosmetic line.
        """
        for flight in self.standings.get("flights", []):
            rows = flight.get("rows", [])
            index = next((i for i, r in enumerate(rows) if r.get("ID") == self.player_id), None)
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

    def my_pairing(self, tid: str | None) -> tuple[dict | None, list]:
        data = self.pairings.get(tid) if tid else None
        if not data or not data.get("published"):
            return None, []
        mine = next((p for p in data["players"] if p.get("player_id") == self.player_id), None)
        if not mine:
            return None, []
        group = next(
            (g["players"] for g in data["groups"]
             if g["group"] == mine["group"] and g["tee_time"] == mine["tee_time"]),
            [],
        )
        return mine, group

    def my_roster_status(self, tid: str | None) -> str:
        """registered / waiting / absent / unknown for the configured player.

        "unknown" is deliberately distinct from "absent": with no roster
        snapshot yet, saying "you are not signed up" would be asserting a
        falsehood about the thing the reader most needs to trust.
        """
        data = self.rosters.get(tid) if tid else None
        if not data or not data.get("available"):
            return "unknown"
        if any(p.get("player_id") == self.player_id for p in data.get("registered", [])):
            return "registered"
        if any(p.get("player_id") == self.player_id for p in data.get("waiting", [])):
            return "waiting"
        return "absent"

    def me_on_board(self, board: dict | None) -> dict | None:
        """Find the configured player on a livescore board.

        Matched by name: the board carries no player ids.
        """
        if not board:
            return None
        for flight in board.get("flights", []):
            for row in flight.get("rows", []):
                if row.get("name") == self.cfg["player"]["name"]:
                    # Board sections are named "B Flight Leaderboard"; the
                    # bare flight letter is what reads well in a summary.
                    return {**row, "flight": flight_short(flight["name"])}
        return None

    def my_live(self) -> dict | None:
        return self.me_on_board(self.live_now)

    def my_result(self, tid: str) -> dict | None:
        data = self.results.get(tid)
        if not data or not data.get("posted"):
            return None
        for flight in data.get("flights", []):
            for row in flight.get("rows", []):
                if row.get("ID") == self.player_id:
                    return {**row, "flight": flight["name"], "flight_short": flight_short(flight["name"])}
        return None

    def played_results(self) -> list[dict]:
        out = []
        for event in self.events:
            data = self.results.get(event["tid"])
            if data and data.get("posted"):
                out.append({"event": event, "me": self.my_result(event["tid"])})
        out.sort(key=lambda r: r["event"]["date"], reverse=True)
        return out

    def event_status(self, event: dict) -> str:
        tid = event["tid"]
        if (self.results.get(tid) or {}).get("posted"):
            return "Results"
        if (self.live.get(tid) or {}).get("live"):
            return "LIVE"
        if (self.pairings.get(tid) or {}).get("published"):
            return "Tee times"
        if (self.rosters.get(tid) or {}).get("sold_out"):
            return "Sold out"
        if event.get("registration_open"):
            return "Register"
        return "Scheduled"

    # -- writing ---------------------------------------------------------

    def _write(self, rel: str, text: str) -> None:
        path = PUBLIC / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

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
            return prefix + target

        self._write(
            rel,
            self.env.get_template(template).render(
                cfg=self.cfg,
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
        my_tee_time, my_group = self.my_pairing(next_event["tid"] if next_event else None)
        me_standing = self.my_standing()

        self._render(
            "index.html", "index.html",
            live=self.live_now,
            me_live=self.my_live(),
            next_event=next_event,
            next_pairings=self.pairings.get(next_event["tid"]) if next_event else None,
            next_roster=self.rosters.get(next_event["tid"]) if next_event else None,
            my_roster_status=self.my_roster_status(next_event["tid"] if next_event else None),
            my_tee_time=my_tee_time,
            my_group=my_group,
            me_standing=me_standing,
            me_gap=self.my_gap(),
            changes=self.changes,
            recent_results=self.played_results()[:5],
        )

        for event in self.events:
            event["status"] = self.event_status(event)
        self._render("schedule.html", "schedule.html", events=self.events)

        columns = [c for c in self.standings.get("columns", []) if c != "Detail"]
        self._render("standings.html", "standings.html",
                     standings=self.standings, columns=columns)

        my_results = []
        for entry in self.played_results():
            if entry["me"]:
                my_results.append({**entry["me"], "event": entry["event"]})
        self._render("me.html", "me.html",
                     me_standing=me_standing, me_gap=self.my_gap(),
                     next_event=next_event, my_tee_time=my_tee_time, my_group=my_group,
                     my_results=my_results)

        self._render("feed.html", "feed.html", changes=self.changes)
        self._render("404.html", "404.html")

        for event in self.events:
            tid = event["tid"]
            pairings = self.pairings.get(tid)
            if pairings and pairings.get("groups"):
                for group in pairings["groups"]:
                    group["has_me"] = any(
                        p.get("player_id") == self.player_id for p in group["players"]
                    )
            results = self.results.get(tid) or {}
            if results.get("columns"):
                results = {**results, "columns": [c for c in results["columns"] if c != "Detail"]}
            self._render("event.html", f"t/{tid}.html",
                         event=event, pairings=pairings,
                         results=results, live=self.live.get(tid),
                         roster=self.rosters.get(tid),
                         skins=self.skins.get(tid),
                         my_roster_status=self.my_roster_status(tid))

        pages = []
        for entry in self.cfg["content_pages"]:
            page = read_json(f"content/{entry['id']}.json")
            if not page:
                continue
            page.setdefault("nav_title", entry["title"])
            pages.append(page)
            self._render("info_page.html", f"info/{page['id']}.html", page=page)
        self._render("info.html", "info.html", pages=pages)

        self._write("status.json", json.dumps(self.status(), indent=2) + "\n")
        self._write("digest.txt", self.digest())
        self._write("feed.xml", self.rss())
        self._write("robots.txt", "User-agent: *\nAllow: /\n")
        log.info("rendered %d pages into %s", len(list(PUBLIC.rglob("*.html"))), PUBLIC)

    # -- machine-readable summaries --------------------------------------

    def status(self) -> dict:
        """Compact JSON snapshot, shaped for a scheduled ChatGPT check."""
        next_event = self.next_event
        my_tee_time, my_group = self.my_pairing(next_event["tid"] if next_event else None)
        me_standing = self.my_standing()
        board = self.live_now
        me_live = self.my_live()
        last = self.played_results()[:1]

        payload = {
            "generated_at": self.now.isoformat(timespec="seconds"),
            "tour": self.cfg["tour_name"],
            "season": self.cfg["season"],
            "site": self.site_url,
            "player": {"id": self.player_id, "name": self.cfg["player"]["name"]},
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
                "my_registration": self.my_roster_status(next_event["tid"]),
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
                        p["name"] for p in my_group if p["player_id"] != self.player_id
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
                "note": self.my_gap(),
            }

        board = self.latest_board
        if board and not self.live_now:
            me = self.me_on_board(board)
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

    def digest(self) -> str:
        """Plain-text briefing - the cheapest thing for an assistant to read."""
        s = self.status()
        lines = [
            f"{self.cfg['site_title']} - status as of "
            f"{self.now.strftime('%b %d, %Y %I:%M %p %Z')}",
            "",
        ]

        if s["live"]:
            lines.append(f"LIVE NOW: {s['live']['event']} at {s['live']['course']}")
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
