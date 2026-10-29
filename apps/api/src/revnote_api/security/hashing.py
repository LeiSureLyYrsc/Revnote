"""Bounded off-loop execution for the CPU-bound password work.

Argon2 is deliberately expensive (64 MiB, 3 passes): running it on the event loop
would freeze every other request for the duration, and running it unbounded on a
thread pool would let a login flood allocate gigabytes. This module provides the
small piece of machinery both problems need — a **capacity-limited**, per-app
limiter that runs a callable in a worker thread.

Design decisions worth knowing:

* **One instance per application, not a module global.** A module-level semaphore
  would be shared by every app in the process and, worse, bound to whichever event
  loop created it first. Tests build several apps; production builds one. The
  instance lives on ``app.state`` via the API layer's dependency cache.
* **The semaphore is created lazily inside the running loop.** ``asyncio.Semaphore``
  binds to a loop when it first blocks, so creating it eagerly at import time
  breaks the second loop. Every access re-checks the loop and rebuilds if the app
  is somehow reused across loops.
* **Capacity is a resource guard, not a brute-force defence.** Two concurrent
  hashes by default: enough to keep a login burst moving, small enough that a
  hostile client cannot turn one endpoint into a memory amplifier. Real rate
  limiting belongs at the edge and is out of scope for v1.
* **No FastAPI/Pydantic/config imports.** ``services/`` may not import them, and
  this module is imported by the service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

__all__ = ["HashLimiter"]

_T = TypeVar("_T")


class HashLimiter:
    """Run CPU-bound password work off the loop with bounded concurrency.

    Usage::

        limiter = HashLimiter(capacity=2)
        digest = await limiter.run(hash_password, password)

    The executor is created lazily with ``max_workers == capacity`` so the thread
    count and the semaphore agree: no queue of threads waiting on a permit, and no
    permit without a thread available.
    """

    __slots__ = ("_capacity", "_executor", "_loop", "_semaphore")

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._semaphore: asyncio.Semaphore | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def capacity(self) -> int:
        return self._capacity

    def _ensure_primitives(self) -> asyncio.Semaphore:
        """Return a semaphore bound to the *current* loop, building state once."""
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            # A new loop (or first use): rebuild both primitives. Any previous
            # executor is left to be garbage collected; it has no pending work
            # because a cross-loop reuse means no task is currently waiting.
            self._semaphore = asyncio.Semaphore(self._capacity)
            self._executor = ThreadPoolExecutor(
                max_workers=self._capacity,
                thread_name_prefix="revnote-auth",
            )
            self._loop = loop
        return self._semaphore

    async def run(self, function: Callable[..., _T], /, *args: object) -> _T:
        """Execute ``function(*args)`` in a worker thread, at most N at a time.

        Cancellation propagates: if the request is abandoned while queued for a
        permit, nothing is submitted. If it is abandoned while running, the thread
        finishes (Argon2 cannot be interrupted) but the caller stops waiting, which
        is the honest behaviour — pretending the hash did not happen would be a lie.
        """
        semaphore = self._ensure_primitives()
        async with semaphore:
            assert self._executor is not None  # set by _ensure_primitives
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, function, *args)

    def shutdown(self) -> None:
        """Release worker threads (used when an app shuts down in tests)."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = None
        self._semaphore = None
        self._loop = None
