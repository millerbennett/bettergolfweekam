"""Second duplicate check inside the workflow's single-run concurrency lock.

Reads GitHub metadata only; an API failure stops the job before any crawl.
Examining the actual Crawl step avoids counting skipped duplicate jobs as work.
"""

import os
from datetime import datetime, timezone

import requests


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def may_crawl(runs, current_id, get_jobs, now):
    candidates = []
    for run in runs:
        if str(run["id"]) == str(current_id):
            continue
        if run["status"] != "completed":
            return False, "another update is active"
        if now - timestamp(run["updated_at"]) < 45 * 60:
            candidates.append(run)
    if len(candidates) > 10:
        return False, "too many recent runs; refusing a catch-up burst"
    for run in candidates:
        for job in get_jobs(run["id"]):
            for step in job.get("steps", []):
                if step["name"] != "Crawl" or step.get("conclusion") == "skipped":
                    continue
                if step.get("started_at") and now - timestamp(step["started_at"]) < 18 * 60:
                    return False, "a crawl started less than 18 minutes ago"
                if step.get("completed_at") and now - timestamp(step["completed_at"]) < 60:
                    return False, "a crawl just finished"
    return True, "ready"


def main():
    now = datetime.now(timezone.utc).timestamp()
    scheduled_at = os.environ.get("SCHEDULED_AT", "")
    if os.environ.get("REFRESH_SOURCE") == "cloudflare":
        age = now - timestamp(scheduled_at)
        if age < 0 or age > 10 * 60:
            allowed, reason = False, "scheduled tick expired while waiting"
        else:
            allowed, reason = check_github(now)
    else:
        allowed, reason = check_github(now)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"allowed={str(allowed).lower()}\n")
    print(f"Crawl guard: {reason}")


def check_github(now):
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    base = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions"

    def get(path):
        response = session.get(base + path, timeout=15, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"GitHub safety check failed: HTTP {response.status_code}")
        return response.json()

    runs = get("/workflows/update.yml/runs?branch=main&per_page=100")["workflow_runs"]
    return may_crawl(runs, os.environ["GITHUB_RUN_ID"],
                     lambda run_id: get(f"/runs/{run_id}/jobs?per_page=100")["jobs"], now)


if __name__ == "__main__":
    main()
