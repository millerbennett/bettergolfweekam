"""Captured 2026 championship boards plus edge cases for 54-hole scoring.

Fixtures captured October 17 during round two. Altered rows below are synthetic
and exercise missing data, season rollover, and the boundaries between rounds.
"""

import json
from datetime import date

import pytest

from build.nationals import championship_view
from scraper import nationals, store
from scraper.crawl import Crawler
from tests.test_parsers import CFG, FakeFetcher, fixture
from tests.test_render import SITE_CFG


class ChampionshipFetcher(FakeFetcher):
    def get(self, url):
        self.last_url = url
        if "Schedule" in url:
            return fixture("nationals_schedule.html")
        return fixture("nationals_index.html")


def board(flight="A"):
    return nationals.fetch_board(FakeFetcher(fixture(f"nationals_{flight.lower()}.html")), CFG,
                                 {"season": 2026}, {"name": flight, "tid": "18314" if flight == "A" else "18324"})


def seed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    schedule = nationals.discover(ChampionshipFetcher(""), CFG)
    store.save_snapshot("nationals/schedule.json", schedule)
    for flight in ("A", "B"):
        store.save_snapshot(f"nationals/2026/{flight.lower()}.json", board(flight))
    return schedule


def test_discovery_combines_course_pools_without_double_counting():
    fetcher = ChampionshipFetcher("")
    data = nationals.discover(fetcher, CFG)
    assert data["start_date"] == "2026-10-16"
    assert data["end_date"] == "2026-10-18"
    assert len(data["rounds"]) == 21  # excludes skins and two-man side events
    assert [(f["name"], f["tid"]) for f in data["flights"]] == [
        ("Champ", "18313"), ("A", "18314"), ("B", "18324"), ("C", "18326"), ("D", "18327")]
    assert len([r for r in data["rounds"] if r["flight"] == "A"]) == 6


def test_real_championship_columns_and_tracked_scores():
    data = board()
    assert len(data["rows"]) == 153
    garrett = next(r for r in data["rows"] if r["name"] == "Brown, Garrett")
    assert garrett["position"] == "T91"
    assert garrett["rounds"] == ["89", "7", ""]
    assert garrett["total"] == "96"
    assert garrett["to_par"] == "+ 17"
    assert garrett["holes"] == 20
    assert garrett["status"] == "in_progress"
    assert garrett["progress"] == "R2 · thru 2 of 18"
    thomas = next(r for r in board("B")["rows"] if r["name"] == "LeMense, Thomas")
    assert thomas["tour"] == "Eastern NC"


@pytest.mark.parametrize("thru,status,progress", [
    ("0", "not_started", "Not started"),
    ("18", "between_rounds", "R1 complete · 18 holes"),
    ("36", "between_rounds", "R2 complete · 36 holes"),
    ("37", "in_progress", "R3 · thru 1 of 18"),
    ("54", "complete", "Finished · 54 holes"),
    ("F", "unknown", "F"), ("WD", "unknown", "WD"),
    ("", "unknown", "Awaiting scores"),
])
def test_progress_does_not_call_18_holes_a_finished_championship(thru, status, progress):
    assert nationals.progress(thru, ["80", "", ""])["status"] == status
    assert nationals.progress(thru, ["80", "", ""])["progress"] == progress
    assert nationals.progress("F", ["80", "81", "82"])["holes"] == 54


@pytest.mark.parametrize("season,flight", [(2025, "A"), (2026, "B")])
def test_reject_wrong_season_or_wrong_board(season, flight):
    with pytest.raises(RuntimeError, match="wrong championship"):
        nationals.fetch_board(FakeFetcher(fixture("nationals_a.html")), CFG,
                              {"season": season}, {"name": flight, "tid": "18314"})


def test_reject_local_board_and_changed_championship_columns():
    for html in [fixture("leaderboard.html"), fixture("nationals_a.html").replace(
            "<td>R1</td>", "<td>Round 1</td>")]:
        with pytest.raises(RuntimeError):
            nationals.fetch_board(FakeFetcher(html), CFG, {"season": 2026}, {"name": "A", "tid": "18314"})


def test_unchanged_board_does_not_change_snapshot_stamp(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    path = tmp_path / "nationals/2026/a.json"
    previous = path.read_bytes()
    assert store.save_snapshot("nationals/2026/a.json", board()) is False
    assert path.read_bytes() == previous


def test_discovery_preserves_verified_ids_when_dropdown_retires(tmp_path, monkeypatch):
    schedule = seed(tmp_path, monkeypatch)
    cfg = {**SITE_CFG, "nationals": {"enabled": True}}
    crawler = Crawler(cfg, "nationals")
    crawler.today = date(2026, 11, 1)
    discovered = {**schedule, "flights": [{"name": n, "tid": None} for n in nationals.FLIGHTS]}
    monkeypatch.setattr(nationals, "discover", lambda *args: discovered)
    crawler.crawl_nationals()
    assert store.read_json("nationals/schedule.json")["flights"] == schedule["flights"]
    # A season rollover must not borrow last year's tournament ids.
    discovered = {**discovered, "season": 2027,
                  "flights": [{"name": n, "tid": None} for n in nationals.FLIGHTS]}
    crawler.force = True
    crawler.crawl_nationals()
    assert all(f["tid"] is None for f in store.read_json("nationals/schedule.json")["flights"])


def test_group_and_dc_views_preserve_upstream_positions(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    cfg = {**SITE_CFG, "players": [
        {"name": "Brown, Garrett", "id": "57627", "slug": "garrett-brown"},
        {"name": "LeMense, Thomas", "id": "48801", "slug": "thomas-lemense"},
        {"name": "Miller, Bennett", "id": "51002", "slug": "bennett-miller"}]}
    view = championship_view(cfg, date(2026, 10, 17))
    assert view["available_flights"] == 2
    assert view["active"]
    assert len(view["group"]) == 2
    assert view["group"][0]["position"] == "T91"
    assert view["group"][1]["is_dc"] is False
    assert [p["slug"] for p in view["not_on_board"]] == ["bennett-miller"]
    assert all(r["tour"] == "Washington, DC Metro" for f in view["flights"] for r in f["dc"])
    assert championship_view(cfg, date(2026, 10, 20))["status"] == "Latest available scores"
    # A stale or mismatched snapshot cannot populate a new year's page.
    wrong = {**board(), "season": 2025}
    store.save_snapshot("nationals/2026/a.json", wrong)
    assert championship_view(cfg, date(2026, 10, 17))["available_flights"] == 1


def test_crawler_isolates_failed_flight_and_preserves_good_scores(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    cfg = {**SITE_CFG, "nationals": {"enabled": True}, "crawl_delay_seconds": 10}
    crawler = Crawler(cfg, "nationals")
    crawler.today = date(2026, 10, 17)
    store.touch(crawler.state, "nationals:discovery")
    old = (tmp_path / "nationals/2026/a.json").read_bytes()
    called = []
    boards = {name: board(name) for name in ("A", "B")}

    def fetch(fetcher, cfg, schedule, flight):
        called.append(flight["name"])
        if flight["name"] == "A":
            return {**boards["A"], "rows": []}
        if flight["name"] == "B":
            return boards["B"]
        raise RuntimeError("temporary upstream outage")

    monkeypatch.setattr(nationals, "fetch_board", fetch)
    crawler.crawl_nationals()
    assert called == list(nationals.FLIGHTS)
    assert len(crawler.errors) == 4
    assert (tmp_path / "nationals/2026/a.json").read_bytes() == old
    assert "nationals:2026:B" in crawler.state
    assert "nationals:2026:A" not in crawler.state


@pytest.mark.parametrize("today,expected", [(date(2026, 10, 17), 5), (date(2026, 11, 1), 0), (date(2026, 8, 1), 0)])
def test_polling_window(tmp_path, monkeypatch, today, expected):
    seed(tmp_path, monkeypatch)
    crawler = Crawler({**SITE_CFG, "nationals": {"enabled": True}}, "auto")
    crawler.today = today
    store.touch(crawler.state, "nationals:discovery")
    calls = []
    monkeypatch.setattr(crawler, "crawl_nationals_board", lambda *args: calls.append(args))
    crawler.crawl_nationals()
    assert len(calls) == expected


def test_render_includes_all_flights_without_javascript(tmp_path, monkeypatch):
    import build.render as render
    data = tmp_path / "data"
    data.mkdir()
    seed(data, monkeypatch)
    public = tmp_path / "public"
    monkeypatch.setattr(render, "PUBLIC", public)
    site = render.Site(SITE_CFG, "https://example.test")
    site.build()
    html = (public / "nationals.html").read_text(encoding="utf-8")
    for flight in nationals.FLIGHTS:
        assert f'id="flight-{flight.lower()}"' in html
    assert "Stephen Okoba" in html
    assert "Full A flight standings · 153 players" in html
    assert "Thru / 54" in html
    assert "R2 · thru 3 of 18" in html
    assert "<script" not in html
    assert 'href="nationals"' in (public / "index.html").read_text(encoding="utf-8")
    status = json.loads((public / "status.json").read_text())
    assert status["nationals"]["group"][0]["name"] == "Okoba, Stephen"
    assert "NATIONALS:" in (public / "p/stephen-okoba/digest.txt").read_text(encoding="utf-8")
    assert json.loads((public / "nationals.json").read_text())["available_flights"] == 2
