import pytest

import app.withdrawal_audit as wa


@pytest.fixture(autouse=True)
def _isolate_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "_FILE", tmp_path / "withdrawal_audit.json")


def test_load_returns_empty_list_when_file_missing():
    assert wa.load_withdrawal_audit() == []


def test_append_executed_record():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
        status="executed",
    )
    audit = wa.load_withdrawal_audit()
    assert len(audit) == 1
    assert audit[0]["status"] == "executed"
    assert audit[0]["id"] == "wd-aaaa1111"


def test_append_failed_record_with_reason():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
        status="failed", reason="Withdrawal exceeds available equity",
    )
    audit = wa.load_withdrawal_audit()
    assert audit[0]["status"] == "failed"
    assert audit[0]["reason"] == "Withdrawal exceeds available equity"


def test_append_canceled_record_with_timestamp():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="2026-06-21T10:00:00-05:00", run_at="2026-06-22T10:00:00-05:00",
        status="canceled", canceled_at="2026-06-21T12:00:00-05:00",
    )
    audit = wa.load_withdrawal_audit()
    assert audit[0]["status"] == "canceled"
    assert audit[0]["canceled_at"] == "2026-06-21T12:00:00-05:00"


def test_audit_log_is_append_only_across_multiple_entries():
    wa.append_withdrawal_audit(
        withdrawal_id="wd-aaaa1111", investor="Moses", amount=500.0,
        requested_at="t1", run_at="t2", status="canceled",
    )
    wa.append_withdrawal_audit(
        withdrawal_id="wd-bbbb2222", investor="Gabe", amount=200.0,
        requested_at="t3", run_at="t4", status="executed",
    )
    audit = wa.load_withdrawal_audit()
    assert len(audit) == 2
    assert [a["id"] for a in audit] == ["wd-aaaa1111", "wd-bbbb2222"]
