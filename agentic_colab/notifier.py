"""Wakes up live-update streams when new events are committed.

Writes happen on worker threads (sync DB code); streams wait on asyncio
events. `notify()` is thread-safe. Streams read the actual data from the
`events` table, so a missed wake-up only costs latency, never data.
"""

from __future__ import annotations

import asyncio
import threading


class Notifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Event]:
        entry = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.add(entry)
        return entry

    def unsubscribe(self, entry: tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
        with self._lock:
            self._waiters.discard(entry)

    def notify(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # loop closed
                self.unsubscribe((loop, ev))
