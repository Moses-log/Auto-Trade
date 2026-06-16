import json
import os

_SPOTS_PATH = "/data/early_access.json"
_MAX_SPOTS = 15


def load_spots() -> int:
    if not os.path.exists(_SPOTS_PATH):
        return _MAX_SPOTS
    try:
        with open(_SPOTS_PATH) as f:
            return int(json.load(f).get("spots_remaining", _MAX_SPOTS))
    except Exception:
        return _MAX_SPOTS


def _save_spots(n: int) -> None:
    os.makedirs(os.path.dirname(_SPOTS_PATH), exist_ok=True)
    with open(_SPOTS_PATH, "w") as f:
        json.dump({"spots_remaining": n}, f)


def decrement_spots() -> int:
    current = load_spots()
    new = max(0, current - 1)
    _save_spots(new)
    return new


def increment_spots() -> int:
    current = load_spots()
    new = min(_MAX_SPOTS, current + 1)
    _save_spots(new)
    return new
