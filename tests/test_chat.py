"""`POST /v1/chat/completions` end to end, over HTTP, against the real app.

Two things are being tested that unit tests cannot reach.

**The header contract.** `scripts/smoke_test.py` and `scripts/load_test.py` both
read `x-prism-*` off the response, and `scripts/load_test.py:57` treats a missing
header as a warning rather than a failure — so a regression there would show up as a
line of output nobody reads. These tests fail instead.

**Reconciliation.** `docs/API_CONTRACT.md:140` requires the cost a client observed
to equal the cost that was stored. That is a property of three separate writes (the
header, the `request_log` row, the budget counter) and only an end-to-end test can
compare them.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from prism.db.models import RequestLog, RequestStatus
from prism.db.session import Database
from prism.providers.fake import FakeProviderClient
from tests.conftest import (
    BUDGET_DEMO_KEY,
    FREE_KEY,
    RESEARCH_KEY,
    SEARCH_KEY,
    FakeClock,
    RecordingSleeper,
)

pytestmark = pytest.mark.postgres

CHAT = "/v1/chat/completions"


def body(model: str = "fast", prompt: str = "What is a load balancer?", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def log_for(db: Database, request_id: str) -> RequestLog:
    async with db.session() as session:
        return (
            await session.execute(
                select(RequestLog).where(RequestLog.request_id == request_id)
            )
        ).scalar_one()


async def all_logs(db: Database) -> list[RequestLog]:
    async with db.session() as session:
        return list(
            (await session.execute(select(RequestLog).order_by(RequestLog.created_at))).scalars()
        )


# -- the happy path ---------------------------------------------------------


async def test_a_completion_is_openai_shaped(client: AsyncClient) -> None:
    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["usage"]["prompt_tokens"] == 5
    # The concrete model that served it, not the alias the caller asked for — this is
    # what an OpenAI client displays and what a cost dashboard groups by.
    assert payload["model"] == "alpha-small"


async def test_every_required_header_is_present(client: AsyncClient) -> None:
    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.headers["x-prism-provider"] == "alpha/alpha-small"
    assert response.headers["x-prism-cache"] == "miss"
    assert response.headers["x-prism-fallback"] == "false"
    assert float(response.headers["x-prism-cost-usd"]) > 0


async def test_an_alias_reaches_the_upstream_as_a_concrete_model(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """The provider is asked for `alpha-small`; a provider asked for `fast` 404s."""
    await client.post(CHAT, json=body(model="fast"), headers=auth(SEARCH_KEY))
    assert providers.calls == ["alpha/alpha-small"]


async def test_unknown_request_fields_are_forwarded_untouched(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """OpenAI compatibility means not knowing better than the caller.

    Prism's request schema names three fields — `model`, `messages`, `stream`. Every
    other OpenAI parameter is unknown to it, and a gateway that dropped `temperature`
    would make every request non-deterministic with nothing in the response to explain
    why. `extra="allow"` plus `exclude_unset=True` is what makes this pass.
    """
    await client.post(
        CHAT,
        json=body(temperature=0, max_tokens=64, seed=7, tool_choice="none"),
        headers=auth(SEARCH_KEY),
    )
    sent = providers.payloads[0]
    assert sent["temperature"] == 0
    assert sent["max_tokens"] == 64
    assert sent["seed"] == 7
    assert sent["tool_choice"] == "none"
    # `stream` was never sent by the caller, so it must not be invented on the way
    # out: a provider that sees `stream: false` explicitly is fine, but one that sees
    # a field the caller never set cannot tell it was the gateway's idea.
    assert "stream" not in sent
    assert sent["model"] == "alpha-small"


# -- accounting -------------------------------------------------------------


async def test_the_cost_header_is_computed_from_the_price_table(client: AsyncClient) -> None:
    """Independently recomputed from `data/model_pricing.json`, not from the code."""
    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    usage = response.json()["usage"]
    expected = (
        Decimal("0.15") * usage["prompt_tokens"] / 1_000_000
        + Decimal("0.60") * usage["completion_tokens"] / 1_000_000
    )
    assert Decimal(response.headers["x-prism-cost-usd"]) == expected.quantize(
        Decimal("0.0000000001")
    )


async def test_header_log_row_and_budget_all_agree(
    client: AsyncClient, seeded: Database
) -> None:
    """The reconciliation property, over four requests.

    This is the test that would catch a cost rounded once for the header and again for
    the column, or a budget charged before a log row that then failed to write.
    """
    from prism.budget import spent_this_period
    from prism.db.models import Tenant

    client_side = Decimal(0)
    for _ in range(4):
        response = await client.post(
            CHAT, json=body(prompt=f"question {_}"), headers=auth(SEARCH_KEY)
        )
        assert response.status_code == 200
        client_side += Decimal(response.headers["x-prism-cost-usd"])

    rows = [row for row in await all_logs(seeded) if row.status == RequestStatus.OK.value]
    assert len(rows) == 4
    assert sum(row.cost_usd for row in rows) == client_side

    async with seeded.session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.team == "search"))
        ).scalar_one()
        assert await spent_this_period(session, tenant) == client_side


async def test_a_successful_request_is_logged_in_full(
    client: AsyncClient, seeded: Database
) -> None:
    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    row = await log_for(seeded, response.headers["x-request-id"])

    assert row.status == RequestStatus.OK.value
    assert row.http_status == 200
    assert row.team == "search"
    assert row.key_prefix == "prism-sk-search"
    assert row.requested_model == "fast"
    assert (row.resolved_provider, row.resolved_model) == ("alpha", "alpha-small")
    assert row.cache == "miss"
    assert row.fallback is False
    assert row.streamed is False
    assert row.retries == 0
    assert row.prompt_tokens == 5
    assert row.completion_tokens > 0
    assert row.latency_ms >= 0
    # No prompt, no completion. docs/DATA_MODEL.md:80 — storing bodies is a decision
    # with a retention policy attached, and this build does not store them.
    assert not hasattr(row, "prompt_text")


async def test_the_log_row_never_contains_the_virtual_key(
    client: AsyncClient, seeded: Database
) -> None:
    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    row = await log_for(seeded, response.headers["x-request-id"])
    assert SEARCH_KEY not in repr(row.__dict__)
    # The prefix is deliberately kept: enough to tell tenants apart in the console,
    # not enough to reconstruct the secret.
    assert row.key_prefix == "prism-sk-search"


# -- routing ----------------------------------------------------------------


async def test_the_auto_router_records_its_decision_in_the_log(
    client: AsyncClient, seeded: Database
) -> None:
    """`docs/DATA_MODEL.md:75` requires the tier and the reason for `auto` requests."""
    long_prompt = "Design a multi-region write-heavy storage layer " * 6
    response = await client.post(
        CHAT, json=body(model="auto", prompt=long_prompt), headers=auth(RESEARCH_KEY)
    )
    assert response.status_code == 200
    assert response.headers["x-prism-provider"] == "alpha/alpha-large"

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.requested_model == "auto"
    assert row.resolved_model == "alpha-large"
    assert "difficulty=complex" in row.route_reason
    assert "heuristic=length" in row.route_reason


async def test_a_direct_request_records_no_route_reason(
    client: AsyncClient, seeded: Database
) -> None:
    """"The caller asked for it" is not a routing decision."""
    response = await client.post(CHAT, json=body(model="fast"), headers=auth(SEARCH_KEY))
    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.route_reason is None


# -- rejections, and their log rows ----------------------------------------


async def test_a_missing_key_is_401_and_logged_without_a_tenant(
    client: AsyncClient, seeded: Database
) -> None:
    """The hardest rejection to log: it is raised in a dependency, before the route.

    The row carries no tenant and no requested model, because at rejection time
    neither was known — the body was never parsed. That is the honest record.
    """
    response = await client.post(CHAT, json=body())
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.REJECTED_AUTH.value
    assert row.http_status == 401
    assert row.tenant_id is None
    assert row.cost_usd == 0


async def test_a_model_off_the_allowlist_is_403_and_logged(
    client: AsyncClient, seeded: Database
) -> None:
    """`search` may use `fast` only."""
    response = await client.post(CHAT, json=body(model="smart"), headers=auth(SEARCH_KEY))
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "model_not_allowed"

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.REJECTED_ALLOWLIST.value
    assert row.requested_model == "smart"
    # Nothing was resolved, so nothing is recorded as resolved. Filling these in
    # would put a provider in the audit trail that was never called.
    assert row.resolved_provider is None


async def test_an_unknown_model_is_404_before_it_is_403(client: AsyncClient) -> None:
    """Answering 403 would confirm the model exists to anyone holding any key."""
    response = await client.post(
        CHAT, json=body(model="no-such-model-xyz"), headers=auth(SEARCH_KEY)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_a_rejected_model_does_not_consume_rate_limit_quota(
    client: AsyncClient, limiter, tenants
) -> None:
    """The reason the allowlist is checked before the limiter (prism/auth.py:22).

    Otherwise a caller could exhaust a team's quota with requests that could never
    have succeeded.
    """
    for _ in range(5):
        await client.post(CHAT, json=body(model="smart"), headers=auth(SEARCH_KEY))
    assert limiter.snapshot(tenants["search"].id) == 0


async def test_the_rate_limit_returns_429_with_retry_after_and_logs(
    client: AsyncClient, seeded: Database
) -> None:
    """`free-tier` is capped at 10 requests per minute."""
    statuses = []
    for index in range(11):
        response = await client.post(
            CHAT, json=body(prompt=f"q{index}"), headers=auth(FREE_KEY)
        )
        statuses.append(response.status_code)

    assert statuses.count(200) == 10
    assert statuses[-1] == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["type"] == "rate_limit_exceeded"

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.REJECTED_RATE_LIMIT.value
    assert row.cost_usd == 0


async def test_the_window_frees_quota_again(
    client: AsyncClient, clock: FakeClock
) -> None:
    """A rate limit is a delay, not a ban."""
    for index in range(10):
        await client.post(CHAT, json=body(prompt=f"q{index}"), headers=auth(FREE_KEY))
    assert (
        await client.post(CHAT, json=body(prompt="blocked"), headers=auth(FREE_KEY))
    ).status_code == 429

    clock.advance(61)
    assert (
        await client.post(CHAT, json=body(prompt="allowed"), headers=auth(FREE_KEY))
    ).status_code == 200


async def test_an_exhausted_budget_is_402_not_429(
    client: AsyncClient, seeded: Database
) -> None:
    """402, because `scripts/load_test.py:106` counts every 429 as rate limiting.

    Returning 429 here would make a budget-exhausted key indistinguishable from a
    rate-limited one in the load test's report — corrupting the one number that test
    exists to measure. See prism/errors.py:BudgetExceededError.
    """
    first = await client.post(CHAT, json=body(), headers=auth(BUDGET_DEMO_KEY))
    assert first.status_code == 200

    second = await client.post(CHAT, json=body(prompt="again"), headers=auth(BUDGET_DEMO_KEY))
    assert second.status_code == 402
    assert second.json()["error"]["type"] == "budget_exceeded"
    # Waiting does not help, so no Retry-After.
    assert "retry-after" not in {k.lower() for k in second.headers}

    row = await log_for(seeded, second.headers["x-request-id"])
    assert row.status == RequestStatus.REJECTED_BUDGET.value


async def test_an_exhausted_budget_makes_no_upstream_call(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """Admission before spend. A rejection that still paid a provider is not a budget."""
    await client.post(CHAT, json=body(), headers=auth(BUDGET_DEMO_KEY))
    calls_after_first = len(providers.calls)
    await client.post(CHAT, json=body(prompt="again"), headers=auth(BUDGET_DEMO_KEY))
    assert len(providers.calls) == calls_after_first


async def test_all_providers_failing_is_502_and_leaks_nothing(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database
) -> None:
    """The upstream's own message never reaches the client.

    It may quote the request, and on a misconfigured provider it may quote the
    credential we sent (`docs/DATA_MODEL.md:44`).
    """
    providers.set("alpha", mode="down")
    providers.set("beta", mode="down")

    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"
    assert "mock-key" not in response.text
    assert "injected" not in response.text
    assert "503" not in response.text

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.UPSTREAM_ERROR.value
    assert row.http_status == 502
    # The *last* target attempted, not the primary: the row answers "where did this
    # request end up", which for a failure that walked the chain is beta.
    assert row.resolved_provider == "beta"
    assert row.fallback is True
    # Three attempts on alpha, three on beta — six upstream calls, five of them extra.
    assert row.retries == 5
    assert providers.calls == ["alpha/alpha-small"] * 3 + ["beta/beta-small"] * 3


async def test_a_failed_request_is_not_charged(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database
) -> None:
    from prism.budget import spent_this_period
    from prism.db.models import Tenant

    providers.set("alpha", mode="down")
    providers.set("beta", mode="down")
    await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))

    async with seeded.session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.team == "search"))
        ).scalar_one()
        assert await spent_this_period(session, tenant) == 0


# -- malformed requests -----------------------------------------------------


async def test_an_empty_message_list_is_a_400_in_openai_shape(
    client: AsyncClient, seeded: Database
) -> None:
    """FastAPI's default here is a 422 with its own body shape.

    A client written against OpenAI branches on 400 and reads `error.message`, so a
    422 sends it down a path it has no handler for.
    """
    response = await client.post(
        CHAT, json={"model": "fast", "messages": []}, headers=auth(SEARCH_KEY)
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "messages"

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.INVALID_REQUEST.value


async def test_a_validation_error_never_echoes_the_prompt(client: AsyncClient) -> None:
    """`exc.errors()` carries the offending input, which here is tenant content."""
    secret = "the-users-private-question-abc123"
    response = await client.post(
        CHAT,
        json={"model": "fast", "messages": [{"content": secret}]},
        headers=auth(SEARCH_KEY),
    )
    assert response.status_code == 400
    assert secret not in response.text


async def test_a_missing_model_is_rejected_before_anything_is_spent(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    response = await client.post(
        CHAT, json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth(SEARCH_KEY)
    )
    assert response.status_code == 400
    assert providers.calls == []


# -- retries and failover ---------------------------------------------------


async def test_a_transient_failure_is_retried_on_the_same_provider(
    client: AsyncClient,
    providers: FakeProviderClient,
    seeded: Database,
    sleeper: RecordingSleeper,
) -> None:
    """A retry is not a fallback, and the header has to tell them apart.

    `fail_first=1` is the only way to express "fails once, then recovers" — see
    `prism/providers/fake.py:Behaviour`.
    """
    providers.set("alpha", mode="down", fail_first=1)

    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.status_code == 200
    assert response.headers["x-prism-provider"] == "alpha/alpha-small"
    assert response.headers["x-prism-fallback"] == "false"
    assert providers.calls == ["alpha/alpha-small"] * 2
    # One backoff, at the policy's initial 200 ms. Nothing waited for it; see
    # tests/conftest.py:RecordingSleeper.
    assert sleeper.delays == [0.2]

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.retries == 1
    assert row.fallback is False


async def test_a_dead_provider_fails_over_and_says_so_in_the_header(
    client: AsyncClient,
    providers: FakeProviderClient,
    seeded: Database,
    sleeper: RecordingSleeper,
) -> None:
    providers.set("alpha", mode="down")

    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.status_code == 200
    assert response.headers["x-prism-provider"] == "beta/beta-small"
    assert response.headers["x-prism-fallback"] == "true"
    assert response.json()["model"] == "beta-small"
    assert providers.calls == ["alpha/alpha-small"] * 3 + ["beta/beta-small"]
    # Two backoffs — both between retries of alpha. Moving to beta is not delayed: a
    # different provider is no likelier to answer because we waited first.
    assert sleeper.delays == [0.2, 0.4]

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.fallback is True
    assert row.resolved_provider == "beta"
    assert row.retries == 3


async def test_a_request_the_upstream_calls_malformed_is_not_failed_over(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """Every provider will reject a malformed request identically.

    Failing it over spends money and latency to be told the same thing twice.
    """
    providers.set("alpha", mode="bad_request")

    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.status_code == 502
    assert providers.calls == ["alpha/alpha-small"]


async def test_a_bad_credential_is_failed_over_but_never_retried(
    client: AsyncClient, providers: FakeProviderClient, sleeper: RecordingSleeper
) -> None:
    """A 401 means *our* stored key for that provider is wrong.

    Retrying cannot fix it; the next provider's key may well be fine. This is the
    case that proves `retry_same` and `try_next` are genuinely independent.
    """
    providers.set("alpha", mode="unauthorized")

    response = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert response.status_code == 200
    assert response.headers["x-prism-fallback"] == "true"
    assert providers.calls == ["alpha/alpha-small", "beta/beta-small"]
    assert sleeper.delays == []


# -- streaming --------------------------------------------------------------


def sse_events(text: str) -> list[str]:
    """The `data:` payloads of an SSE body, in order."""
    return [
        block[len("data:") :].strip()
        for block in text.split("\n\n")
        if block.startswith("data:")
    ]


def deltas(events: list[str]) -> str:
    """The assistant text reassembled from chunk deltas."""
    out = []
    for event in events:
        if event == "[DONE]" or event.startswith('{"error"'):
            continue
        chunk = json.loads(event)
        out.append(chunk["choices"][0]["delta"].get("content") or "")
    return "".join(out)


async def test_a_stream_is_sse_and_terminates_with_done(client: AsyncClient) -> None:
    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert events[-1] == "[DONE]"
    # More than one event, and the reply is split across them rather than delivered
    # whole in the first: this is the shape `docs/API_CONTRACT.md:85` requires.
    assert len(events) > 3
    assert json.loads(events[0])["choices"][0]["delta"]["role"] == "assistant"


async def test_a_stream_carries_the_headers_but_not_the_cost(client: AsyncClient) -> None:
    """`docs/API_CONTRACT.md:88`: the cost is not known when the headers are flushed.

    A `content-length` would mean Starlette had buffered the whole body before
    sending it, which is the failure `docs/IMPLEMENTATION_GUIDE.md:96` describes.
    """
    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert response.headers["x-prism-provider"] == "alpha/alpha-small"
    assert response.headers["x-prism-cache"] == "miss"
    assert response.headers["x-prism-fallback"] == "false"
    assert "x-prism-cost-usd" not in response.headers
    assert "content-length" not in response.headers


async def test_the_upstream_is_told_to_stream(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """Whether Prism streams and whether the upstream streams are one decision.

    A gateway that opened a non-streaming upstream call and chunked the finished
    answer itself would pass a naive SSE test and still deliver the whole reply at
    once, after the full generation latency.
    """
    await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert providers.payloads[0]["stream"] is True

    providers.reset()
    await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    assert "stream" not in providers.payloads[0]


async def test_a_streamed_request_is_metered_once_the_stream_ends(
    client: AsyncClient, seeded: Database, tenants: dict
) -> None:
    """The cost the headers could not carry still has to reach the log and the budget.

    `docs/API_CONTRACT.md:88` requires it, and it is the only path where the row is
    written after the response has already been delivered.
    """
    from prism.budget import spent_this_period

    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert response.status_code == 200

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.streamed is True
    assert row.status == RequestStatus.OK.value
    assert row.http_status == 200
    # Usage arrives on the final chunk (scripts/mock_provider.py:203), so a gateway
    # that stopped reading at the first chunk would record zeros here.
    assert row.prompt_tokens == 5
    assert row.completion_tokens > 0
    assert row.cost_usd > 0

    async with seeded.session() as session:
        spent = await spent_this_period(session, tenants["search"])
    assert spent == row.cost_usd


async def test_a_streams_content_matches_the_non_streamed_answer(
    client: AsyncClient,
) -> None:
    """Nothing is dropped, duplicated or re-serialised on the way through.

    The same prompt gives the same reply from the fake (`crc32`, not `hash`), so the
    reassembled deltas must equal the non-streaming body exactly.
    """
    streamed = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    plain = await client.post(CHAT, json=body(), headers=auth(SEARCH_KEY))
    expected = plain.json()["choices"][0]["message"]["content"]
    assert deltas(sse_events(streamed.text)).strip() == expected.strip()


async def test_a_stream_fails_over_before_the_first_token(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database
) -> None:
    """Failover is safe here precisely because nothing has been forwarded yet.

    That is the open/iterate split in `prism/providers/base.py`, and the header has
    to reflect the provider that actually served the stream.
    """
    providers.set("alpha", mode="down")

    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert response.status_code == 200
    assert response.headers["x-prism-provider"] == "beta/beta-small"
    assert response.headers["x-prism-fallback"] == "true"
    assert sse_events(response.text)[-1] == "[DONE]"

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.fallback is True
    assert row.streamed is True
    assert row.cost_usd > 0


async def test_a_chain_that_cannot_open_a_stream_is_a_json_502(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database
) -> None:
    """Not a 200 whose body is one error event.

    The stream is opened before the response exists, so a total failure still gets a
    real status line — which is what an OpenAI client branches on.
    """
    providers.set("alpha", mode="down")
    providers.set("beta", mode="down")

    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"
    assert "mock-key" not in response.text

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.UPSTREAM_ERROR.value
    assert row.http_status == 502


async def test_a_stream_that_dies_mid_response_is_terminated_not_spliced(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database
) -> None:
    """The documented answer to `docs/IMPLEMENTATION_GUIDE.md:172`.

    Terminate with an error event; never restart on another provider and splice the
    outputs. The `beta` assertion is the important half — it is what "no splicing"
    means in code.
    """
    providers.set("alpha", die_after_chunks=3)

    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert response.status_code == 200

    events = sse_events(response.text)
    assert len(events) == 5  # three chunks, one error, then the terminator
    assert json.loads(events[3])["error"]["type"] == "upstream_error"
    # `[DONE]` is still sent: a client with no terminator waits out its own read
    # timeout before showing the user anything.
    assert events[-1] == "[DONE]"
    assert providers.calls == ["alpha/alpha-small"]

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.status == RequestStatus.UPSTREAM_ERROR.value
    # 200 is what went out on the wire, and the row must not claim otherwise.
    assert row.http_status == 200
    assert row.streamed is True


async def test_a_dying_stream_leaks_nothing_to_the_client(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    providers.set("alpha", die_after_chunks=2)
    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))
    assert "injected" not in response.text
    assert "mock-key" not in response.text


async def test_a_stream_that_died_before_its_usage_chunk_is_not_charged(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database, tenants: dict
) -> None:
    """Usage arrives last, so a stream that dies has none — and none is billed.

    Documented in the README's known limitations: a provider that never reports usage
    is un-billable rather than un-servable. Estimating tokens ourselves would put a
    number into the accounting that no provider ever agreed to.
    """
    from prism.budget import spent_this_period

    providers.set("alpha", die_after_chunks=2)
    response = await client.post(CHAT, json=body(stream=True), headers=auth(SEARCH_KEY))

    row = await log_for(seeded, response.headers["x-request-id"])
    assert row.completion_tokens == 0
    assert row.cost_usd == 0

    async with seeded.session() as session:
        assert await spent_this_period(session, tenants["search"]) == 0


async def test_streaming_still_obeys_the_budget(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """Admission does not know or care whether a request streams.

    `docs/DATA_MODEL.md:91` permits a stream to overshoot by its own final cost, but
    it must still be refused once nothing is left.
    """
    first = await client.post(
        CHAT, json=body(stream=True), headers=auth(BUDGET_DEMO_KEY)
    )
    assert first.status_code == 200
    # Drain it, so the charge in the generator's `finally` has actually happened.
    assert sse_events(first.text)[-1] == "[DONE]"

    second = await client.post(
        CHAT, json=body(prompt="again", stream=True), headers=auth(BUDGET_DEMO_KEY)
    )
    assert second.status_code == 402
    assert second.headers["content-type"].startswith("application/json")


# -- isolation --------------------------------------------------------------


async def test_the_admin_plane_is_not_written_to_the_request_log(
    client: AsyncClient, seeded: Database
) -> None:
    """The log is the audit trail for tenant traffic.

    Mixing operator requests into it would corrupt every count the usage API derives.
    """
    await client.get("/healthz")
    await client.get("/readyz")
    assert await all_logs(seeded) == []
