"""The retry and failover policy, tested without a database or a socket.

This module deliberately needs neither, so it runs in the `-m "not postgres"` lane:
retries and failover are pure control flow over the two booleans
`prism/providers/base.py:classify` produces, and the fastest possible feedback on
that control flow is worth more than testing it through HTTP. The end-to-end
consequences — the `x-prism-fallback` header, `request_log.retries` — are asserted
in `tests/test_chat.py`.

`sleep` is injected everywhere. The real policy waits 200 ms then 400 ms, so a
faithful test of a fully-exhausted chain would add 1.2 s of pure waiting to every
future run of the suite while proving nothing that the recorded delays do not.
"""

from __future__ import annotations

import asyncio

import pytest

from prism.config import GatewayConfig, ResolvedTarget, RetryPolicy
from prism.dispatch import ChainExhausted, Dispatcher
from prism.providers.base import ProviderCallFailed, classify
from prism.routing import resolve

POLICY = RetryPolicy(max_attempts=3, initial_backoff_ms=200, backoff_multiplier=2.0)


class Sleeps:
    """Records the delays asked for; never serves them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def dispatcher(policy: RetryPolicy = POLICY, *, sleeps: Sleeps | None = None) -> Dispatcher:
    """A dispatcher whose backoff is pinned to its ceiling, so delays are exact."""
    return Dispatcher(policy, sleep=sleeps or Sleeps(), jitter=lambda: 1.0)


@pytest.fixture
async def chain(config: GatewayConfig) -> tuple[ResolvedTarget, ...]:
    """The real `fast` chain: alpha-small, then beta-small.

    Async because `resolve` is: a router may embed to classify. This alias is not a
    router, so nothing is embedded here — the await is the price of one code path
    for all three kinds of request.
    """
    return (await resolve(config, "fast")).chain


def failing(status: int) -> ProviderCallFailed:
    """A failure carrying the real taxonomy's verdict for `status`."""
    retry_same, try_next = classify(status)
    return ProviderCallFailed(
        f"Upstream returned {status}",
        status=status,
        retry_same=retry_same,
        try_next=try_next,
    )


class Script:
    """An operation that fails according to a per-provider script.

    Keyed by provider name rather than by call count, because "alpha is down and beta
    is healthy" is the situation being tested and expressing it as a call index makes
    the test break whenever the retry count changes.
    """

    def __init__(self, **failures: ProviderCallFailed | list[ProviderCallFailed | None]) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def __call__(self, target: ResolvedTarget) -> str:
        self.calls.append(target.label)
        scripted = self.failures.get(target.provider.name)
        if isinstance(scripted, list):
            index = sum(1 for c in self.calls if c == target.label) - 1
            scripted = scripted[index] if index < len(scripted) else None
        if scripted is not None:
            raise scripted
        return f"served by {target.label}"


# -- the happy path ---------------------------------------------------------


async def test_a_first_time_success_costs_nothing_extra(chain) -> None:
    sleeps = Sleeps()
    operation = Script()

    outcome = await dispatcher(sleeps=sleeps).run(chain, operation)

    assert outcome.value == "served by alpha/alpha-small"
    assert outcome.attempts == 1
    assert outcome.retries == 0
    assert outcome.fallback is False
    assert operation.calls == ["alpha/alpha-small"]
    # No delay before the *first* attempt: the failing call is not a retry.
    assert sleeps.delays == []


async def test_the_operation_is_handed_each_target_in_turn(chain) -> None:
    """The whole reason streaming and non-streaming can share this loop.

    `run` knows nothing about what the operation does — only which target to hand it.
    """
    seen: list[str] = []

    async def operation(target: ResolvedTarget) -> str:
        seen.append(target.model)
        raise failing(503)

    with pytest.raises(ChainExhausted):
        await dispatcher().run(chain, operation)

    assert set(seen) == {"alpha-small", "beta-small"}


# -- retrying the same target ------------------------------------------------


async def test_a_transient_failure_is_retried_on_the_same_target(chain) -> None:
    sleeps = Sleeps()
    operation = Script(alpha=[failing(503), None])

    outcome = await dispatcher(sleeps=sleeps).run(chain, operation)

    assert outcome.value == "served by alpha/alpha-small"
    assert outcome.attempts == 2
    assert outcome.retries == 1
    # Retried, not failed over — and that distinction is a header.
    assert outcome.fallback is False
    assert operation.calls == ["alpha/alpha-small"] * 2
    assert sleeps.delays == [0.2]


async def test_the_attempt_budget_is_per_target_not_per_request(chain) -> None:
    """`max_attempts=3` means three attempts at *each* target, six in total here.

    The alternative reading — three attempts for the whole request — would mean a
    chain of four providers never reached the last two, which makes the fourth entry
    in a config's fallback list decoration.
    """
    operation = Script(alpha=failing(503), beta=failing(503))

    with pytest.raises(ChainExhausted):
        await dispatcher().run(chain, operation)

    assert operation.calls == ["alpha/alpha-small"] * 3 + ["beta/beta-small"] * 3


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_statuses_are_retried(chain, status: int) -> None:
    operation = Script(alpha=[failing(status), None])
    outcome = await dispatcher().run(chain, operation)
    assert outcome.attempts == 2


async def test_a_transport_failure_with_no_status_is_still_retried(chain) -> None:
    """A dropped connection has no status code, and the dispatcher does not need one.

    The flags are what it acts on. Which failures *set* `retry_same` is not this
    module's business and is tested where the decision is made — see
    `tests/test_providers.py`, where a read timeout deliberately does not.
    """
    operation = Script(
        alpha=[
            ProviderCallFailed("Connection reset", retry_same=True, try_next=True),
            None,
        ]
    )
    outcome = await dispatcher().run(chain, operation)
    assert outcome.attempts == 2
    assert outcome.fallback is False


# -- backoff -----------------------------------------------------------------


async def test_backoff_grows_exponentially_between_retries(chain) -> None:
    sleeps = Sleeps()
    operation = Script(alpha=failing(503), beta=[failing(503), failing(503), None])

    outcome = await dispatcher(sleeps=sleeps).run(chain, operation)

    assert outcome.fallback is True
    # Two retries of alpha, then two of beta. The move from alpha to beta contributes
    # no delay of its own — waiting does not make a *different* provider healthier,
    # and the wait would be spent out of the caller's latency budget.
    assert sleeps.delays == [0.2, 0.4, 0.2, 0.4]


async def test_the_backoff_is_jittered_not_fixed(chain) -> None:
    """Full jitter: the computed backoff is a ceiling, not the delay itself.

    Without it, every request that failed at the same instant retries at the same
    instant, so the herd that overwhelmed a provider arrives again intact, having
    only paused. The jitter source is injected here so the multiplication is visible;
    in production it is `random.random`.
    """
    sleeps = Sleeps()
    quiet = Dispatcher(POLICY, sleep=sleeps, jitter=lambda: 0.25)
    operation = Script(alpha=[failing(503), failing(503), None])

    await quiet.run(chain, operation)

    assert sleeps.delays == [0.05, 0.1]


async def test_a_policy_with_no_retries_still_fails_over(chain) -> None:
    """`max_attempts=1` is a legitimate configuration: fail fast, move on."""
    sleeps = Sleeps()
    operation = Script(alpha=failing(503))

    outcome = await dispatcher(RetryPolicy(max_attempts=1), sleeps=sleeps).run(
        chain, operation
    )

    assert operation.calls == ["alpha/alpha-small", "beta/beta-small"]
    assert outcome.fallback is True
    assert sleeps.delays == []


# -- failing over ------------------------------------------------------------


async def test_a_dead_target_hands_over_to_the_next_in_the_chain(chain) -> None:
    operation = Script(alpha=failing(503))

    outcome = await dispatcher().run(chain, operation)

    assert outcome.value == "served by beta/beta-small"
    assert outcome.index == 1
    assert outcome.fallback is True
    # Four upstream calls for one request: three at alpha, one at beta. `retries`
    # counts the extra calls, not just the ones at the target that served it.
    assert outcome.attempts == 4
    assert outcome.retries == 3


async def test_a_malformed_request_is_neither_retried_nor_failed_over(chain) -> None:
    """400: every provider will say the same thing. One attempt, total."""
    sleeps = Sleeps()
    operation = Script(alpha=failing(400))

    with pytest.raises(ChainExhausted) as caught:
        await dispatcher(sleeps=sleeps).run(chain, operation)

    assert operation.calls == ["alpha/alpha-small"]
    assert sleeps.delays == []
    assert caught.value.attempts == 1
    assert caught.value.fallback is False


async def test_a_bad_credential_is_failed_over_without_being_retried(chain) -> None:
    """401 is where the two flags disagree, and so is the test that they are two.

    A single `is_retryable` boolean cannot express "pointless to retry, sensible to
    fail over" — it would either hammer a provider whose key is wrong or never try
    the provider whose key is right.
    """
    sleeps = Sleeps()
    operation = Script(alpha=failing(401))

    outcome = await dispatcher(sleeps=sleeps).run(chain, operation)

    assert operation.calls == ["alpha/alpha-small", "beta/beta-small"]
    assert outcome.fallback is True
    assert sleeps.delays == []


async def test_a_failure_that_forbids_both_stops_immediately(chain) -> None:
    """The mid-stream case: output has already reached the client.

    `prism/providers/http.py:HttpProviderStream` raises exactly this, and
    `docs/IMPLEMENTATION_GUIDE.md:172` forbids splicing another provider's answer
    onto the partial one. The dispatcher does not special-case streaming to honour
    that — it just obeys the flags.
    """
    operation = Script(
        alpha=ProviderCallFailed("died mid-stream", retry_same=False, try_next=False)
    )

    with pytest.raises(ChainExhausted):
        await dispatcher().run(chain, operation)

    assert operation.calls == ["alpha/alpha-small"]


# -- exhaustion --------------------------------------------------------------


async def test_an_exhausted_chain_reports_what_it_tried(chain) -> None:
    operation = Script(alpha=failing(503), beta=failing(429))

    with pytest.raises(ChainExhausted) as caught:
        await dispatcher().run(chain, operation)

    exhausted = caught.value
    assert exhausted.attempts == 6
    assert exhausted.retries == 5
    assert exhausted.fallback is True
    # The last target attempted, which is what the log row records as
    # `resolved_provider` — "where did this request end up", not "where did it start".
    assert exhausted.target is not None
    assert exhausted.target.provider.name == "beta"
    assert exhausted.last is not None
    assert exhausted.last.status == 429
    # Repeats in order, so the sequence distinguishes a retry from a failover.
    assert exhausted.attempted == ("alpha/alpha-small",) * 3 + ("beta/beta-small",) * 3


async def test_dispatching_against_an_empty_chain_is_a_programming_error(config) -> None:
    """Unreachable through `routing.resolve`, whose chain is never empty.

    Explicit so a future caller gets an exception rather than a silent no-op that
    looks like a provider outage.
    """
    with pytest.raises(ValueError):
        await dispatcher().run((), Script())


# -- concurrency -------------------------------------------------------------


async def test_one_dispatcher_serves_concurrent_requests_independently(chain) -> None:
    """The dispatcher is app-scoped, so its counters must be locals, not attributes.

    A shared `self._attempts` would work perfectly in every test above and report
    nonsense the moment two requests overlapped — which is every moment in
    production.
    """
    operations = [Script(alpha=[failing(503)] * i + [None]) for i in range(4)]
    shared = dispatcher()

    outcomes = await asyncio.gather(
        *(shared.run(chain, operation) for operation in operations)
    )

    assert [outcome.attempts for outcome in outcomes] == [1, 2, 3, 4]
    # The fourth exhausted alpha's three attempts and fell over to beta.
    assert [outcome.fallback for outcome in outcomes] == [False, False, False, True]
