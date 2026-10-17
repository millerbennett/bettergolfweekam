from datetime import datetime, timezone

from scheduler.guard import may_crawl


NOW = 1_800_000_000


def ago(seconds):
    return datetime.fromtimestamp(NOW - seconds, timezone.utc).isoformat()


def run(run_id=1, status="completed"):
    return {"id": run_id, "status": status, "updated_at": ago(10)}


def jobs(start=60, end=10, conclusion="success"):
    return [{"steps": [{"name": "Crawl", "started_at": ago(start),
                        "completed_at": ago(end), "conclusion": conclusion}]}]


def test_first_refresh_and_current_run_are_allowed():
    assert may_crawl([run(2, "in_progress")], 2, lambda _: [], NOW)[0]


def test_queued_or_running_refresh_blocks_crawl():
    for status in ("queued", "in_progress", "waiting", "pending"):
        assert not may_crawl([run(status=status)], 2, lambda _: [], NOW)[0]


def test_duplicate_and_failed_crawl_are_suppressed():
    for conclusion in ("success", "failure"):
        assert not may_crawl([run()], 2, lambda _: jobs(conclusion=conclusion), NOW)[0]


def test_skipped_job_does_not_postpone_next_real_refresh():
    assert may_crawl([run()], 2, lambda _: jobs(conclusion="skipped"), NOW)[0]


def test_normal_twenty_minute_cadence_is_allowed():
    assert may_crawl([run()], 2, lambda _: jobs(start=20 * 60, end=19 * 60), NOW)[0]


def test_long_running_crawl_gets_breathing_room_when_it_finishes():
    assert not may_crawl([run()], 2, lambda _: jobs(start=25 * 60, end=5), NOW)[0]


def test_large_backlog_is_skipped_without_fetching_all_jobs():
    def unexpected(_):
        raise AssertionError("must not fetch jobs for a backlog")
    assert not may_crawl([run(n) for n in range(11)], 100, unexpected, NOW)[0]


def test_rerunning_the_same_workflow_cannot_bypass_the_cooldown():
    current = {**run(2, "in_progress"), "run_attempt": 2}
    assert not may_crawl([current], 2, lambda _: jobs(), NOW)[0]


def test_rerun_after_the_cooldown_can_proceed():
    current = {**run(2, "in_progress"), "run_attempt": 2}
    assert may_crawl([current], 2, lambda _: jobs(start=20 * 60, end=19 * 60), NOW)[0]
