"""Polite HTTP client for amateurgolftour.net.

robots.txt on the origin asks for `Crawl-Delay: 10`, so every request goes
through a shared rate limiter. The site is ASP.NET WebForms: the dropdowns on
Pairings/Leaderboard are ordinary form POSTs carrying __VIEWSTATE, so we GET the
page to harvest the hidden fields, then POST them back with the selection.
"""

from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "bettergolfweekam/1.0 (personal read-only mirror of a tour member's own "
    "schedule; +https://github.com/millerbennett/bettergolfweekam)"
)

HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


class Fetcher:
    def __init__(self, crawl_delay: float = 10.0, timeout: int = 45, retries: int = 3):
        self.crawl_delay = crawl_delay
        self.timeout = timeout
        self.retries = retries
        self._last_request = 0.0
        self.request_count = 0
        self._form_cache: dict[str, str] = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def _wait(self) -> None:
        gap = time.monotonic() - self._last_request
        if self._last_request and gap < self.crawl_delay:
            time.sleep(self.crawl_delay - gap)
        self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kwargs) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            self.request_count += 1
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                log.info("%s %s -> %s (%d bytes)", method, url, resp.status_code, len(resp.content))
                return resp.text
            except Exception as exc:  # network blip, 5xx, timeout
                last_error = exc
                log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt + 1, self.retries, exc)
                time.sleep(2 ** attempt * 5)
        raise RuntimeError(f"{method} {url} failed after {self.retries} attempts") from last_error

    def get(self, url: str) -> str:
        return self._request("GET", url)

    def form_page(self, url: str, cache: bool = False) -> str:
        """GET a WebForms page so its hidden state (and dropdowns) can be read.

        With `cache=True` the unposted page is reused for the rest of the run.
        Its __VIEWSTATE/__EVENTVALIDATION pair stays valid across repeated POSTs
        (they are not single-use nonces here), which halves the request count
        when walking a dropdown over many tournaments - and at a 10s crawl
        delay that is the difference between a 7-minute and a 14-minute crawl.
        """
        if cache and url in self._form_cache:
            return self._form_cache[url]
        page = self._request("GET", url)
        if cache:
            self._form_cache[url] = page
        return page

    def submit(self, url: str, page: str, fields: dict[str, str]) -> str:
        """POST `fields` back to `url`, carrying the hidden state from `page`.

        ASP.NET validates the submitted value against __EVENTVALIDATION, so a
        selection that is not present in `page`'s own dropdown returns HTTP 500.
        Callers should check the options first.
        """
        data = {name: extract_hidden(page, name) for name in HIDDEN_FIELDS}
        data = {k: v for k, v in data.items() if v is not None}
        data.update(fields)
        return self._request(
            "POST",
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url},
        )

    def post_form(self, url: str, fields: dict[str, str]) -> str:
        """GET `url` for its hidden WebForms state, then POST `fields` back."""
        return self.submit(url, self.form_page(url), fields)


def extract_hidden(html: str, name: str) -> str | None:
    match = re.search(
        r'<input[^>]*name="%s"[^>]*value="([^"]*)"' % re.escape(name), html, re.I
    )
    if not match:
        match = re.search(
            r'<input[^>]*value="([^"]*)"[^>]*name="%s"' % re.escape(name), html, re.I
        )
    return match.group(1) if match else None
