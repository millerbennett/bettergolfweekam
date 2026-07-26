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


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A Site backed by a temp data/ dir containing one live event."""
    data, public = tmp_path / "data", tmp_path / "public"
    data.mkdir()
    monkeypatch.setattr(store, "DATA", data)
    monkeypatch.setattr(render_mod, "PUBLIC", public)

    def write(rel, payload):
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    live = sources.fetch_livescore(FakeFetcher(fixture("leaderboard.html")), CFG, TID)
    live.update(tid=TID, event=EVENT)

    pairings = sources.fetch_pairings(FakeFetcher(fixture("pairings.html")), CFG, TID)
    pairings["event"] = EVENT

    standings = sources.fetch_standings(FakeFetcher(fixture("standings.html")), CFG, 2026)

    # A future event too, so "next up" has somewhere to point after today.
    later = {**EVENT, "tid": "17639", "name": "DC Metro Open Championship",
             "date": (TODAY + timedelta(days=13)).isoformat(), "is_major": True,
             "course": "Rock Harbor (Rock)"}

    write("schedule.json", {"season": 2026, "events": [EVENT, later]})
    write("standings.json", standings)
    write("changes.json", [{"ts": "2026-07-25T12:00:00+00:00", "kind": "teetimes",
                           "title": "Tee times posted", "detail": "You are off 1:20 PM",
                           "url": f"/t/{TID}.html"}])
    write("meta.json", {"last_run": "2026-07-26T19:00:00+00:00"})
    write(f"live/{TID}.json", live)
    write(f"pairings/{TID}.json", pairings)

    # The upcoming major: a real field the configured player is NOT in.
    roster = sources.fetch_roster(FakeFetcher(fixture("roster.html")), CFG, later["tid"])
    roster["event"] = later
    write(f"roster/{later['tid']}.json", roster)

    site = Site(SITE_CFG, "https://example.invalid")
    site.build()
    return site, public


def read(public, name):
    return (public / name).read_text(encoding="utf-8")


def test_live_event_surfaces_on_the_dashboard(site):
    _, public = site
    index = read(public, "index.html")
    assert "Playing now" in index
    assert "DC Metro Battlefield Open" in index
    assert "B Flight Leaderboard" in index


def test_dashboard_highlights_my_live_score(site):
    _, public = site
    index = read(public, "index.html")
    # 87, +15, thru 18 - leading B flight in the fixture.
    assert "87" in index and "+ 15" in index
    assert 'class="mine"' in index


def test_status_json_reports_the_live_round(site):
    _, public = site
    status = json.loads(read(public, "status.json"))
    assert status["live"] is not None
    assert status["live"]["event"] == "DC Metro Battlefield Open"
    assert status["live"]["me"]["total"] == "87"
    assert status["live"]["me"]["thru"] == "18"
    assert len(status["live"]["leaders"]) == 5


def test_digest_leads_with_the_live_round(site):
    _, public = site
    digest = read(public, "digest.txt")
    assert "LIVE NOW: DC Metro Battlefield Open" in digest
    assert "You: 87" in digest


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


def test_every_event_gets_a_page(site):
    _, public = site
    assert (public / "t" / f"{TID}.html").exists()
    assert (public / "t" / "17639.html").exists()


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
