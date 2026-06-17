import json
import os
import threading
from pathlib import Path

_FILE = Path(os.getenv("VISITS_PATH", "/data/visits.json"))
_lock = threading.Lock()


def increment_and_get() -> int:
    with _lock:
        count = 0
        if _FILE.exists():
            try:
                count = json.loads(_FILE.read_text()).get("count", 0)
            except Exception:
                pass
        count += 1
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"count": count}))
        tmp.replace(_FILE)
    return count
