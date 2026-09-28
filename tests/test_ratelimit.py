"""The rate limiter, tested at the unit level with a clock the test controls.

`scripts/load_test.py` is the end-to-end check for over-admission, but it can only
say "too many got in" — it cannot say why. These tests pin the three properties the
implementation actually rests on: the window slides rather than resetting, a
rejection does not extend itself, and concurrency cannot admit more than the limit.
"""

from __future__ import annotations

import threading

from prism.ratelimit import SlidingWindowLimiter
from tests.conftest import FakeClock


def test_admits_exactly_the_limit_then_rejects(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    decisions = [limiter.try_acquire(1, 3) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.remaining for d in decisions] == [2, 1, 0, 0]


def test_tenants_do_not_share_a_window(clock: FakeClock) -> None:
    """A noisy key must not be able to rate-limit a quiet one."""
    limiter = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        limiter.try_acquire(1, 3)
    assert limiter.try_acquire(1, 3).allowed is False
    assert limiter.try_acquire(2, 3).allowed is True


def test_the_window_slides_rather_than_resetting(clock: FakeClock) -> None:
    """This is the whole reason the implementation keeps timestamps.

    Three requests spread over the first half-minute free up one slot at a time as
    each ages out, not all three at a minute boundary. A fixed-window counter passes
    "admits exactly the limit" above and fails this: at t=60 it would forgive all
    three at once and admit a burst of six inside 200 ms.
    """
    limiter = SlidingWindowLimiter(clock=clock)
    for offset in (0, 10, 20):
        clock.now = 1_000.0 + offset
        assert limiter.try_acquire(1, 3).allowed is True

    clock.now = 1_059.0
    assert limiter.try_acquire(1, 3).allowed is False

    # The first request (t=0) has now aged out; the other two have not.
    clock.now = 1_061.0
    assert limiter.try_acquire(1, 3).allowed is True
    assert limiter.try_acquire(1, 3).allowed is False


def test_a_rejection_is_not_recorded(clock: FakeClock) -> None:
    """Otherwise a client that keeps retrying converts its rate limit into a ban."""
    limiter = SlidingWindowLimiter(clock=clock)
    for _ in range(2):
        limiter.try_acquire(1, 2)
    for _ in range(50):
        limiter.try_acquire(1, 2)

    # Sixty-one seconds after the two *admitted* requests, quota is free again —
    # which is only true if the fifty rejections left no timestamps behind.
    clock.now += 61
    assert limiter.try_acquire(1, 2).allowed is True


def test_retry_after_points_at_the_oldest_request(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    limiter.try_acquire(1, 1)
    clock.now += 15
    decision = limiter.try_acquire(1, 1)
    assert decision.allowed is False
    # The one request in the window was made 15s ago, so a slot frees in 45s.
    assert round(decision.retry_after) == 45


def test_a_zero_limit_means_no_requests_not_unlimited(clock: FakeClock) -> None:
    """A misconfigured tenant must fail closed."""
    limiter = SlidingWindowLimiter(clock=clock)
    assert limiter.try_acquire(1, 0).allowed is False
    assert limiter.try_acquire(1, -5).allowed is False


def test_no_over_admission_under_thread_contention() -> None:
    """The property `scripts/load_test.py` fails the build over.

    Real threads, real contention, a real clock: forty threads race to acquire from
    a limit of ten and exactly ten may win. Replacing the locked section with a
    read-then-write — read the count, decide, then append — makes this fail
    intermittently, which is exactly how the bug behaves in production.
    """
    limiter = SlidingWindowLimiter()
    admitted: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(40)

    def worker() -> None:
        start.wait()
        decision = limiter.try_acquire(1, 10)
        with lock:
            admitted.append(decision.allowed)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(admitted) == 10


def test_prune_drops_only_inactive_tenants(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    limiter.try_acquire(1, 5)
    clock.now += 61
    limiter.try_acquire(2, 5)

    assert limiter.prune() == 1
    assert limiter.snapshot(1) == 0
    assert limiter.snapshot(2) == 1
