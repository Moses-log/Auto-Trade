import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def _grace(sch, job):
    # Jobs added before scheduler.start() are pending and only pick up the
    # scheduler's job_defaults once it starts, so fall back to the default.
    return getattr(job, "misfire_grace_time", sch.scheduler._job_defaults["misfire_grace_time"])


def test_jobs_tolerate_a_late_event_loop():
    """A job that fires a second or two late must still run.

    APScheduler's built-in misfire_grace_time is 1 second: on 2026-10-07 the
    16:00 ET weekday_jobs tick was 1.4s late and the whole P&L/recap bundle
    was silently dropped ("Run time of job ... was missed by 0:00:01.437613").
    """
    from app import scheduler as sch
    sch.scheduler.remove_all_jobs()
    sch.setup_jobs()
    jobs = sch.scheduler.get_jobs()
    assert jobs
    too_strict = {j.id: _grace(sch, j) for j in jobs if _grace(sch, j) < 60}
    assert not too_strict, f"jobs that drop when the loop is <60s late: {too_strict}"
