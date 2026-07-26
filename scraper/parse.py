"""HTML table helpers.

Every data page on amateurgolftour.net renders one of three shapes:

  * `<table class='schedule-table'>` - schedule, results, standings. Flight
    section headings are `<tr class='schedule-table-regional'><td colspan=N>`.
  * `<table id="reports_grid">` - tee times / pairings.
  * `<span id="lblLeaderBoard">` rows - livescore, with `lbHoleHeader` marking
    each flight section.

All of them are flat enough that regex extraction is more predictable than a
DOM parser here, and it keeps CI dependency-free beyond requests + jinja2.
"""

from __future__ import annotations

import html as html_lib
import re

TAG_RE = re.compile(r"<[^>]+>")
ROW_RE = re.compile(r"<tr\b(?P<attrs>[^>]*)>(?P<body>.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<(?P<tag>t[dh])\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>", re.S | re.I)


def clean(fragment: str) -> str:
    """Strip tags and collapse whitespace, keeping <br> as a newline."""
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = TAG_RE.sub("", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def first_link(fragment: str) -> str | None:
    match = re.search(r"""href=['"]([^'"]+)['"]""", fragment, re.I)
    return html_lib.unescape(match.group(1)) if match else None


class Row:
    __slots__ = ("cells", "raw_cells", "css", "is_header", "colspan")

    def __init__(self, attrs: str, body: str):
        css = re.search(r"""class=['"]([^'"]*)['"]""", attrs, re.I)
        self.css = css.group(1) if css else ""
        matches = list(CELL_RE.finditer(body))
        self.raw_cells = [m.group("body") for m in matches]
        self.cells = [clean(c) for c in self.raw_cells]
        self.is_header = bool(matches) and all(m.group("tag").lower() == "th" for m in matches)
        self.colspan = 0
        if len(matches) == 1:
            span = re.search(r"""colspan=['"]?(\d+)""", matches[0].group("attrs"), re.I)
            self.colspan = int(span.group(1)) if span else 0

    @property
    def is_section(self) -> bool:
        """A single wide cell acting as a flight/section heading."""
        return self.colspan >= 2 and bool(self.cells and self.cells[0])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Row({self.cells!r})"


def rows_of(table_html: str) -> list[Row]:
    return [Row(m.group("attrs"), m.group("body")) for m in ROW_RE.finditer(table_html)]


def find_table(page: str, *, css_class: str | None = None, table_id: str | None = None) -> str | None:
    """Return the inner HTML of the first matching table.

    Tables here never nest, so scanning to the next `</table>` is safe.
    """
    if table_id:
        pattern = r"""<table[^>]*id=['"]%s['"][^>]*>""" % re.escape(table_id)
    elif css_class:
        pattern = r"""<table[^>]*class=['"][^'"]*%s[^'"]*['"][^>]*>""" % re.escape(css_class)
    else:
        pattern = r"<table\b[^>]*>"
    match = re.search(pattern, page, re.I)
    if not match:
        return None
    end = page.find("</table>", match.end())
    return page[match.end(): end if end != -1 else len(page)]


def find_all_tables(page: str, *, css_class: str | None = None) -> list[str]:
    """Inner HTML of every matching table, in document order.

    The roster page stacks four of them (capacity, registered, waiting summary,
    waiting list) under the same class, so callers classify by header content.
    """
    pattern = (
        r"""<table[^>]*class=['"][^'"]*%s[^'"]*['"][^>]*>""" % re.escape(css_class)
        if css_class else r"<table\b[^>]*>"
    )
    tables = []
    for match in re.finditer(pattern, page, re.I):
        end = page.find("</table>", match.end())
        tables.append(page[match.end(): end if end != -1 else len(page)])
    return tables


def sectioned_table(table_html: str) -> tuple[list[str], list[dict]]:
    """Parse a table whose rows are grouped under colspan section headings.

    Returns (column headers, list of {section, values, raw}) records.
    """
    headers: list[str] = []
    records: list[dict] = []
    section = ""
    for row in rows_of(table_html):
        if row.is_header and not headers:
            headers = row.cells
            continue
        if row.is_section:
            section = row.cells[0]
            continue
        if not row.cells or not any(row.cells):
            continue
        records.append({"section": section, "values": row.cells, "raw": row.raw_cells})
    return headers, records


def zip_record(headers: list[str], values: list[str]) -> dict[str, str]:
    return {h: (values[i] if i < len(values) else "") for i, h in enumerate(headers)}


def selected_option(page: str, name: str) -> str | None:
    """The value the server marked `selected` in a <select>.

    The schedule and standings dropdowns both mark the live season this way,
    which is what lets the crawler follow a season rollover on its own instead
    of waiting for someone to edit config.json every January.
    """
    match = re.search(
        r"""<select[^>]*(?:name|id)=['"]%s['"][^>]*>(.*?)</select>""" % re.escape(name),
        page, re.S | re.I,
    )
    if not match:
        return None
    option = re.search(
        r"""<option[^>]*\s+selected[^>]*value=['"]([^'"]*)['"]|"""
        r"""<option[^>]*value=['"]([^'"]*)['"][^>]*\s+selected""",
        match.group(1), re.I,
    )
    if not option:
        return None
    return option.group(1) or option.group(2)


def select_options(page: str, name: str) -> list[tuple[str, str]]:
    """Return (value, label) pairs for a <select> by name or id."""
    match = re.search(
        r"""<select[^>]*(?:name|id)=['"]%s['"][^>]*>(.*?)</select>""" % re.escape(name),
        page,
        re.S | re.I,
    )
    if not match:
        return []
    seen: set[str] = set()
    options = []
    for value, label in re.findall(
        r"""<option[^>]*value=['"]([^'"]*)['"][^>]*>(.*?)</option>""", match.group(1), re.S | re.I
    ):
        if value in seen:
            continue
        seen.add(value)
        options.append((value, clean(label)))
    return options
