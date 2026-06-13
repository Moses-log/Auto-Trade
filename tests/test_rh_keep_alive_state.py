import os
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

from unittest.mock import patch


def test_get_last_run_ts_returns_none_when_file_missing(tmp_path):
    from app.rh_keep_alive_state import get_last_run_ts
    with patch("app.rh_keep_alive_state._RECORD_FILE", tmp_path / "missing.json"):
        assert get_last_run_ts() is None


def test_record_run_then_get_last_run_ts_roundtrip(tmp_path):
    from app.rh_keep_alive_state import get_last_run_ts, record_run
    record_file = tmp_path / "rh_keep_alive_state.json"
    with patch("app.rh_keep_alive_state._RECORD_FILE", record_file):
        record_run(1749648000)
        assert get_last_run_ts() == 1749648000


def test_record_run_overwrites_prior_value(tmp_path):
    from app.rh_keep_alive_state import get_last_run_ts, record_run
    record_file = tmp_path / "rh_keep_alive_state.json"
    with patch("app.rh_keep_alive_state._RECORD_FILE", record_file):
        record_run(1749648000)
        record_run(1749734400)
        assert get_last_run_ts() == 1749734400


def test_get_last_run_ts_handles_malformed_json(tmp_path):
    from app.rh_keep_alive_state import get_last_run_ts
    record_file = tmp_path / "rh_keep_alive_state.json"
    record_file.write_text("not valid json")
    with patch("app.rh_keep_alive_state._RECORD_FILE", record_file):
        assert get_last_run_ts() is None
