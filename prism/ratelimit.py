"""Per-key requests-per-minute limiting.

`docs/DATA_MODEL.md:118` permits this state to live in memory for a single
instance, but requires it to be **race-safe** — "token bucket, sliding window with
atomic operations, or equivalent — not read-then-write". `scripts/load_test.py`
exists to catch exactly that bug: it fires a burst well above a key's limit and
fails the build if more than `limit` requests are admitted.

**Sliding window log, not a fixed window.** A fixed window counts requests per
calendar minute and resets at the boundary, which admits up to `2 × limit`
requests across a boundary — 10 at 11:59:59.9 and 10 more at 12:00:00.1 is twenty
requests in 200 ms while reporting compliance with a limit of ten. The load test
bursts inside a single window so it would not catch that, but a client that
retries on a timer would hit it constantly. A sliding window log answers the exact
question the limit asks: how many requests has this key made in the last 60
seconds?

The cost of exactness is memory: one timestamp per admitted request per key,
bounded by the key's own limit (the busiest seeded key is 300/min, so ~300 floats).
That is the whole reason the exact structure is affordable here and a counter
would be needed at scale.

**Why a lock at all, in an async single-threaded server.** Inside one event loop a
function with no `await` in it cannot be interleaved, so the critical section below
is already atomic — and that invariant, not the lock, is what makes this correct.
The lock is cheap insurance against the invariant being broken later: the moment
someone runs this under `--workers 1` with a thread pool, or adds an `await`
mid-function, the no-await argument silently stops holding while the lock keeps
working. An uncontended `threading.Lock` costs tens of nanoseconds; a
read-then-write race costs a failed evaluation.

Restart behaviour is deliberate and documented in the README's Known limitations:
in-memory state means a restart forgives outstanding usage. That is the accepted
trade for Must Have; budgets, which must *not* be forgiven, are in Postgres.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class Decision:
    """The outcome of one admission attempt."""

    allowed: bool
    limit: int
    #: Requests still available in the current window, after this decision.
    remaining: int
    #: Seconds until the window frees a slot. 0 when the request was admitted.
    retry_after: float = 0.0


class SlidingWindowLimiter:
    """Requests-per-minute, per tenant, exact within its window."""

    def __init__(
        self,
        *,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # `time.monotonic`, never `time.time`. Wall clock can step backwards — an
        # NTP correction or a VM resuming from suspend — and a window computed from
        # a clock that moved backwards either forgives every outstanding request or
        # locks a key out until the clock catches up. Injectable so tests can move
        # time deliberately instead of sleeping.
        self._clock = clock
        self._window = window_seconds
        self._hits: dict[int, deque[float]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, tenant_id: int, limit: int) -> Decision:
        """Admit or reject one request, and record it if admitted.

        Read, evict, test and append happen under one lock and in one pass. There
        is no window in which another caller can observe the count *before* this
        request is recorded — which is the whole difference between this and the
        read-then-write shape the load test hunts for.
        """
        if limit <= 0:
            # A limit of zero means "no requests", not "unlimited". Treating a
            # missing or zero limit as unlimited is how a misconfigured tenant
            # gets free rein, so it is rejected explicitly.
            return Decision(allowed=False, limit=limit, remaining=0, retry_after=self._window)

        now = self._clock()
        cutoff = now - self._window

        with self._lock:
            hits = self._hits.get(tenant_id)
            if hits is None:
                hits = self._hits[tenant_id] = deque()

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                # Rejections are deliberately *not* recorded. Recording them would
                # let a client that keeps hammering a limited key extend its own
                # lockout indefinitely, turning a rate limit into a ban.
                retry_after = max(0.0, hits[0] + self._window - now)
                return Decision(
                    allowed=False, limit=limit, remaining=0, retry_after=retry_after
                )

            hits.append(now)
            return Decision(allowed=True, limit=limit, remaining=limit - len(hits))

    def snapshot(self, tenant_id: int) -> int:
        """Requests currently counted against `tenant_id`. For the ops console."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits.get(tenant_id)
            if not hits:
                return 0
            return sum(1 for hit in hits if hit > cutoff)

    def prune(self) -> int:
        """Drop keys with no recent activity. Returns how many were dropped.

        Without this, a gateway that has seen a million one-off keys holds a
        million empty deques for the life of the process. Called opportunistically
        rather than on a timer, so there is no background task to supervise.
        """
        cutoff = self._clock() - self._window
        with self._lock:
            stale = [
                tenant_id
                for tenant_id, hits in self._hits.items()
                if not hits or hits[-1] <= cutoff
            ]
            for tenant_id in stale:
                del self._hits[tenant_id]
            return len(stale)

    def reset(self) -> None:
        """Forget everything. Tests only."""
        with self._lock:
            self._hits.clear()


__all__ = ["WINDOW_SECONDS", "Decision", "SlidingWindowLimiter"]
