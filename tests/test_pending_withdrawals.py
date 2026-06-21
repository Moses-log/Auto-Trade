import json
import pytest

import app.pending_withdrawals as pw


@pytest.fixture(autouse=True)
def _isolate_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "_FILE", tmp_path / "pending_withdrawals.json")


def test_load_returns_empty_list_when_file_missing():
    assert pw.load_pending_withdrawals() == []


def test_save_then_load_roundtrip():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111",
        investor="Moses",
        amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00",
        run_at="2026-06-22T10:00:00-05:00",
    )
    pending = pw.load_pending_withdrawals()
    assert len(pending) == 1
    assert pending[0]["id"] == "wd-aaaa1111"
    assert pending[0]["investor"] == "Moses"
    assert pending[0]["amount"] == 500.0
    assert pending[0]["run_at"] == "2026-06-22T10:00:00-05:00"


def test_save_appends_multiple_records():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
    )
    pw.save_pending_withdrawal(
        withdrawal_id="wd-bbbb2222", investor="Gabe", amount=200.0,
        requested_at="2026-06-21T11:00:00-05:00", run_at="2026-06-22T11:00:00-05:00",
    )
    pending = pw.load_pending_withdrawals()
    assert {p["id"] for p in pending} == {"wd-aaaa1111", "wd-bbbb2222"}


def test_remove_pending_withdrawal_removes_only_matching_id():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
    )
    pw.save_pending_withdrawal(
        withdrawal_id="wd-bbbb2222", investor="Gabe", amount=200.0,
        requested_at="2026-06-21T11:00:00-05:00", run_at="2026-06-22T11:00:00-05:00",
    )
    pw.remove_pending_withdrawal("wd-aaaa1111")
    pending = pw.load_pending_withdrawals()
    assert len(pending) == 1
    assert pending[0]["id"] == "wd-bbbb2222"


def test_get_pending_withdrawal_returns_none_when_not_found():
    assert pw.get_pending_withdrawal("wd-missing") is None


def test_get_pending_withdrawal_returns_matching_record():
    pw.save_pending_withdrawal(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
    )
    record = pw.get_pending_withdrawal("wd-aaaa1111")
    assert record["investor"] == "Moses"


def test_load_returns_empty_list_on_corrupt_json(tmp_path, monkeypatch):
    bad_file = tmp_path / "corrupt.json"
    bad_file.write_text("not valid json")
    monkeypatch.setattr(pw, "_FILE", bad_file)
    assert pw.load_pending_withdrawals() == []
