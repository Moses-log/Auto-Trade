import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")


def test_hf_jobs_registered():
    from app import scheduler as sch
    sch.scheduler.remove_all_jobs()
    sch.setup_jobs()
    ids = {j.id for j in sch.scheduler.get_jobs()}
    assert "hf_poll" in ids
    assert "hf_recap" in ids
