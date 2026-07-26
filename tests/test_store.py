"""Snapshot-writing rules.

The workflow gates both its git commit and its Cloudflare deploy on `git diff`
over data/. That makes "does this write touch the file at all" a load-bearing
behaviour, not an implementation detail — so it is pinned here.
"""

from __future__ import annotations

import json

import pytest

from scraper import store


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(store, "DATA", d)
    return d


def test_first_write_creates_the_file(data_dir):
    assert store.save_snapshot("x.json", {"a": 1}) is True
    assert json.loads((data_dir / "x.json").read_text())["a"] == 1


def test_unchanged_content_leaves_the_file_untouched(data_dir):
    """The bug this guards: bumping fetched_at on every poll showed up as a
    git diff, so the cron committed and redeployed even when the tour had
    published nothing."""
    store.save_snapshot("x.json", {"a": 1})
    before = (data_dir / "x.json").read_bytes()

    assert store.save_snapshot("x.json", {"a": 1}) is False
    assert (data_dir / "x.json").read_bytes() == before


def test_changed_content_rewrites_and_restamps(data_dir):
    store.save_snapshot("x.json", {"a": 1})
    first = json.loads((data_dir / "x.json").read_text())["fetched_at"]

    assert store.save_snapshot("x.json", {"a": 2}) is True
    after = json.loads((data_dir / "x.json").read_text())
    assert after["a"] == 2
    assert after["fetched_at"] >= first


def test_fetched_at_tracks_content_change_not_poll_time(data_dir):
    """Last-polled belongs in state.json, which is gitignored and may churn."""
    store.save_snapshot("x.json", {"a": 1})
    stamp = json.loads((data_dir / "x.json").read_text())["fetched_at"]
    for _ in range(3):
        store.save_snapshot("x.json", {"a": 1})
    assert json.loads((data_dir / "x.json").read_text())["fetched_at"] == stamp


def test_nested_paths_are_created(data_dir):
    assert store.save_snapshot("roster/17639.json", {"tid": "17639"}) is True
    assert (data_dir / "roster" / "17639.json").exists()


def test_state_helpers_round_trip(data_dir):
    state = {}
    assert store.age_hours(state, "k") == float("inf")
    store.touch(state, "k")
    assert store.age_hours(state, "k") < 1
    store.save_state(state)
    assert "k" in store.load_state()


def test_change_feed_is_capped(data_dir):
    changes = []
    for i in range(store.MAX_CHANGES + 50):
        store.record_change(changes, "test", f"entry {i}")
    store.save_changes(changes)
    assert len(store.load_changes()) == store.MAX_CHANGES
