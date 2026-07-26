"""Crawl orchestrator.

`--mode auto` (what cron runs) decides what is worth fetching from the schedule
and the freshness stamps in data/state.json, so most runs make one or two
requests and exit. That keeps us inside the site's 10s crawl delay without
long-running jobs, and keeps commits (and therefore Cloudflare deploys) rare.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import sources
from .net import Fetcher
from .store import (
    age_hours,
    load_changes,
    load_config,
    load_state,
    now_utc,
    read_json,
    record_change,
    save_changes,
    save_snapshot,
    save_state,
    touch,
    write_json,
)

log = logging.getLogger("crawl")


def local_today(cfg: dict) -> date:
    return datetime.now(ZoneInfo(cfg["timezone"])).date()


def days_until(iso: str, today: date) -> int:
    return (date.fromisoformat(iso) - today).days


class Crawler:
    def __init__(self, cfg: dict, mode: str, force: bool = False):
        self.cfg = cfg
        self.mode = mode
        self.force = force or mode == "full"
        self.fetcher = Fetcher(crawl_delay=cfg.get("crawl_delay_seconds", 10))
        self.state = load_state()
        self.changes = load_changes()
        self.today = local_today(cfg)
        self.fresh = cfg["freshness"]
        self.changed = False
        self.errors: list[str] = []

    # -- helpers ---------------------------------------------------------

    def due(self, key: str, max_age_hours: float) -> bool:
        return self.force or age_hours(self.state, key) >= max_age_hours

    def attempt(self, label: str, fn, *args) -> None:
        """Run one crawl unit, isolating its failure from the rest of the run.

        A single flaky page should not cost us the other data we already
        fetched this run, nor leave state.json unwritten.
        """
        try:
            fn(*args)
        except Exception as exc:
            log.error("%s failed: %s", label, exc)
            self.errors.append(f"{label}: {exc}")

    def event_label(self, event: dict) -> str:
        return f"{event['name']} @ {event['course']}"

    def save(self, rel: str, payload: dict, key: str) -> bool:
        changed = save_snapshot(rel, payload)
        touch(self.state, key)
        self.changed = self.changed or changed
        return changed

    # -- units of work ---------------------------------------------------

    def crawl_schedule(self) -> None:
        season = self.cfg["season"]
        previous = read_json("schedule.json") or {}
        prior_by_tid = {e.get("tid"): e for e in previous.get("events", [])}

        payload = sources.fetch_schedule(self.fetcher, self.cfg, season)
        changed = self.save("schedule.json", payload, "schedule")
        if not changed or not prior_by_tid:
            return

        for event in payload["events"]:
            before = prior_by_tid.get(event["tid"])
            if before is None:
                record_change(self.changes, "schedule", f"New event: {self.event_label(event)}",
                              f"{event['date']} - {event['start_time']} {event['start_type']}",
                              f"/t/{event['tid']}.html")
                continue
            if before.get("date") != event["date"]:
                record_change(self.changes, "schedule", f"Date changed: {event['name']}",
                              f"{before.get('date')} -> {event['date']}", f"/t/{event['tid']}.html")
            if before.get("start_time") != event["start_time"]:
                record_change(self.changes, "schedule", f"Start time changed: {event['name']}",
                              f"{before.get('start_time')} -> {event['start_time']}",
                              f"/t/{event['tid']}.html")
            if not before.get("registration_open") and event.get("registration_open"):
                record_change(self.changes, "registration", f"Registration open: {event['name']}",
                              f"{event['date']} - {event['cost']}", f"/t/{event['tid']}.html")

    def crawl_standings(self) -> None:
        season = self.cfg["season"]
        previous = read_json("standings.json") or {}
        payload = sources.fetch_standings(self.fetcher, self.cfg, season)
        if not self.save("standings.json", payload, "standings"):
            return

        me = self.cfg["player"]["id"]
        before = _find_player_standing(previous, me)
        after = _find_player_standing(payload, me)
        if after and before and (before.get("Position"), before.get("Points")) != (
            after.get("Position"), after.get("Points")
        ):
            record_change(self.changes, "standings", "Your points race position updated",
                          f"{after.get('flight', '')}: #{before.get('Position')} "
                          f"({before.get('Points')} pts) -> #{after.get('Position')} "
                          f"({after.get('Points')} pts)", "/me.html")
        else:
            record_change(self.changes, "standings", "Points race updated", "", "/standings.html")

    def crawl_pairings(self, event: dict) -> None:
        tid = event["tid"]
        rel = f"pairings/{tid}.json"
        previous = read_json(rel) or {}
        payload = sources.fetch_pairings(self.fetcher, self.cfg, tid)
        payload["event"] = _event_stub(event)
        changed = self.save(rel, payload, f"pairings:{tid}")
        if not changed:
            return

        if payload["published"] and not previous.get("published"):
            mine = _find_player_pairing(payload, self.cfg["player"]["id"])
            detail = f"{len(payload['groups'])} groups posted"
            if mine:
                detail = (f"You are off {mine['tee_time']} from hole "
                          f"{mine['starting_hole']} ({mine['group'].strip()})")
            record_change(self.changes, "teetimes", f"Tee times posted: {self.event_label(event)}",
                          detail, f"/t/{tid}.html")
        elif payload["published"]:
            record_change(self.changes, "teetimes", f"Tee times updated: {self.event_label(event)}",
                          "", f"/t/{tid}.html")

    def crawl_results(self, event: dict) -> None:
        tid = event["tid"]
        rel = f"results/{tid}.json"
        previous = read_json(rel) or {}
        payload = sources.fetch_results(self.fetcher, self.cfg, tid)
        payload["event"] = _event_stub(event)
        changed = self.save(rel, payload, f"results:{tid}")
        if not changed:
            return

        if payload["posted"] and not previous.get("posted"):
            mine = _find_player_result(payload, self.cfg["player"]["id"])
            detail = ""
            if mine:
                detail = (f"You finished {mine.get('Position')} in {mine.get('flight', '')} "
                          f"({mine.get('Score')}, {mine.get('Points')} pts)")
            record_change(self.changes, "results", f"Results posted: {self.event_label(event)}",
                          detail, f"/t/{tid}.html")
        elif payload["posted"]:
            record_change(self.changes, "results", f"Results updated: {self.event_label(event)}",
                          "", f"/t/{tid}.html")

    def crawl_roster(self, event: dict) -> None:
        tid = event["tid"]
        rel = f"roster/{tid}.json"
        previous = read_json(rel) or {}
        payload = sources.fetch_roster(self.fetcher, self.cfg, tid)
        payload["event"] = _event_stub(event)
        if not self.save(rel, payload, f"roster:{tid}"):
            return
        if not previous:
            return  # first sighting is not news

        was, now = _roster_status(previous, self.cfg["player"]["id"]), \
            _roster_status(payload, self.cfg["player"]["id"])
        if was != now:
            wording = {
                "registered": "You are registered (paid) for",
                "waiting": "You are on the waiting list for",
                "absent": "You are no longer listed for",
            }[now]
            record_change(self.changes, "roster", f"{wording} {self.event_label(event)}",
                          f"{payload['filled_slots']} of {payload['total_slots']} slots filled, "
                          f"{payload['total_waiting']} waiting", f"/t/{tid}.html")

        if payload.get("sold_out") and not previous.get("sold_out"):
            record_change(self.changes, "roster", f"Sold out: {self.event_label(event)}",
                          f"{payload['total_waiting']} on the waiting list", f"/t/{tid}.html")
        elif previous.get("sold_out") and payload.get("sold_out") is False:
            record_change(self.changes, "roster", f"Spots opened: {self.event_label(event)}",
                          f"{payload['open_slots']} now open", f"/t/{tid}.html")

    def crawl_live(self, event: dict) -> None:
        tid = event["tid"]
        rel = f"live/{tid}.json"
        previous = read_json(rel) or {}
        payload = sources.fetch_livescore(self.fetcher, self.cfg, tid)
        if not payload.get("available"):
            touch(self.state, f"live:{tid}")
            return

        payload["tid"] = tid
        payload["event"] = _event_stub(event)
        changed = self.save(rel, payload, f"live:{tid}")
        if changed and payload["live"] and not previous.get("live"):
            record_change(self.changes, "live", f"Scoring underway: {self.event_label(event)}",
                          "", f"/t/{tid}.html")

    def crawl_content(self) -> None:
        for page in self.cfg["content_pages"]:
            page_id = page["id"]
            key = f"content:{page_id}"
            if not self.due(key, self.fresh["content_hours"]):
                continue
            rel = f"content/{page_id}.json"
            previous = read_json(rel) or {}
            payload = sources.fetch_content(self.fetcher, self.cfg, page_id)
            payload["nav_title"] = page["title"]
            if self.save(rel, payload, key) and previous:
                record_change(self.changes, "announcement", f"Page updated: {page['title']}",
                              _first_sentence(payload.get("text", "")), f"/info/{page_id}.html")

    # -- planner ---------------------------------------------------------

    def run(self) -> None:
        if self.mode in ("full", "auto", "daily"):
            if self.due("schedule", self.fresh["schedule_hours"]):
                self.attempt("schedule", self.crawl_schedule)

        schedule = read_json("schedule.json") or {"events": []}
        events = [e for e in schedule.get("events", []) if e.get("tid")]

        if self.mode == "full":
            # One-off backfill: every event's pairings and results, so the mirror
            # carries the whole season rather than just the current window. This
            # supersedes the windowed passes below, which would otherwise refetch
            # the same pages (--force makes everything look due).
            for event in events:
                self.attempt(f"pairings {event['tid']}", self.crawl_pairings, event)
                if days_until(event["date"], self.today) >= 0:
                    self.attempt(f"roster {event['tid']}", self.crawl_roster, event)
                else:
                    self.attempt(f"results {event['tid']}", self.crawl_results, event)
        else:
            # Tee times: poll hard in the days before an event, since they drop
            # without warning and that is the whole reason this mirror exists.
            upcoming = [
                e for e in events
                if 0 <= days_until(e["date"], self.today) <= self.fresh["pairings_window_days"]
            ]
            for event in upcoming:
                known = read_json(f"pairings/{event['tid']}.json") or {}
                if known.get("available") is False:
                    continue  # not on this tour's sheet; polling it will never help
                max_age = 24.0 if known.get("published") else self.fresh["pairings_minutes"] / 60.0
                if self.due(f"pairings:{event['tid']}", max_age):
                    self.attempt(f"pairings {event['tid']}", self.crawl_pairings, event)

            # Rosters: who is in the field and how much room is left. Worth
            # watching further out than tee times, since that is the window
            # where you would still decide to sign up.
            for event in events:
                away = days_until(event["date"], self.today)
                if not 0 <= away <= self.fresh["roster_window_days"]:
                    continue
                if self.due(f"roster:{event['tid']}", self.fresh["roster_hours"]):
                    self.attempt(f"roster {event['tid']}", self.crawl_roster, event)

            # Results for anything recently played that has not posted yet.
            recent = [
                e for e in events
                if -self.fresh["results_window_days"] <= days_until(e["date"], self.today) <= 0
            ]
            for event in recent:
                posted = (read_json(f"results/{event['tid']}.json") or {}).get("posted")
                max_age = 24.0 * 30 if posted else self.fresh["results_recent_hours"]
                if self.due(f"results:{event['tid']}", max_age):
                    self.attempt(f"results {event['tid']}", self.crawl_results, event)

        # Live scoring on the day of an event.
        if self.mode in ("full", "auto", "live"):
            for event in [e for e in events if days_until(e["date"], self.today) == 0]:
                self.attempt(f"live {event['tid']}", self.crawl_live, event)

        if self.mode in ("full", "daily") or self.due("standings", self.fresh["standings_hours"]):
            self.attempt("standings", self.crawl_standings)

        if self.mode in ("full", "auto", "daily"):
            self.attempt("content", self.crawl_content)

        save_state(self.state)
        save_changes(self.changes)
        write_json(
            "meta.json",
            {
                "last_run": now_utc().isoformat(timespec="seconds"),
                "mode": self.mode,
                "requests": self.fetcher.request_count,
                "content_changed": self.changed,
                "errors": self.errors,
                "source": f"{self.cfg['base_url']}/{self.cfg['tour_slug']}_tour_pages/default.aspx",
            },
        )
        log.info("done: mode=%s requests=%d changed=%s errors=%d",
                 self.mode, self.fetcher.request_count, self.changed, len(self.errors))


# --------------------------------------------------------------------------
# Lookups used for the personalised change-feed lines
# --------------------------------------------------------------------------

def _event_stub(event: dict) -> dict:
    return {k: event.get(k) for k in ("tid", "date", "name", "course", "start_time",
                                      "start_type", "is_major")}


def _find_player_standing(payload: dict, player_id: str) -> dict | None:
    for flight in payload.get("flights", []):
        for row in flight.get("rows", []):
            if row.get("ID") == player_id:
                return {**row, "flight": flight["name"]}
    return None


def _find_player_result(payload: dict, player_id: str) -> dict | None:
    for flight in payload.get("flights", []):
        for row in flight.get("rows", []):
            if row.get("ID") == player_id:
                return {**row, "flight": flight["name"]}
    return None


def _roster_status(payload: dict, player_id: str) -> str:
    """Where the configured player sits on a roster: registered/waiting/absent."""
    if any(p.get("player_id") == player_id for p in payload.get("registered", [])):
        return "registered"
    if any(p.get("player_id") == player_id for p in payload.get("waiting", [])):
        return "waiting"
    return "absent"


def _find_player_pairing(payload: dict, player_id: str) -> dict | None:
    for player in payload.get("players", []):
        if player.get("player_id") == player_id:
            return player
    return None


def _first_sentence(text: str, limit: int = 110) -> str:
    """A short preview for the change feed.

    These pages repeat their title as both h1 and h2, so drop a leading
    duplicate rather than showing the same words three times in one feed line.
    """
    words = text.split()
    # Longest immediately-repeated opening phrase wins, so "Refund Policy
    # Refund Policy Players must..." previews as "Players must...".
    for size in range(min(8, len(words) // 2), 0, -1):
        if words[:size] == words[size:size * 2]:
            words = words[size:]
            break
    preview = " ".join(words)
    return preview[:limit] + ("..." if len(preview) > limit else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl the Golfweek Am Tour site into data/")
    parser.add_argument("--mode", default="auto", choices=["auto", "full", "daily", "live"])
    parser.add_argument("--force", action="store_true", help="ignore freshness stamps")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    Crawler(load_config(), args.mode, force=args.force).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
