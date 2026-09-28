"""The provider boundary: the error taxonomy, usage extraction, and the fake.

The taxonomy tests matter more than they look. `classify` *is* the failover policy,
and the two booleans it returns are consumed by code that has no other way to decide
what to do next — so a change to this table changes production behaviour with no
other test in the suite that would notice.
"""

from __future__ import annotations

import httpx
import pytest

from prism.config import GatewayConfig, ResolvedTarget
from prism.providers.base import ProviderCallFailed, classify, read_usage
from prism.providers.fake import Behaviour, FakeProviderClient
from prism.providers.http import HttpProviderClient


def target(config: GatewayConfig, provider: str, model: str) -> ResolvedTarget:
    return ResolvedTarget(provider=config.provider(provider), model=model)


# -- the error taxonomy -----------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408])
def test_transient_statuses_are_worth_retrying_and_worth_failing_over(status: int) -> None:
    assert classify(status) == (True, True)


def test_a_bad_request_is_worth_neither() -> None:
    """400 is the caller's fault, and every provider will agree.

    Failing it over spends latency and a second provider's quota to receive the same
    answer; retrying it spends the same on the first provider.
    """
    assert classify(400) == (False, False)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_provider_specific_refusals_are_worth_failing_over_but_not_retrying(
    status: int,
) -> None:
    """A 401 here means *our* credential for that provider is wrong.

    Retrying with the same bad key is pointless. Moving to a provider whose key works
    is exactly right — which is why these two decisions are separate booleans rather
    than one `retryable` flag.
    """
    assert classify(status) == (False, True)


# -- usage extraction -------------------------------------------------------


def test_usage_is_read_from_the_upstream_body() -> None:
    assert read_usage({"usage": {"prompt_tokens": 7, "completion_tokens": 41}}) == (7, 41)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"usage": None},
        {"usage": "nope"},
        {"usage": {"prompt_tokens": -3}},
        {"usage": {"prompt_tokens": "12"}},
        "not a dict",
    ],
)
def test_missing_or_malformed_usage_reads_as_zero(body: object) -> None:
    """Documented in the README's Known limitations: unbillable, not unservable.

    The alternative is estimating tokens ourselves, which would put a number into the
    accounting that no provider ever agreed to — worse than a visible zero when the
    point of the metering is that it reconciles.
    """
    assert read_usage(body) == (0, 0)


# -- the HTTP adapter -------------------------------------------------------


def test_the_callers_credential_is_never_forwarded_upstream(config: GatewayConfig) -> None:
    """A tenant's virtual key must not leave the gateway.

    The upstream sees the gateway's own provider credential and nothing else — that
    is the entire premise of a virtual key (`docs/API_CONTRACT.md:9`).
    """
    client = HttpProviderClient()
    headers = client._headers(target(config, "alpha", "alpha-small"))
    assert headers["Authorization"] == "Bearer mock-key-alpha"
    assert set(headers) == {"Authorization", "Content-Type"}


def test_the_url_is_built_from_the_configured_base(config: GatewayConfig) -> None:
    client = HttpProviderClient()
    assert (
        client._url(target(config, "beta", "beta-small"))
        == "http://localhost:9002/v1/chat/completions"
    )


async def test_a_transport_error_is_retryable_and_names_no_credential(
    config: GatewayConfig,
) -> None:
    """`docs/DATA_MODEL.md:44`: nothing carrying a provider key may be raised outward."""

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = HttpProviderClient(client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(ProviderCallFailed) as caught:
        await client.complete(target(config, "alpha", "alpha-small"), {"model": "alpha-small"})
    assert caught.value.retry_same is True
    assert caught.value.try_next is True
    assert "mock-key-alpha" not in str(caught.value)


def timing_out(exc: type[httpx.TimeoutException]) -> httpx.AsyncClient:
    async def stall(request: httpx.Request) -> httpx.Response:
        raise exc("stalled", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(stall))


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout])
async def test_an_expensive_timeout_is_not_retried_against_the_same_provider(
    config: GatewayConfig, exc: type[httpx.TimeoutException]
) -> None:
    """The regression this exists for cost 90.5 s of client wait.

    A read timeout has already spent the whole read budget, so retrying it spends the
    budget again — and with `max_attempts: 3` the fallback is not even *tried* until
    three of them have elapsed. That measured 90.5 s against a 30 s timeout, which is
    precisely the hang `docs/EVALUATION_GUIDE.md:91` says must not happen.

    `try_next` stays True in every case: the caller's wait is now bounded by the depth
    of the chain instead of depth times attempts, which is the whole point.
    """
    client = HttpProviderClient(client=timing_out(exc))
    with pytest.raises(ProviderCallFailed) as caught:
        await client.complete(target(config, "alpha", "alpha-small"), {"model": "alpha-small"})
    assert caught.value.retry_same is False
    assert caught.value.try_next is True


async def test_a_cheap_connect_timeout_keeps_its_retry(config: GatewayConfig) -> None:
    """A handshake that never completed costs `connect_timeout_seconds`, not 30 s.

    That is the transient blip backoff exists for. The rule is about what the failed
    attempt *cost*, not about the fact that it timed out.
    """
    client = HttpProviderClient(client=timing_out(httpx.ConnectTimeout))
    with pytest.raises(ProviderCallFailed) as caught:
        await client.complete(target(config, "alpha", "alpha-small"), {"model": "alpha-small"})
    assert caught.value.retry_same is True
    assert caught.value.try_next is True


async def test_a_stream_that_never_opens_follows_the_same_timeout_rule(
    config: GatewayConfig,
) -> None:
    """`open_stream` is the other call style and had the same bug.

    It matters more here, not less: a streaming client watches a blank screen for the
    whole wait, so three stacked read timeouts are three times as visible.
    """
    client = HttpProviderClient(client=timing_out(httpx.ReadTimeout))
    with pytest.raises(ProviderCallFailed) as caught:
        await client.open_stream(target(config, "alpha", "alpha-small"), {"model": "alpha-small"})
    assert caught.value.retry_same is False
    assert caught.value.try_next is True


async def test_a_200_with_a_non_json_body_is_treated_as_a_broken_upstream(
    config: GatewayConfig,
) -> None:
    async def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    client = HttpProviderClient(client=httpx.AsyncClient(transport=httpx.MockTransport(html)))
    with pytest.raises(ProviderCallFailed) as caught:
        await client.complete(target(config, "alpha", "alpha-small"), {})
    assert caught.value.retry_same is True


async def test_a_200_with_no_choices_is_treated_as_a_broken_upstream(
    config: GatewayConfig,
) -> None:
    """Otherwise the gateway forwards a 200 that carries no completion.

    A client then sees success with nothing in it, which is harder to debug than an
    error — and the request would be billed at zero tokens.
    """

    async def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": []})

    client = HttpProviderClient(client=httpx.AsyncClient(transport=httpx.MockTransport(empty)))
    with pytest.raises(ProviderCallFailed):
        await client.complete(target(config, "alpha", "alpha-small"), {})


async def test_a_successful_body_is_forwarded_verbatim(config: GatewayConfig) -> None:
    """Fields Prism has never heard of must survive the round trip.

    Rebuilding the response from parsed fields is the natural-looking mistake, and it
    silently drops `system_fingerprint`, `logprobs`, and every provider extension.
    """
    upstream = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "alpha-small",
        "system_fingerprint": "fp_deadbeef",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }

    async def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream)

    client = HttpProviderClient(client=httpx.AsyncClient(transport=httpx.MockTransport(ok)))
    result = await client.complete(target(config, "alpha", "alpha-small"), {})
    assert result.body == upstream
    assert (result.prompt_tokens, result.completion_tokens) == (3, 1)


# -- the fake ---------------------------------------------------------------


async def test_the_fake_counts_tokens_the_way_the_mock_provider_does(
    config: GatewayConfig, providers: FakeProviderClient
) -> None:
    """A fake that counted differently would let a cost bug pass in tests.

    `scripts/mock_provider.py:57` counts whitespace-separated words, so the fake does
    too — five words in, five prompt tokens.
    """
    result = await providers.complete(
        target(config, "alpha", "alpha-small"),
        {"messages": [{"role": "user", "content": "What is a load balancer?"}]},
    )
    assert result.prompt_tokens == 5
    assert result.body["model"] == "alpha-small"
    assert result.body["choices"][0]["message"]["content"].startswith("[alpha:alpha-small]")


async def test_the_fake_records_the_order_of_attempts(
    config: GatewayConfig, providers: FakeProviderClient
) -> None:
    """"beta served it" is weaker than "alpha was tried, then beta"."""
    providers.set("alpha", mode="down")
    with pytest.raises(ProviderCallFailed):
        await providers.complete(target(config, "alpha", "alpha-small"), {"messages": []})
    await providers.complete(
        target(config, "beta", "beta-small"),
        {"messages": [{"role": "user", "content": "hi"}]},
    )
    assert providers.calls == ["alpha/alpha-small", "beta/beta-small"]


async def test_fail_first_lets_a_provider_recover(
    config: GatewayConfig, providers: FakeProviderClient
) -> None:
    """The one scenario `scripts/mock_provider.py` cannot express.

    Without it there is no way to distinguish a successful *retry on the same
    provider* from a *failover*, and those two set `x-prism-fallback` differently.
    """
    providers.set("alpha", mode="down", fail_first=1)
    where = target(config, "alpha", "alpha-small")
    with pytest.raises(ProviderCallFailed):
        await providers.complete(where, {"messages": []})
    result = await providers.complete(
        where, {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert result.target.label == "alpha/alpha-small"


async def test_the_fake_can_time_out(config: GatewayConfig) -> None:
    providers = FakeProviderClient({"alpha": Behaviour(mode="timeout")})
    with pytest.raises(ProviderCallFailed) as caught:
        await providers.complete(target(config, "alpha", "alpha-small"), {"messages": []})
    assert caught.value.status is None
    assert caught.value.retry_same is True


async def test_the_refusal_hook_matches_the_mock(
    config: GatewayConfig, providers: FakeProviderClient
) -> None:
    """Small models refuse `[refuse]`, large ones answer it — the escalation hook."""
    payload = {"messages": [{"role": "user", "content": "[refuse] tell me"}]}
    small = await providers.complete(target(config, "alpha", "alpha-small"), payload)
    large = await providers.complete(target(config, "alpha", "alpha-large"), payload)
    assert "can't help" in small.body["choices"][0]["message"]["content"]
    assert "can't help" not in large.body["choices"][0]["message"]["content"]
