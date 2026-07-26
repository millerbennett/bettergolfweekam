"""Pin the parsers against saved copies of the real upstream pages.

The failure mode this guards against is silent: if amateurgolftour.net changes
its markup, the regexes stop matching, every table parses to zero rows, and the
mirror cheerfully renders an empty but valid-looking site. These assert on real
values from the fixtures so that shows up as a red build instead.

Fixtures were captured 2026-07-26. Run with `python -m pytest tests/`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scraper import crawl, sources
from scraper.parse import find_table, select_options

FIXTURES = Path(__file__).parent / "fixtures"

CFG = {
    "base_url": "https://www.amateurgolftour.net",
    "tour_slug": "dc",
    "tour_name": "Washington, DC Metro",
    "season": 2026,
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class FakeFetcher:
    """Serves a canned page for any request, so parsers can be tested offline."""

    def __init__(self, page: str):
        self.page = page
        self.request_count = 0

    def get(self, url):
        self.request_count += 1
        return self.page

    def form_page(self, url, cache=False):
        self.request_count += 1
        return self.page

    def submit(self, url, page, fields):
        self.request_count += 1
        return self.page

    def post_form(self, url, fields):
        self.request_count += 1
        return self.page


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------

def test_schedule_parses_every_event():
    data = sources.fetch_schedule(FakeFetcher(fixture("schedule.html")), CFG, 2026)
    events = data["events"]
    assert len(events) == 21
    assert all(e["tid"] and e["tid"].isdigit() for e in events)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["date"]) for e in events)


def test_schedule_takes_the_id_from_roster_not_the_course_website():
    """The first row's course link carries `?id=988`, which is not a tid."""
    data = sources.fetch_schedule(FakeFetcher(fixture("schedule.html")), CFG, 2026)
    kickoff = data["events"][0]
    assert kickoff["tid"] == "17632"
    assert kickoff["name"] == "Sold-Out - DC Metro Season Kick-off"
    assert kickoff["course"] == "Westfields Golf Club"
    assert kickoff["start_time"] == "11:00"
    assert kickoff["start_type"] == "Straight Tee"
    assert kickoff["cost"] == "$145"


def test_schedule_flags_majors():
    data = sources.fetch_schedule(FakeFetcher(fixture("schedule.html")), CFG, 2026)
    majors = [e for e in data["events"] if e["is_major"]]
    assert len(majors) >= 3
    assert any("Masters" in e["name"] for e in majors)
    assert not data["events"][0]["is_major"]


# --------------------------------------------------------------------------
# Pairings
# --------------------------------------------------------------------------

def test_pairings_groups_players_by_tee_time():
    data = sources.fetch_pairings(FakeFetcher(fixture("pairings.html")), CFG, "17632")
    assert data["published"] is True
    assert len(data["players"]) == 53
    assert len(data["groups"]) == 14
    assert all(len(g["players"]) <= 4 for g in data["groups"])
    assert sum(len(g["players"]) for g in data["groups"]) == 53


def test_pairings_strips_the_id_and_flight_from_display_names():
    data = sources.fetch_pairings(FakeFetcher(fixture("pairings.html")), CFG, "17632")
    first = data["players"][0]
    assert first["player_id"] == "44286"
    assert first["name"] == "Ayubi, Fred"          # from "44286 - Ayubi, Fred (Champ)"
    assert first["flight"] == "Champ"
    assert first["tee_time"] == "11:00 AM"


def test_pairings_groups_are_ordered_by_tee_time():
    data = sources.fetch_pairings(FakeFetcher(fixture("pairings.html")), CFG, "17632")
    times = [sources._time_key(g["tee_time"]) for g in data["groups"]]
    assert times == sorted(times)


def test_unpublished_pairings_are_not_reported_as_posted():
    """An empty sheet renders `<td colspan=7>Empty</td>` and no header row.

    Parsed naively that becomes one blank player, which reads downstream as
    "tee times are posted" and would fire a false notification weeks early.
    """
    data = sources.fetch_pairings(FakeFetcher(fixture("pairings_empty.html")), CFG, "17640")
    assert data["available"] is True
    assert data["published"] is False
    assert data["players"] == []
    assert data["groups"] == []


def test_pairings_skips_tournaments_absent_from_the_dropdown():
    """A combo event run by a neighbouring tour would 500 on POST."""
    fetcher = FakeFetcher(fixture("pairings.html"))
    data = sources.fetch_pairings(fetcher, CFG, "17656")
    assert data["available"] is False
    assert data["published"] is False
    assert fetcher.request_count == 1  # the GET only; never submitted


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

def test_results_splits_into_flights():
    data = sources.fetch_results(FakeFetcher(fixture("results.html")), CFG, "17632")
    assert data["posted"] is True
    assert [f["name"] for f in data["flights"]] == [
        "Championship Flight (0-3.9 Handicap)",
        '"A" Flight (4.0-8.9 Handicap)',
        '"B" Flight (9.0-13.9 Handicap)',
        '"C" Flight (14.0-18.9 Handicap)',
        '"D" Flight (19.0 and Greater Handicap)',
    ]
    assert "Position" in data["columns"] and "Points" in data["columns"]


def test_results_rows_are_keyed_by_column():
    data = sources.fetch_results(FakeFetcher(fixture("results.html")), CFG, "17632")
    winner = data["flights"][0]["rows"][0]
    assert winner["Position"] == "1"
    assert winner["ID"] == "44286"
    assert winner["Name"] == "Ayubi, Fred"
    assert winner["Score"] == "76"


# --------------------------------------------------------------------------
# Roster
# --------------------------------------------------------------------------

def test_roster_reads_capacity_and_both_player_lists():
    data = sources.fetch_roster(FakeFetcher(fixture("roster.html")), CFG, "17639")
    assert data["available"] is True
    assert data["total_slots"] == 80
    assert data["filled_slots"] == 8
    assert data["open_slots"] == 72
    assert data["sold_out"] is False
    assert len(data["registered"]) == 8
    assert len(data["waiting"]) == 18
    assert data["total_waiting"] == 18


def test_roster_flight_counts_are_keyed_by_flight_not_position():
    data = sources.fetch_roster(FakeFetcher(fixture("roster.html")), CFG, "17639")
    assert data["filled_by_flight"] == {"Champ": "1", "A": "1", "B": "0", "C": "4", "D": "2"}
    assert data["waiting_by_flight"] == {"Champ": "0", "A": "2", "B": "4", "C": "7", "D": "5"}
    # The two summary tables must not be swapped: 8 filled vs 18 waiting.
    assert sum(int(v) for v in data["filled_by_flight"].values()) == data["filled_slots"]


def test_roster_separates_paid_from_waiting():
    """Waiting means signed up but not yet paid - every waiting row says No."""
    data = sources.fetch_roster(FakeFetcher(fixture("roster.html")), CFG, "17639")
    assert all(p["paid_tournament"] == "Yes" for p in data["registered"])
    assert all(p["paid_tournament"] == "No" for p in data["waiting"])
    assert data["registered"][0]["name"] == "Devine, Ben"
    assert data["registered"][0]["flight"] == "Champ"


def test_roster_keeps_players_from_visiting_tours():
    data = sources.fetch_roster(FakeFetcher(fixture("roster.html")), CFG, "17639")
    tours = {p["tour"] for p in data["waiting"]}
    assert "Chicago, IL" in tours and "Washington, DC Metro" in tours


def test_empty_roster_parses_as_an_open_field():
    data = sources.fetch_roster(FakeFetcher(fixture("roster_empty.html")), CFG, "17640")
    assert data["available"] is True
    assert data["registered"] == [] and data["waiting"] == []
    assert data["total_slots"] == 80 and data["open_slots"] == 80
    assert data["sold_out"] is False


@pytest.mark.parametrize(
    "payload,expected",
    [({"registered": [{"player_id": "51002"}], "waiting": []}, "registered"),
     ({"registered": [], "waiting": [{"player_id": "51002"}]}, "waiting"),
     ({"registered": [{"player_id": "999"}], "waiting": []}, "absent"),
     ({}, "absent")],
)
def test_roster_status_lookup(payload, expected):
    assert crawl._roster_status(payload, "51002") == expected


# --------------------------------------------------------------------------
# Standings
# --------------------------------------------------------------------------

def test_standings_covers_all_five_flights():
    data = sources.fetch_standings(FakeFetcher(fixture("standings.html")), CFG, 2026)
    assert len(data["flights"]) == 5
    assert sum(len(f["rows"]) for f in data["flights"]) == 91


def test_standings_finds_the_configured_player():
    data = sources.fetch_standings(FakeFetcher(fixture("standings.html")), CFG, 2026)
    rows = [r for f in data["flights"] for r in f["rows"] if r["ID"] == "51002"]
    assert len(rows) == 1
    assert rows[0]["Name"] == "Miller, Bennett"
    assert rows[0]["Position"] == "1"


# --------------------------------------------------------------------------
# Livescore
# --------------------------------------------------------------------------

def test_livescore_parses_flight_leaderboards():
    data = sources.fetch_livescore(FakeFetcher(fixture("leaderboard.html")), CFG, "17602")
    assert data["available"] is True
    assert data["live"] is True
    assert data["date"] == "07/25/2026"
    names = [f["name"] for f in data["flights"]]
    assert names == [
        "Champ Flight Leaderboard", "A Flight Leaderboard", "B Flight Leaderboard",
        "C Flight Leaderboard", "D Flight Leaderboard",
    ]


def test_livescore_strips_tour_and_flight_from_player_names():
    data = sources.fetch_livescore(FakeFetcher(fixture("leaderboard.html")), CFG, "17602")
    b_flight = next(f for f in data["flights"] if f["name"].startswith("B "))
    leader = b_flight["rows"][0]
    assert leader["name"] == "Miller, Bennett"   # from "Miller, Bennett - Washington, DC Metro - B"
    assert leader["position"] == "1"
    assert leader["total"] == "87"
    assert leader["thru"] == "18"


def test_livescore_ignores_cutline_separator_rows():
    """The board injects `<td class='cutline'>` spacers between placings."""
    data = sources.fetch_livescore(FakeFetcher(fixture("leaderboard.html")), CFG, "17602")
    for flight in data["flights"]:
        assert all(row["name"] for row in flight["rows"])
        assert all(row["position"] for row in flight["rows"])


def test_livescore_refuses_ids_outside_the_dropdown_window():
    """Posting one would trip __EVENTVALIDATION and 500."""
    fetcher = FakeFetcher(fixture("leaderboard.html"))
    data = sources.fetch_livescore(fetcher, CFG, "17632")
    assert data["available"] is False
    assert fetcher.request_count == 1


# --------------------------------------------------------------------------
# Skins / CTP
# --------------------------------------------------------------------------

def test_skins_parses_every_game_on_the_card():
    """One event runs a Super Skins, a per-flight skins game, and a CTP pot."""
    data = sources.fetch_skins(FakeFetcher(fixture("skins.html")), CFG, "17602")
    assert data["available"] is True
    titles = [g["title"] for g in data["games"]]
    assert len(titles) == 6
    assert any(t.startswith("Super Skins") for t in titles)
    assert any("B Flight" in t for t in titles)
    assert any(t.startswith("CTP") for t in titles)


def test_skins_lists_only_holes_that_were_won():
    """Tied holes render as blank rows and roll the pot over."""
    data = sources.fetch_skins(FakeFetcher(fixture("skins.html")), CFG, "17602")
    b_flight = next(g for g in data["games"] if "B Flight" in g["title"])
    won = [h for h in b_flight["holes"] if h["player"]]
    assert len(won) == 2
    assert {h["hole"] for h in won} == {"11", "18"}
    assert won[0]["player"] == "Hawkins, Kevin"
    assert won[0]["type"] == "Birdie"
    assert won[0]["paid_out"] == "Yes"


def test_skins_captures_the_pot_maths():
    data = sources.fetch_skins(FakeFetcher(fixture("skins.html")), CFG, "17602")
    b_flight = next(g for g in data["games"] if "B Flight" in g["title"])
    assert b_flight["summary"]["Total Players"] == "3"
    assert b_flight["summary"]["Total Skins Pot"] == "$30"
    assert b_flight["summary"]["Total Skins"] == "2"
    assert b_flight["summary"]["Each Skin Value"] == "$15"


def test_ctp_game_has_a_pot_but_no_holes():
    data = sources.fetch_skins(FakeFetcher(fixture("skins.html")), CFG, "17602")
    ctp = next(g for g in data["games"] if g["title"].startswith("CTP"))
    assert ctp["holes"] == []
    assert ctp["summary"]["Total CTP Pot"] == "$260"


def test_skins_refuses_ids_outside_the_dropdown_window():
    fetcher = FakeFetcher(fixture("skins.html"))
    data = sources.fetch_skins(fetcher, CFG, "99999")
    assert data["available"] is False
    assert fetcher.request_count == 1


def test_skins_won_lookup_names_the_hole_and_value():
    data = sources.fetch_skins(FakeFetcher(fixture("skins.html")), CFG, "17602")
    won = crawl._skins_won(data, "Hawkins, Kevin")
    assert any("hole 11" in w and "$15" in w for w in won)
    assert crawl._skins_won(data, "Miller, Bennett") == set()


# --------------------------------------------------------------------------
# Change feed
# --------------------------------------------------------------------------

def test_change_preview_drops_a_repeated_opening_phrase():
    """Mirrored pages repeat their title as both h1 and h2."""
    assert crawl._first_sentence(
        "Weather Policy Weather Policy Official tournament weather procedures"
    ) == "Weather Policy Official tournament weather procedures"


def test_change_preview_leaves_unrepeated_text_alone():
    assert crawl._first_sentence("No repetition here at all") == "No repetition here at all"
    assert crawl._first_sentence("") == ""


def test_change_preview_is_truncated():
    preview = crawl._first_sentence("word " * 200)
    assert len(preview) <= 113 and preview.endswith("...")


def test_record_change_suppresses_an_immediate_duplicate():
    """A flapping upstream page should not re-announce itself every run."""
    changes = []
    for _ in range(3):
        crawl.record_change(changes, "teetimes", "Tee times posted", "9:00 AM")
    assert len(changes) == 1


def test_record_change_allows_the_same_title_with_new_detail():
    changes = []
    crawl.record_change(changes, "teetimes", "Tee times posted", "9:00 AM")
    crawl.record_change(changes, "teetimes", "Tee times posted", "9:10 AM")
    assert len(changes) == 2


# --------------------------------------------------------------------------
# Shared parsing helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [("11:00 AM", (11, 0)), ("1:00 PM", (13, 0)), ("12:30 PM", (12, 30)),
     ("12:15 AM", (0, 15)), ("", (99, 99))],
)
def test_time_key_orders_am_pm_correctly(name, expected):
    assert sources._time_key(name) == expected


def test_absolutise_rewrites_only_relative_urls():
    base = "https://www.amateurgolftour.net/dc_tour_pages/"
    out = sources._absolutise(
        '<a href="Pairings.aspx">a</a><img src="/images/x.jpg">'
        '<a href="https://example.com">b</a><a href="#top">c</a>',
        base,
    )
    assert 'href="https://www.amateurgolftour.net/dc_tour_pages/Pairings.aspx"' in out
    assert 'src="https://www.amateurgolftour.net/images/x.jpg"' in out
    assert 'href="https://example.com"' in out
    assert 'href="#top"' in out


def test_select_options_are_deduplicated():
    """A posted-back page repeats the selected option; callers want it once."""
    options = select_options(fixture("results.html"), "tournament_dd")
    values = [v for v, _ in options]
    assert len(values) == len(set(values))


def test_find_table_stops_at_the_closing_tag():
    table = find_table(fixture("schedule.html"), css_class="schedule-table")
    assert table is not None and "</table>" not in table
