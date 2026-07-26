"""Render tests, driven by a synthetic data/ directory.

The live-scoring path only executes on the ~18 days a year the tour actually
plays, so a break in it would sit undetected until the morning it matters.
These tests fabricate a tournament happening "today" and assert the dashboard,
digest and status output all reflect it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import build.render as render_mod
from build.render import Site, flight_short
from scraper import sources, store
from tests.test_parsers import CFG, FakeFetcher, fixture

LATER_TID = "17639"
TIMEZONE = "America/New_York"
# Must match how Site derives its own "today", or this drifts a day out on a
# UTC CI runner in the evening and the live-event assertions go flaky.
TODAY = datetime.now(ZoneInfo(TIMEZONE)).date()
TID = "17602"

PLAYER = {"id": "51002", "name": "Miller, Bennett", "flight": "B"}

SITE_CFG = {
    **CFG,
    "tour_short": "DC Metro",
    "site_title": "DC Metro Golfweek Am Tour",
    "timezone": TIMEZONE,
    "player": PLAYER,
    "freshness": {"pairings_minutes": 45, "pairings_window_days": 5},
    "content_pages": [],
}

EVENT = {
    "tid": TID,
    "date": TODAY.isoformat(),
    "name": "DC Metro Battlefield Open",
    "course": "Stonewall Golf Club",
    "start_time": "1:00",
    "start_type": "Straight Tee",
    "cost": "$160",
    "is_major": False,
    "course_url": None,
    "register_url": None,
    "registration_open": False,
}


def build_site(tmp_path, monkeypatch, board_fixture="leaderboard.html"):
    """A Site backed by a temp data/ dir containing today's event."""
    data, public = tmp_path / "data", tmp_path / "public"
    data.mkdir()
    monkeypatch.setattr(store, "DATA", data)
    monkeypatch.setattr(render_mod, "PUBLIC", public)

    def write(rel, payload):
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    live = sources.fetch_livescore(FakeFetcher(fixture(board_fixture)), CFG, TID)
    live.update(tid=TID, event=EVENT)

    pairings = sources.fetch_pairings(FakeFetcher(fixture("pairings.html")), CFG, TID)
    pairings["event"] = EVENT

    standings = sources.fetch_standings(FakeFetcher(fixture("standings.html")), CFG, 2026)

    # A future event too, so "next up" has somewhere to point after today.
    later = {**EVENT, "tid": "17639", "name": "DC Metro Open Championship",
             "date": (TODAY + timedelta(days=13)).isoformat(), "is_major": True,
             "course": "Rock Harbor (Rock)", "registration_open": True,
             "register_url": "https://www.amateurgolftour.net/register/"
                             "tournamentReg.aspx?tournament=17639"}

    # An already-played event too, so all three schedule sections exist.
    earlier = {**EVENT, "tid": "17601", "name": "DC Metro Battle on the Ridge",
               "date": (TODAY - timedelta(days=43)).isoformat(),
               "course": "Blue Ridge Shadows Golf Club", "is_major": False}

    write("schedule.json", {"season": 2026, "events": [earlier, EVENT, later]})
    write("standings.json", standings)
    write("changes.json", [{"ts": "2026-07-25T12:00:00+00:00", "kind": "teetimes",
                           "title": "Tee times posted", "detail": "You are off 1:20 PM",
                           "url": f"/t/{TID}.html"}])
    write("meta.json", {"last_run": "2026-07-26T19:00:00+00:00"})
    write(f"live/{TID}.json", live)
    write(f"pairings/{TID}.json", pairings)

    skins = sources.fetch_skins(FakeFetcher(fixture("skins.html")), CFG, TID)
    skins["event"] = EVENT
    write(f"skins/{TID}.json", skins)

    # The upcoming major: a real field the configured player is NOT in.
    roster = sources.fetch_roster(FakeFetcher(fixture("roster.html")), CFG, later["tid"])
    roster["event"] = later
    write(f"roster/{later['tid']}.json", roster)

    site = Site(SITE_CFG, "https://example.invalid")
    site.build()
    return site, public


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Today's event, already finished — every card in."""
    return build_site(tmp_path, monkeypatch)


@pytest.fixture
def live_site(tmp_path, monkeypatch):
    """Today's event mid-round, with players still on the course."""
    return build_site(tmp_path, monkeypatch, "leaderboard_live.html")


def read(public, name):
    return (public / name).read_text(encoding="utf-8")


def test_round_in_progress_says_playing_now(live_site):
    _, public = live_site
    index = read(public, "index.html")
    assert "Playing now" in index
    assert "still on the course" in index
    assert "B Flight Leaderboard" in index


def test_finished_round_is_not_called_live(site):
    """Every card is in. Previously "has rows" counted as live, so the
    dashboard claimed play was underway all evening after the last putt."""
    _, public = site
    index = read(public, "index.html")
    assert "Playing now" not in index
    assert "Today's round — final" in index
    assert "B Flight Leaderboard" in index      # the board still shows


def test_todays_board_is_the_first_thing_on_the_page(site):
    _, public = site
    index = read(public, "index.html")
    assert index.index("Today's round") < index.index("Next up")


def test_dashboard_highlights_my_live_score(site):
    _, public = site
    index = read(public, "index.html")
    # 87, +15, thru 18 - leading B flight in the fixture.
    assert "87" in index and "+ 15" in index
    assert 'class="mine"' in index


def test_status_json_reports_the_live_round(live_site):
    _, public = live_site
    status = json.loads(read(public, "status.json"))
    assert status["live"] is not None
    assert status["live"]["event"] == "DC Metro Battlefield Open"
    assert status["live"]["status"] == "in_progress"
    assert status["live"]["still_on_course"] > 0
    assert len(status["live"]["leaders"]) == 5


def test_status_json_omits_live_once_the_round_is_over(site):
    _, public = site
    status = json.loads(read(public, "status.json"))
    assert status["live"] is None
    assert status["last_round"]["me"]["total"] == "87"


def test_digest_leads_with_the_live_round(live_site):
    _, public = live_site
    digest = read(public, "digest.txt")
    assert "LIVE NOW: DC Metro Battlefield Open" in digest
    assert "still on the course" in digest


def test_digest_does_not_claim_live_after_the_last_putt(site):
    _, public = site
    digest = read(public, "digest.txt")
    assert "LIVE NOW" not in digest
    assert "LAST ROUND" in digest


def test_status_json_reports_my_tee_time(site):
    _, public = site
    status = json.loads(read(public, "status.json"))
    tee = status["next_event"]["my_tee_time"]
    assert status["next_event"]["tee_times_posted"] is True
    assert tee is not None
    assert tee["time"] and tee["starting_hole"]
    assert PLAYER["name"] not in tee["playing_with"]


def test_status_json_reports_my_points_position(site):
    _, public = site
    status = json.loads(read(public, "status.json"))
    assert status["my_standing"]["flight"] == "B"
    assert status["my_standing"]["position"] == "1"
    assert "Leading B by" in status["my_standing"]["note"]


def test_event_page_lists_the_field_and_waiting_list(site):
    """The signal worth having early: an entry deadline you have not met."""
    _, public = site
    page = read(public, f"t/{LATER_TID}.html")
    assert "Devine, Ben" in page          # registered
    assert "Rosen, Jason" in page         # waiting list
    assert "not signed up" in page
    assert "8/80" in page and "72" in page


def test_missing_roster_reads_as_unknown_not_absent(site):
    """Today's event has no roster snapshot.

    Reporting "you are not signed up" from absent data would assert a
    falsehood about the exact thing the reader needs to trust.
    """
    _, public = site
    status = json.loads(read(public, "status.json"))
    assert status["next_event"]["my_registration"] == "unknown"
    assert status["next_event"]["field"] is None
    assert "NOT SIGNED UP" not in read(public, "digest.txt")


def test_event_without_roster_data_omits_the_field_section(site):
    """Today's event has no roster file; the section must not render empty."""
    _, public = site
    assert "<h2>Field</h2>" not in read(public, f"t/{TID}.html")


def test_skins_render_on_the_event_page(site):
    _, public = site
    page = read(public, f"t/{TID}.html")
    assert "Skins" in page
    assert "Hawkins, Kevin" in page      # won hole 11 in B flight
    assert "$30" in page                 # B flight pot


def test_finished_round_is_reported_before_official_results(site):
    """Official results can lag the round by days.

    The livescore board is the only record in that gap, so the digest must be
    able to report it - clearly labelled unofficial.
    """
    _, public = site
    site_obj, _ = site
    board = site_obj.latest_board
    assert board is not None
    me = site_obj.me_on_board(board)
    assert me["total"] == "87" and me["position"] == "1" and me["flight"] == "B"


def test_schedule_is_ordered_by_relevance_not_chronology(site):
    """Happening now, then what's coming, then history newest-first."""
    _, public = site
    page = read(public, "schedule.html")
    now, soon, past = (page.find("Happening today"), page.find("Coming up"),
                       page.find("Played"))
    assert -1 < now < soon < past


def test_schedule_uses_no_table(site):
    """The 6-column table forced a horizontal scrollbar even on desktop."""
    _, public = site
    page = read(public, "schedule.html")
    assert "<table" not in page
    assert 'class="ev-hit"' in page


def test_schedule_omits_a_badge_for_the_default_state(site):
    """A "Scheduled" badge on every row is noise and steals name width."""
    _, public = site
    assert "tag-idle" not in read(public, "schedule.html")


def test_upstream_links_are_chips_not_inline_text(site):
    """They were 17px tall and unhittable on a phone."""
    _, public = site
    page = read(public, f"t/{TID}.html")
    assert page.count('class="chip"') == 4
    assert "Verify upstream (deep links" not in page


def test_homepage_has_no_redundant_updates_button(site):
    _, public = site
    assert "All updates" not in read(public, "index.html")


REGISTER_URL = "https://www.amateurgolftour.net/register/tournamentReg.aspx?tournament=17639"


def test_open_event_gets_a_real_register_link(site):
    """The schedule showed a "Register" badge but never linked anywhere."""
    _, public = site
    for page in ("schedule.html", f"t/{LATER_TID}.html"):
        assert REGISTER_URL in read(public, page), f"missing on {page}"


def test_homepage_cta_follows_the_next_event_not_the_open_one(site):
    """"Next up" here is today's event, whose entry has long closed.

    Advertising a Register button for a different event under that heading
    would point at the wrong tournament.
    """
    _, public = site
    assert "tournamentReg.aspx" not in read(public, "index.html")


def test_register_button_is_not_offered_once_registered(site):
    """The played event is one I'm entered for; only the state should show."""
    _, public = site
    page = read(public, f"t/{TID}.html")
    assert "tournamentReg.aspx" not in page


def test_schedule_row_keeps_two_independent_links(site):
    """Row body opens the event; the button goes to registration.

    A row-wide <a> can't contain a second <a>, so the title stretches its hit
    area over the row instead.
    """
    _, public = site
    page = read(public, "schedule.html")
    assert 'class="ev-name stretch"' in page
    assert 'class="btn-mini"' in page
    assert "<a class=\"ev-hit\"" not in page


def test_every_event_gets_a_page(site):
    _, public = site
    for tid in (TID, LATER_TID, "17601"):
        assert (public / "t" / f"{tid}.html").exists()


def test_nested_pages_resolve_shared_assets(site):
    _, public = site
    assert 'href="../style.css"' in read(public, f"t/{TID}.html")
    assert 'href="style.css"' in read(public, "index.html")


def test_page_links_omit_the_html_extension(site):
    """Cloudflare Pages 308s /schedule.html -> /schedule.

    Linking to the .html name would put a redirect on every click and every
    scheduled fetch, so hrefs are canonical even though files keep .html names.
    """
    import re
    _, public = site
    for name in ("index.html", f"t/{TID}.html", "me.html"):
        page = read(public, name)
        internal = re.findall(r'href="(?!https?:|data:|mailto:)([^"]+)"', page)
        offenders = [h for h in internal if h.endswith(".html")]
        assert not offenders, f"{name} links to {offenders}"
        assert any(h.rstrip("/").endswith("schedule") for h in internal)


def test_index_links_to_itself_as_root(site):
    _, public = site
    assert 'href="./"' in read(public, "index.html")
    assert 'href="../"' in read(public, f"t/{TID}.html")


def test_advertised_urls_are_canonical(site):
    _, public = site
    status = json.loads(read(public, "status.json"))
    assert not status["next_event"]["url"].endswith(".html")
    assert ".html" not in read(public, "feed.xml")
    assert ".html" not in read(public, "digest.txt")


def test_feed_is_well_formed_xml(site):
    import xml.dom.minidom
    _, public = site
    xml.dom.minidom.parseString(read(public, "feed.xml"))


def test_headers_file_ships_with_the_site(site):
    """Cloudflare Pages needs it at the root, or digest.txt gets cached stale."""
    _, public = site
    headers = read(public, "_headers")
    assert "/digest.txt" in headers
    assert "must-revalidate" in headers


@pytest.mark.parametrize(
    "raw,expected",
    [('"B" Flight (9.0-13.9 Handicap)', "B"),
     ("Championship Flight (0-3.9 Handicap)", "Champ"),
     ('"D" Flight (19.0 and Greater Handicap)', "D")],
)
def test_flight_short_labels(raw, expected):
    assert flight_short(raw) == expected
