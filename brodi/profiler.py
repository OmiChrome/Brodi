"""Opt-in wall-clock tracing, including awaited network/OS time (unlike CPU profiles).

Set BRODI_PROFILE to a JSONL path to trace real commands. Reports contain stage names,
durations and error types, never utterances, arguments, page contents or credentials.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

_active: ContextVar[Profile | None] = ContextVar("brodi_profile", default=None)
_parent: ContextVar[int | None] = ContextVar("brodi_profile_parent", default=None)
_write_lock = threading.Lock()
log = logging.getLogger(__name__)


class Profile:
    def __init__(self, label: str = "command"):
        self.label = label
        self.events: list[dict] = []
        self.started = time.perf_counter()
        self._lock = threading.Lock()

    @contextmanager
    def capture(self):
        token = _active.set(self)
        parent = _parent.set(None)
        try:
            yield self
        finally:
            _parent.reset(parent)
            _active.reset(token)

    def report(self) -> dict:
        with self._lock:
            events = [dict(e) for e in self.events if "duration_ms" in e]
        groups = defaultdict(list)
        for event in events:
            groups[event["stage"]].append(event["duration_ms"])
        stages = []
        for name, values in groups.items():
            ordered = sorted(values)
            stages.append(
                {
                    "stage": name,
                    "count": len(values),
                    "total_ms": round(sum(values), 3),
                    "p50_ms": round(ordered[math.ceil(len(values) * 0.5) - 1], 3),
                    "p95_ms": round(ordered[math.ceil(len(values) * 0.95) - 1], 3),
                    "max_ms": round(max(values), 3),
                }
            )
        return {
            "label": self.label,
            "stages": sorted(stages, key=lambda s: -s["total_ms"]),
            "events": sorted(events, key=lambda e: e["id"]),
            "note": "Inclusive wall time; nested stages overlap. Dispatch excludes speech and window paint.",
        }

    def append(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.report()) + "\n")

    def trace(self) -> dict:
        """Chrome/Perfetto trace event format, importable at ui.perfetto.dev."""
        return {
            "traceEvents": [
                {
                    "name": e["stage"],
                    "ph": "X",
                    "ts": e["start_ms"] * 1000,
                    "dur": e["duration_ms"] * 1000,
                    "pid": 1,
                    "tid": e["thread"],
                    "args": {"parent": e["parent"], "error": e.get("error", "")},
                }
                for e in self.report()["events"]
            ]
        }


@contextmanager
def span(stage: str):
    profile = _active.get()
    if profile is None:
        yield
        return
    started = time.perf_counter()
    with profile._lock:
        event = {
            "id": len(profile.events),
            "parent": _parent.get(),
            "stage": stage,
            "start_ms": (started - profile.started) * 1000,
            "thread": threading.get_ident(),
        }
        profile.events.append(event)
    token = _parent.set(event["id"])
    try:
        yield
    except BaseException as exc:
        event["error"] = type(exc).__name__
        raise
    finally:
        event["duration_ms"] = (time.perf_counter() - started) * 1000
        _parent.reset(token)


def profiled(stage: str, *, root: bool = False):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args, **kwargs):
                path = os.environ.get("BRODI_PROFILE") if root and _active.get() is None else None
                if path:
                    profile = Profile(stage)
                    try:
                        with profile.capture(), span(stage):
                            return await fn(*args, **kwargs)
                    finally:
                        try:
                            profile.append(path)
                        except OSError:
                            log.exception("Could not write performance trace")
                with span(stage):
                    return await fn(*args, **kwargs)

            return async_wrapper

        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            with span(stage):
                return fn(*args, **kwargs)

        return sync_wrapper

    return decorate
