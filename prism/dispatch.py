"""Retries, backoff and failover: the one place that decides what to try next.

`docs/IMPLEMENTATION_GUIDE.md:56` asks for two different things in one sentence —
"retry transient errors with exponential backoff; on a down or rate-limited
provider, move to the next in the chain". They are two nested loops, and keeping
them in one module rather than in the route is what makes the policy statable:

    for each target in the chain:          # failover, outer
        for each attempt at that target:   # retry, inner
            ...

**The dispatcher never inspects a status code.** Whether to retry the same target
and whether to try the next one are answered by `prism/providers/base.py:classify`
at the point the response was seen, and arrive here as two booleans on the
exception. That split matters: a dispatcher that re-derived the policy from
`exc.status` would be a second copy of the table, and the two copies would
disagree the first time somebody added a status to one of them.

**The operation is a callback, so streaming and non-streaming share this loop.**
`run()` takes `Callable[[ResolvedTarget], Awaitable[T]]` — `complete` for one,
`open_stream` for the other. The alternative, a retry loop per call style, is how
you end up with streaming that silently has no failover; and failover for streaming
is only safe because `open_stream` returns before any byte reaches the client (see
`prism/providers/base.py`'s module docstring).

**Backoff is jittered, and the jitter is not decoration.** Without it, N requests
that all hit the same provider at the same failure retry in the same millisecond,
so the herd that just overwhelmed the provider arrives again intact, having only
paused. Full jitter — a uniform sample from `[0, backoff]` rather than the backoff
itself — spreads them, and it is what AWS's own analysis of the three variants
recommends for total work done. `sleep` and `jitter` are injectable because a test
that proved the 200 ms / 400 ms sequence by actually waiting 600 ms would be
charged to every future run of the suite.

**Nothing is slept before moving to the *next* provider.** A backoff is a bet that
the same endpoint will be healthier shortly. A different provider is not more likely
to answer because we waited 400 ms first, and the delay would be spent out of the
caller's latency budget for nothing.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from prism.config import ResolvedTarget, RetryPolicy
from prism.providers.base import ProviderCallFailed

logger = logging.getLogger("prism.dispatch")

#: One attempt against one target. Raises `ProviderCallFailed` or returns a result.
type Operation[T] = Callable[[ResolvedTarget], Awaitable[T]]


@dataclass(frozen=True)
class Dispatched[T]:
    """A successful attempt, plus what it took to get there.

    `attempts` counts every upstream call made for this request, including the one
    that succeeded and including calls to earlier targets in the chain. So `retries`
    is "how many extra upstream calls did this request cost", which is the question
    the `request_log.retries` column is worth asking — a column that counted only
    retries of the *final* target would report 0 for a request that hammered a dead
    provider three times before failing over, which is the exact request an operator
    is looking for.
    """

    value: T
    target: ResolvedTarget
    attempts: int
    #: Position of the serving target in the chain. 0 is the primary.
    index: int

    @property
    def retries(self) -> int:
        return self.attempts - 1

    @property
    def fallback(self) -> bool:
        """True when something other than the primary served it — `x-prism-fallback`."""
        return self.index > 0


class ChainExhausted(Exception):
    """Every target in the chain has been tried and none of them served the request.

    Carries the accounting the log row needs (`attempts`, `retries`, `fallback`) and
    the last failure, so the route can record *which* target it gave up on without
    the route having to track attempts itself.

    Like `ProviderCallFailed`, this is never rendered to a client:
    `prism/api/chat.py` converts it into a generic 502, because `attempted` names
    providers and `last` may carry an upstream message
    (`docs/DATA_MODEL.md:44`).
    """

    def __init__(
        self,
        *,
        attempted: tuple[str, ...],
        attempts: int,
        target: ResolvedTarget | None,
        last: ProviderCallFailed | None,
    ) -> None:
        super().__init__(
            f"All {len(set(attempted))} target(s) failed after {attempts} attempt(s): "
            f"{' -> '.join(attempted)}"
        )
        #: Every attempt in order, as `provider/model` labels. A label appearing
        #: twice in a row is a retry; a new label is a failover.
        self.attempted = attempted
        self.attempts = attempts
        #: The target of the final attempt, for the log row's `resolved_provider`.
        #: Taken from the loop rather than from `last.target`, so an adapter that
        #: forgets to attach a target to its exception cannot quietly cost the audit
        #: trail the one field that says where the request ended up.
        self.target = target
        self.last = last

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)

    @property
    def fallback(self) -> bool:
        """True when the chain got past the primary before giving up."""
        return len(set(self.attempted)) > 1


class Dispatcher:
    """Runs one operation against a resolved chain until it succeeds or runs out.

    Holds no per-request state, so one instance is shared by the whole process and
    is safe to call concurrently — the loop's counters are locals.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.random

    def delay_seconds(self, retry_index: int) -> float:
        """Jittered backoff, in seconds, before retry `retry_index` (0-based).

        The first retry is `retry_index=0` and so gets `initial_backoff_ms` as its
        ceiling — the failing call itself is not a retry and is never delayed.
        """
        return self.policy.backoff_ms(retry_index) * self._jitter() / 1000

    async def run[T](
        self, chain: Sequence[ResolvedTarget], operation: Operation[T]
    ) -> Dispatched[T]:
        """Try `operation` down `chain`, retrying transient failures. Raises `ChainExhausted`."""
        if not chain:
            # Unreachable through `routing.resolve`, whose Route.chain is never
            # empty; explicit so a future caller cannot get a silent no-op.
            raise ValueError("Cannot dispatch against an empty chain.")

        attempted: list[str] = []
        attempts = 0
        failure: ProviderCallFailed | None = None
        last_target: ResolvedTarget | None = None

        for index, target in enumerate(chain):
            last_target = target
            for attempt in range(self.policy.max_attempts):
                if attempt:
                    delay = self.delay_seconds(attempt - 1)
                    logger.info(
                        "retrying %s in %.3fs (attempt %d of %d)",
                        target.label,
                        delay,
                        attempt + 1,
                        self.policy.max_attempts,
                    )
                    await self._sleep(delay)

                attempts += 1
                attempted.append(target.label)
                try:
                    value = await operation(target)
                except ProviderCallFailed as exc:
                    failure = exc
                    logger.warning("attempt %d failed: %s", attempts, exc)
                    if exc.retry_same and attempt + 1 < self.policy.max_attempts:
                        continue
                    break
                return Dispatched(
                    value=value, target=target, attempts=attempts, index=index
                )

            if failure is not None and not failure.try_next:
                # A 400, or a stream that already delivered output. Trying the next
                # provider would spend money to be told the same thing, or would
                # splice two answers together — see prism/providers/base.py.
                break

        raise ChainExhausted(
            attempted=tuple(attempted),
            attempts=attempts,
            target=last_target,
            last=failure,
        )


__all__ = ["ChainExhausted", "Dispatched", "Dispatcher", "Operation"]
