"""
memprofile.py — Temporary memory-leak instrumentation.

Purpose
───────
The Render process memory creeps up ~10 MB/day until the 512 MB cap is hit
and daily reports get OOM-killed. This module logs evidence to pinpoint the
leaking code so we can fix the root cause, then remove this file.

Two signals are emitted, both as ordinary JSON log lines (logger "memprofile"):

  1. RSS (resident set size) — the real number Render bills against. Logged
     before/after every heavy scheduler job (via `profile_job`) and once an
     hour (via `log_snapshot`). A job whose "after" RSS is reliably higher
     than its "before", run after run, is the leaker.

  2. tracemalloc top allocators — Python-level allocation sites ranked by
     retained size, plus growth since the previous snapshot and since
     startup. Over a few days the leaking source line climbs to the top.

Cost / safety
─────────────
- RSS reading is free (one small /proc read). Always on.
- tracemalloc adds bookkeeping memory roughly proportional to tracked
  allocations; kept modest via a small frame count. Gated separately so it
  can be switched off without touching RSS logging.
- Nothing here ever raises into the caller — a broken probe must never take
  down a real-money job. Every path is wrapped defensively.

Env vars
────────
- MEMPROFILE=0            disable everything (RSS + tracemalloc).       default: on
- MEMPROFILE_TRACE=0      keep RSS logging but disable tracemalloc.     default: on
- MEMPROFILE_FRAMES=<n>   tracemalloc traceback depth.                  default: 5
- MEMPROFILE_TOP=<n>      how many top allocator lines to log.          default: 10
"""

from __future__ import annotations

import logging
import os
import tracemalloc
from contextlib import asynccontextmanager
from typing import Optional

log = logging.getLogger("memprofile")


def _flag(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


_ENABLED = _flag("MEMPROFILE", True)
_TRACE_ENABLED = _ENABLED and _flag("MEMPROFILE_TRACE", True)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_FRAMES = max(1, _int_env("MEMPROFILE_FRAMES", 5))
_TOP = max(1, _int_env("MEMPROFILE_TOP", 10))

# Snapshots for diffing. _baseline is the first snapshot after startup
# (cumulative growth); _last is the previous snapshot (per-interval growth).
_baseline: Optional[tracemalloc.Snapshot] = None
_last: Optional[tracemalloc.Snapshot] = None


def init() -> None:
    """Start tracemalloc. Call once at startup, before the first job runs."""
    if not _ENABLED:
        log.info("memprofile disabled (MEMPROFILE=0)")
        return
    if _TRACE_ENABLED and not tracemalloc.is_tracing():
        try:
            tracemalloc.start(_FRAMES)
            log.info("memprofile started (tracemalloc frames=%d, top=%d)", _FRAMES, _TOP)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("memprofile: tracemalloc failed to start: %s", exc)
    else:
        log.info("memprofile started (RSS only, tracemalloc disabled)")


def read_rss_mb() -> Optional[float]:
    """Current resident set size in MB, or None if unavailable.

    Reads /proc/self/status (Linux — Render's runtime). Falls back to
    resource.getrusage on other platforms (note: ru_maxrss is PEAK, not
    current, so it only ever rises — still useful as a ceiling signal).
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return round(kb / 1024, 1)
    except Exception:
        pass
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kB, macOS reports bytes. We only hit this branch off
        # Linux (macOS/dev), so treat as bytes.
        return round(maxrss / (1024 * 1024), 1)
    except Exception:
        return None


def _log_tracemalloc(tag: str) -> None:
    """Log top allocators plus growth vs last snapshot and vs baseline."""
    global _baseline, _last
    if not _TRACE_ENABLED or not tracemalloc.is_tracing():
        return
    try:
        snap = tracemalloc.take_snapshot()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("memprofile: take_snapshot failed at %s: %s", tag, exc)
        return

    traced_mb = round(tracemalloc.get_traced_memory()[0] / (1024 * 1024), 1)

    # Absolute top allocators by retained size.
    try:
        top = snap.statistics("lineno")[:_TOP]
        for i, stat in enumerate(top, 1):
            frame = stat.traceback[0]
            log.info(
                "memprofile TOP %s #%d",
                tag,
                i,
                extra={
                    "mem_tag": tag,
                    "kind": "top",
                    "rank": i,
                    "site": f"{frame.filename}:{frame.lineno}",
                    "size_kb": round(stat.size / 1024, 1),
                    "count": stat.count,
                },
            )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("memprofile: top stats failed at %s: %s", tag, exc)

    # Growth since baseline (cumulative) — the clearest leak signal over days.
    if _baseline is not None:
        try:
            grown = snap.compare_to(_baseline, "lineno")[:_TOP]
            for i, stat in enumerate(grown, 1):
                if stat.size_diff <= 0:
                    continue
                frame = stat.traceback[0]
                log.info(
                    "memprofile GROWTH-SINCE-START %s #%d",
                    tag,
                    i,
                    extra={
                        "mem_tag": tag,
                        "kind": "growth_since_start",
                        "rank": i,
                        "site": f"{frame.filename}:{frame.lineno}",
                        "size_diff_kb": round(stat.size_diff / 1024, 1),
                        "count_diff": stat.count_diff,
                    },
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("memprofile: baseline compare failed at %s: %s", tag, exc)
    else:
        _baseline = snap

    _last = snap
    log.info(
        "memprofile TRACED %s",
        tag,
        extra={"mem_tag": tag, "kind": "traced_total", "traced_mb": traced_mb},
    )


def log_snapshot(tag: str) -> None:
    """Log RSS and (if enabled) tracemalloc top allocators. Never raises."""
    if not _ENABLED:
        return
    try:
        rss = read_rss_mb()
        log.info(
            "memprofile RSS %s",
            tag,
            extra={"mem_tag": tag, "kind": "rss", "rss_mb": rss},
        )
        _log_tracemalloc(tag)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("memprofile: log_snapshot(%s) failed: %s", tag, exc)


@asynccontextmanager
async def profile_job(tag: str):
    """Async context manager: log RSS before/after a job plus the delta.

    A job whose RSS delta is consistently positive across runs is retaining
    memory. Usage:

        async with profile_job("weekday_jobs"):
            ...job body...
    """
    if not _ENABLED:
        yield
        return
    before = read_rss_mb()
    log.info(
        "memprofile JOB-START %s",
        tag,
        extra={"mem_tag": tag, "kind": "job_start", "rss_mb": before},
    )
    try:
        yield
    finally:
        after = read_rss_mb()
        delta = round(after - before, 1) if (after is not None and before is not None) else None
        log.info(
            "memprofile JOB-END %s",
            tag,
            extra={
                "mem_tag": tag,
                "kind": "job_end",
                "rss_before_mb": before,
                "rss_after_mb": after,
                "rss_delta_mb": delta,
            },
        )
        # Full allocator snapshot after the heavy job — this is where the
        # leaking line shows up if the leak is job-driven.
        _log_tracemalloc(f"{tag}:end")
