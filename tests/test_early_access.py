import json
import os
import tempfile
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("WEBHOOK_SECRET", "test_secret")

import app.early_access as ea


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ea, "_SPOTS_PATH", str(tmp_path / "early_access.json"))
    yield


def test_load_spots_defaults_to_15_when_file_missing():
    assert ea.load_spots() == 15


def test_load_spots_reads_existing_file():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 10}, f)
    assert ea.load_spots() == 10


def test_decrement_spots_reduces_count():
    ea.decrement_spots()
    assert ea.load_spots() == 14


def test_decrement_spots_floors_at_zero():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 0}, f)
    ea.decrement_spots()
    assert ea.load_spots() == 0


def test_increment_spots_increases_count():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 10}, f)
    ea.increment_spots()
    assert ea.load_spots() == 11


def test_increment_spots_caps_at_15():
    with open(ea._SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": 15}, f)
    ea.increment_spots()
    assert ea.load_spots() == 15
