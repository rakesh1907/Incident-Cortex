"""
Background jobs: RCCA Drive lookup (once) + New Relic snapshot (interval, until resolved).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

_stop_flags: dict[str, threading.Event] = {}
_threads: list[threading.Thread] = []


def stop_incident_insight_jobs(inc_number: str) -> None:
    ev = _stop_flags.pop(str(inc_number), None)
    if ev is not None:
        ev.set()


def start_incident_insight_jobs(
    inc_number: str,
    *,
    get_incident: Callable[[], Optional[Any]],
    run_rcca_lookup: Callable[[], None],
    run_nr_monitor: Callable[[threading.Event], None],
) -> None:
    """
    Fire-and-forget background threads. Stopped via stop_incident_insight_jobs(inc_number).
    """
    key = str(inc_number)
    stop_incident_insight_jobs(key)
    ev = threading.Event()
    _stop_flags[key] = ev

    def rcca_wrapper():
        time.sleep(4)
        if ev.is_set():
            return
        try:
            run_rcca_lookup()
        except Exception as ex:
            print(f"[insights] RCCA job error: {ex}")

    def nr_wrapper():
        time.sleep(12)
        try:
            run_nr_monitor(ev)
        except Exception as ex:
            print(f"[insights] NR monitor error: {ex}")

    t1 = threading.Thread(target=rcca_wrapper, name=f"rcca-{key}", daemon=True)
    t2 = threading.Thread(target=nr_wrapper, name=f"nr-{key}", daemon=True)
    t1.start()
    t2.start()
    _threads.extend([t1, t2])
