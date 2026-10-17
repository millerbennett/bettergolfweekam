from unittest.mock import Mock

import pytest
import requests

from scraper.net import Fetcher


@pytest.fixture
def client(monkeypatch):
    # Use a fake monotonic clock so pacing is tested without any real waits/network.
    clock = {"now": 100.0, "sleeps": []}
    monkeypatch.setattr("scraper.net.time.monotonic", lambda: clock["now"])
    def sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr("scraper.net.time.sleep", sleep)
    fetcher = Fetcher(crawl_delay=0)
    response = requests.Response()
    response.status_code = 200
    response._content = b"ok"
    fetcher.session.request = Mock(return_value=response)
    return fetcher, clock, response


def test_ten_seconds_is_a_floor_even_if_config_is_lowered(client):
    fetcher, clock, _ = client
    fetcher.get("https://example.invalid/a")
    fetcher.get("https://example.invalid/b")
    assert clock["sleeps"] == [10]


@pytest.mark.parametrize("status", [403, 429, 503])
def test_overload_or_block_response_stops_all_further_pages(client, status):
    fetcher, _, response = client
    response.status_code = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        fetcher.get("https://example.invalid/a")
    with pytest.raises(RuntimeError, match="stopping this crawl"):
        fetcher.get("https://example.invalid/b")
    assert fetcher.session.request.call_count == 1


def test_repeated_network_errors_open_the_circuit(client):
    fetcher, clock, _ = client
    starts = []
    def failure(*args, **kwargs):
        starts.append(clock["now"])
        raise requests.Timeout("timeout")
    fetcher.session.request.side_effect = failure
    with pytest.raises(RuntimeError):
        fetcher.get("https://example.invalid/a")
    with pytest.raises(RuntimeError, match="stopping this crawl"):
        fetcher.get("https://example.invalid/b")
    assert len(starts) == 3
    assert all(b - a >= 10 for a, b in zip(starts, starts[1:]))


def test_request_budget_includes_retries_and_stops_followup_pages(client):
    fetcher, _, _ = client
    fetcher.max_requests = 2
    fetcher.session.request.side_effect = requests.Timeout("timeout")
    with pytest.raises(RuntimeError, match="budget exhausted"):
        fetcher.get("https://example.invalid/a")
    with pytest.raises(RuntimeError, match="budget exhausted"):
        fetcher.get("https://example.invalid/b")
    assert fetcher.request_count == fetcher.session.request.call_count == 2


def test_missing_page_is_not_retried_but_other_pages_can_load(client):
    fetcher, _, response = client
    response.status_code = 404
    with pytest.raises(requests.HTTPError):
        fetcher.get("https://example.invalid/missing")
    response.status_code = 200
    assert fetcher.get("https://example.invalid/next") == "ok"
    assert fetcher.session.request.call_count == 2
