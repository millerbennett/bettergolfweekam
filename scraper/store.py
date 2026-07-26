"""Reading/writing the committed JSON snapshots under data/."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PUBLIC = ROOT / "public"

MAX_CHANGES = 300


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def read_json(rel: str, default=None):
    path = DATA / rel
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(rel: str, payload) -> None:
    path = DATA / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def save_snapshot(rel: str, payload: dict) -> bool:
    """Write `payload` stamped with fetched_at. Returns True if content changed.

    fetched_at is excluded from the comparison so an unchanged page does not
    churn a commit (and therefore does not burn a Cloudflare Pages deploy).
    """
    previous = read_json(rel) or {}
    stamped = dict(payload)
    stamped["fetched_at"] = now_utc().isoformat(timespec="seconds")

    comparable = {k: v for k, v in stamped.items() if k != "fetched_at"}
    prior = {k: v for k, v in previous.items() if k != "fetched_at"}
    changed = comparable != prior
    if not changed and previous:
        # Keep the freshness stamp current without rewriting the body.
        stamped = {**previous, "fetched_at": stamped["fetched_at"]}
    write_json(rel, stamped)
    return changed


# --------------------------------------------------------------------------
# Freshness bookkeeping
# --------------------------------------------------------------------------

def load_state() -> dict:
    return read_json("state.json", default={}) or {}


def save_state(state: dict) -> None:
    write_json("state.json", state)


def age_hours(state: dict, key: str) -> float:
    stamp = state.get(key)
    if not stamp:
        return float("inf")
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now_utc() - seen).total_seconds() / 3600.0


def touch(state: dict, key: str) -> None:
    state[key] = now_utc().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Change feed
# --------------------------------------------------------------------------

def load_changes() -> list[dict]:
    return read_json("changes.json", default=[]) or []


def record_change(changes: list[dict], kind: str, title: str, detail: str = "", url: str = "") -> None:
    entry = {
        "ts": now_utc().isoformat(timespec="seconds"),
        "kind": kind,
        "title": title,
        "detail": detail,
        "url": url,
    }
    # Guard against a flapping upstream page re-announcing the same thing.
    for recent in changes[:5]:
        if recent["kind"] == kind and recent["title"] == title and recent["detail"] == detail:
            return
    changes.insert(0, entry)


def save_changes(changes: list[dict]) -> None:
    write_json("changes.json", changes[:MAX_CHANGES])
