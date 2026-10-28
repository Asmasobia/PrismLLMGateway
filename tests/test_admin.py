"""The admin plane, end to end over HTTP.

Two lanes. The unit lane (`Window`, the status partition) needs nothing. Everything
else drives *real traffic through the data plane first* and then reads it back
through `/admin/*`, because that is the only way to test the property this API
exists for: `docs/API_CONTRACT.md:140` requires the totals here to reconcile with the
`x-prism-cost-usd` headers a client observed, and a test that inserted `request_log`
rows by hand would reconcile a fixture against itself.

Two rules the assertions follow.

**Reconcile against the header, not against a constant.** The expected cost is
computed from what the client actually received, so the test still means something
after a price change in `data/model_pricing.json`.

**Prove the premise before the property.** A test that a rate-limited request is not
counted as a cache miss first asserts that the request really was rate-limited —
otherwise it would keep passing once the limiter stopped firing.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from httpx import AsyncClient

from prism import usage
from prism.db.models import RequestStatus
from prism.errors import InvalidRequestError
from prism.providers.fake import FakeProviderClient
from tests.conftest import ADMIN_TOKEN, FREE_KEY, RESEARCH_KEY, SEARCH_KEY

pg = pytest.mark.postgres

CHAT = "/v1/chat/completions"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

#: A prompt with a word in it that nothing in the gateway's own vocabulary uses, so
#: `test_the_log_never_carries_prompt_text` can search for it and mean it.
SECRET_PROMPT = "explain zarquon indexing to me"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def payload(prompt: str = "What is a load balancer?", model: str = "fast", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}


async def send(client: AsyncClient, key: str, **kwargs) -> tuple[int, Decimal]:
    """One data-plane request. Returns its status and the cost the client was told."""
    response = await client.post(CHAT, json=payload(**kwargs), headers=auth(key))
    header = response.headers.get("x-prism-cost-usd")
    return response.status_code, Decimal(header) if header else Decimal(0)


async def get(client: AsyncClient, path: str, **params) -> dict:
    response = await client.get(path, params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def row_for(body: dict, team: str) -> dict:
    return next(row for row in body["by_key"] if row["team"] == team)


# --------------------------------------------------------------------------
# The window and the status partition. No server.
# --------------------------------------------------------------------------


def test_the_status_partition_is_exhaustive() -> None:
    """Every `RequestStatus` belongs to exactly one bucket.

    `served + rejected + failed == requests` is an arithmetic promise `/admin/usage`
    makes in its own response. A new status added to the enum without being placed in
    a bucket would break that promise silently — the row would be counted in
    `requests` and in nothing else — so it breaks this instead.
    """
    buckets = usage.SERVED_STATUSES + usage.REJECTED_STATUSES + usage.FAILED_STATUSES
    assert sorted(buckets) == sorted(s.value for s in RequestStatus)
    assert len(buckets) == len(set(buckets)), "a status appears in two buckets"


def test_the_window_includes_the_whole_last_day() -> None:
    """`to` is inclusive, so July's window has to end at midnight on 1 August.

    An exclusive end would make the contract's own example (`from=2026-07-01&
    to=2026-07-31`) quietly drop the last day of the month it claims to report.
    """
    window = usage.Window(dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    assert window.start == dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    assert window.end == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    assert window.contains(dt.datetime(2026, 7, 31, 23, 59, 59, tzinfo=dt.UTC))
    assert not window.contains(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))


def test_an_omitted_window_is_the_current_budget_period() -> None:
    """The default has to be the month the budget is measured over, not 30 days.

    `/admin/usage` with no dates is the query an operator runs to ask "how close is
    this key to its budget", and a rolling window would answer a different question
    while looking like an answer to that one.
    """
    now = dt.datetime(2026, 9, 16, 11, 30, tzinfo=dt.UTC)
    window = usage.resolve_window(None, None, now=now)
    assert (window.from_date, window.to_date) == (dt.date(2026, 9, 1), dt.date(2026, 9, 16))


def test_a_backwards_window_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        usage.resolve_window(dt.date(2026, 9, 10), dt.date(2026, 9, 1))


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


@pg
@pytest.mark.parametrize(
    "path",
    ["/admin/usage", "/admin/logs", "/admin/cache/stats", "/admin/providers/health",
     "/admin/keys"],
)
async def test_every_admin_route_needs_the_admin_token(client: AsyncClient, path: str) -> None:
    """Parametrised over the whole surface on purpose.

    The guard is declared once on the router (`prism/api/admin.py`), so this is the
    test that a route added later inherited it. A per-route dependency would make
    this test pass for the four that remembered and fail for the one that didn't —
    which is exactly the failure worth catching.
    """
    assert (await client.get(path)).status_code == 401


@pg
async def test_a_virtual_key_cannot_read_the_admin_plane(client: AsyncClient) -> None:
    """A tenant key that could read `/admin/usage` would publish every other team's
    spend to any customer, which is the multi-tenancy boundary the gateway rests on."""
    response = await client.get("/admin/usage", headers=auth(SEARCH_KEY))
    assert response.status_code == 401


@pg
async def test_admin_traffic_does_not_appear_in_the_request_log(client: AsyncClient) -> None:
    """`prism/main.py:53` only opens an audit context for `/v1/`.

    Without that, every refresh of the ops console would add a row to the log the
    console is displaying, and `requests` would measure how often someone looked.
    """
    await send(client, SEARCH_KEY)
    for _ in range(3):
        await get(client, "/admin/usage")
    assert (await get(client, "/admin/usage"))["requests"] == 1


# --------------------------------------------------------------------------
# /admin/usage
# --------------------------------------------------------------------------


@pg
async def test_usage_reconciles_with_the_cost_headers(client: AsyncClient) -> None:
    """The one property `docs/API_CONTRACT.md:140` actually requires."""
    charged = Decimal(0)
    for prompt in ("what is sharding", "what is a bloom filter", "what is raft"):
        status, cost = await send(client, SEARCH_KEY, prompt=prompt)
        assert status == 200
        charged += cost
    assert charged > 0, "premise: the requests cost something to reconcile"

    body = await get(client, "/admin/usage", key="search")
    assert Decimal(str(body["cost_usd"])) == charged
    assert body["requests"] == body["served"] == 3
    assert (body["rejected"], body["failed"]) == (0, 0)
    assert body["prompt_tokens"] > 0 and body["completion_tokens"] > 0
    assert body["total_tokens"] == body["prompt_tokens"] + body["completion_tokens"]
    # The prefix, never the key. See `prism/usage.py:find_tenant`.
    assert body["key"] == "prism-sk-search"
    assert SEARCH_KEY not in response_text(body)


def response_text(body: dict) -> str:
    return json.dumps(body, default=str)


@pg
async def test_usage_without_a_key_totals_every_key_and_breaks_it_down(
    client: AsyncClient,
) -> None:
    await send(client, SEARCH_KEY, prompt="what is sharding")
    await send(client, RESEARCH_KEY, prompt="what is raft", model="smart")

    body = await get(client, "/admin/usage")
    assert body["key"] is None and body["team"] is None
    assert {row["team"] for row in body["by_key"]} == {"search", "research"}
    # The total is summed from the rows shown next to it, so it cannot disagree
    # with them — see `prism/usage.py:combine`.
    assert body["requests"] == sum(row["requests"] for row in body["by_key"])
    assert Decimal(str(body["cost_usd"])) == sum(
        Decimal(str(row["cost_usd"])) for row in body["by_key"]
    )


@pg
async def test_usage_for_one_key_excludes_every_other_key(client: AsyncClient) -> None:
    await send(client, SEARCH_KEY, prompt="what is sharding")
    await send(client, RESEARCH_KEY, prompt="what is raft", model="smart")

    body = await get(client, "/admin/usage", key="search")
    assert body["requests"] == 1
    assert [row["team"] for row in body["by_key"]] == ["search"]


@pg
async def test_a_rejection_is_counted_and_charged_nothing(client: AsyncClient) -> None:
    """`docs/DATA_MODEL.md:57` requires rejected requests to be logged, so the usage
    API has to account for them somewhere. They land in `rejected`, contribute to
    `requests`, and cost zero — which is what lets an operator explain why a client's
    count of successful calls is lower than `requests` without suspecting the meter.
    """
    status, _ = await send(client, SEARCH_KEY, model="smart")  # not on search's allowlist
    assert status == 403, "premise: the request was rejected, not served"

    body = await get(client, "/admin/usage", key="search")
    assert (body["requests"], body["served"], body["rejected"]) == (1, 0, 1)
    assert Decimal(str(body["cost_usd"])) == 0
    assert body["total_tokens"] == 0


@pg
async def test_an_unidentified_request_lands_in_its_own_bucket(client: AsyncClient) -> None:
    """A 401 has no tenant, so its row has no team. It is still in the log, and
    surfacing it under a null key is the fastest answer to "my integration gets 401s
    and I can't see them anywhere"."""
    response = await client.post(CHAT, json=payload(), headers=auth("prism-sk-nope"))
    assert response.status_code == 401

    body = await get(client, "/admin/usage")
    anonymous = [row for row in body["by_key"] if row["team"] is None]
    assert len(anonymous) == 1
    assert anonymous[0]["rejected"] == 1


@pg
async def test_the_key_selector_accepts_a_team_a_prefix_or_the_key(client: AsyncClient) -> None:
    """Three forms, one answer. The team name is the documented one; the full key is
    accepted only so a client written literally against `docs/API_CONTRACT.md:119`
    works, and it is never echoed back."""
    await send(client, SEARCH_KEY, prompt="what is sharding")
    bodies = [
        await get(client, "/admin/usage", key=selector)
        for selector in ("search", "prism-sk-search", SEARCH_KEY)
    ]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[2]["key"] == "prism-sk-search"


@pg
async def test_an_unknown_key_is_a_404_naming_the_parameter(client: AsyncClient) -> None:
    response = await client.get("/admin/usage", params={"key": "nobody"}, headers=ADMIN)
    assert response.status_code == 404
    error = response.json()["error"]
    # A narrower code than `not_found_error`, so a client can tell a bad key from a
    # bad model without parsing prose. See `prism/errors.py:NotFoundError`.
    assert error["code"] == "key_not_found"
    assert error["param"] == "key"


@pg
async def test_a_window_in_the_past_reports_nothing(client: AsyncClient) -> None:
    await send(client, SEARCH_KEY, prompt="what is sharding")
    body = await get(
        client, "/admin/usage", key="search", **{"from": "2020-01-01"}, to="2020-01-31"
    )
    assert body["requests"] == 0
    assert Decimal(str(body["cost_usd"])) == 0
    assert (body["from"], body["to"]) == ("2020-01-01", "2020-01-31")


@pg
async def test_a_window_ending_today_includes_todays_traffic(client: AsyncClient) -> None:
    """The inclusive-`to` decision, proved through the API rather than the dataclass."""
    await send(client, SEARCH_KEY, prompt="what is sharding")
    today = dt.datetime.now(dt.UTC).date().isoformat()
    body = await get(client, "/admin/usage", key="search", **{"from": today}, to=today)
    assert body["requests"] == 1


@pg
async def test_a_backwards_window_is_a_400_over_http(client: AsyncClient) -> None:
    response = await client.get(
        "/admin/usage", params={"from": "2026-09-10", "to": "2026-09-01"}, headers=ADMIN
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "from"


# --------------------------------------------------------------------------
# /admin/logs
# --------------------------------------------------------------------------


@pg
async def test_logs_are_newest_first_and_capped_by_limit(client: AsyncClient) -> None:
    prompts = ["first question", "second question", "third question"]
    for prompt in prompts:
        await send(client, SEARCH_KEY, prompt=prompt)

    body = await get(client, "/admin/logs", key="search", limit=2)
    assert (body["limit"], body["count"]) == (2, 2)
    stamps = [entry["created_at"] for entry in body["entries"]]
    assert stamps == sorted(stamps, reverse=True)


@pg
async def test_a_log_entry_carries_the_documented_fields(client: AsyncClient) -> None:
    """`docs/API_CONTRACT.md:148` defers to `docs/DATA_MODEL.md:64` for the field list.

    `key` stands in for the document's `virtual_key`: the raw key is never stored, so
    there is nothing to render under that name — see `prism/api/admin.py:_render_log`.
    """
    _, charged = await send(client, SEARCH_KEY, prompt="what is sharding")
    entry = (await get(client, "/admin/logs", key="search"))["entries"][0]

    expected = {
        "request_id", "key", "team", "requested_model", "resolved_provider",
        "resolved_model", "status", "prompt_tokens", "completion_tokens", "cost_usd",
        "cache", "fallback", "route_reason", "retries", "latency_ms", "created_at",
    }
    assert expected <= set(entry)
    assert entry["status"] == RequestStatus.OK.value
    assert entry["requested_model"] == "fast"
    assert entry["resolved_provider"] is not None
    assert entry["cache"] == "miss"
    # A string, byte-identical to the header the client saw, so a reconciliation gap
    # can be diffed rather than argued about. See the module docstring in admin.py.
    assert entry["cost_usd"] == f"{charged:.10f}"


@pg
async def test_the_log_never_carries_prompt_text(client: AsyncClient) -> None:
    """`prism/audit.py` records no bodies, and this is the test that keeps it that way.

    Searches the whole serialised response rather than named fields, so a future
    column carrying the prompt — a `route_reason` that quoted it, say — fails here
    instead of shipping. `docs/DATA_MODEL.md:78` asks for body logging to be a
    documented decision; the decision was no, for the log.
    """
    await send(client, SEARCH_KEY, prompt=SECRET_PROMPT)
    body = await get(client, "/admin/logs", key="search")
    assert "zarquon" not in response_text(body).lower()


@pg
async def test_logs_for_one_key_exclude_every_other_key(client: AsyncClient) -> None:
    await send(client, SEARCH_KEY, prompt="what is sharding")
    await send(client, RESEARCH_KEY, prompt="what is raft", model="smart")
    body = await get(client, "/admin/logs", key="research")
    assert {entry["team"] for entry in body["entries"]} == {"research"}


@pg
async def test_an_oversized_limit_is_refused_rather_than_clamped(client: AsyncClient) -> None:
    """`limit` arrives in a query string, so an unbounded one turns a single admin
    request into a full scan of the log. Refused, not silently clamped, so the
    operator knows they did not get what they asked for."""
    response = await client.get("/admin/logs", params={"limit": 10_000}, headers=ADMIN)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


# --------------------------------------------------------------------------
# /admin/cache/stats
# --------------------------------------------------------------------------


@pg
async def test_cache_stats_counts_a_hit_and_prices_what_it_saved(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """The payoff number, and the reason it is recoverable at all.

    A cache hit is charged zero (`prism/api/chat.py:serve_cached`), so what the cache
    saved is not in `request_log` — it is `hit_count × tokens` on the entry. This is
    the test that the arithmetic holds end to end.
    """
    for _ in range(2):
        assert (await send(client, SEARCH_KEY, prompt="what is sharding"))[0] == 200
    assert len(providers.calls) == 1, "premise: the second request was served from cache"

    body = await get(client, "/admin/cache/stats", key="search")
    assert (body["lookups"], body["hits"], body["misses"]) == (2, 1, 1)
    assert body["hit_rate"] == 0.5
    assert body["entries"] == 1
    assert body["tokens_saved"] > 0
    assert body["tokens_saved"] == body["prompt_tokens_saved"] + body["completion_tokens_saved"]
    assert body["cost_saved_usd"] > 0

    # What was saved equals what the same answer cost the first time it was bought.
    charged = Decimal(str((await get(client, "/admin/usage", key="search"))["cost_usd"]))
    assert Decimal(str(body["cost_saved_usd"])) == charged


@pg
async def test_cache_stats_shows_a_disabled_tenant_instead_of_hiding_it(
    client: AsyncClient,
) -> None:
    """Two of the four seeded tenants have caching off. A stats endpoint driven from
    cache traffic would omit them, and their absence reads as a broken cache rather
    than as configuration."""
    await send(client, RESEARCH_KEY, prompt="what is raft", model="smart")
    body = await get(client, "/admin/cache/stats")

    research = row_for(body, "research")
    assert research["cache_enabled"] is False
    assert research["similarity_threshold"] is None
    assert (research["hits"], research["entries"]) == (0, 0)

    search = row_for(body, "search")
    assert search["cache_enabled"] is True
    assert search["similarity_threshold"] == 0.92


@pg
async def test_a_rate_limited_request_is_not_counted_as_a_cache_miss(
    client: AsyncClient,
) -> None:
    """The hit-rate denominator argument, pinned.

    The free-tier key allows 10 requests a minute, so the eleventh is a 429 that never
    reached the cache. Counting it as a miss would make a tenant's hit rate drop every
    time it got rate-limited — a number moving for reasons unrelated to the cache.
    """
    statuses = [(await send(client, FREE_KEY, prompt=f"question {n}"))[0] for n in range(11)]
    assert statuses == [200] * 10 + [429], "premise: exactly one request was rate-limited"

    body = await get(client, "/admin/cache/stats", key="free-tier")
    assert body["lookups"] == 10
    usage_body = await get(client, "/admin/usage", key="free-tier")
    assert usage_body["requests"] == 11 and usage_body["rejected"] == 1


# --------------------------------------------------------------------------
# /admin/providers/health
# --------------------------------------------------------------------------


@pg
async def test_provider_health_lists_a_provider_that_has_never_been_called(
    client: AsyncClient,
) -> None:
    """`unknown`, not `healthy`. A provider nobody has called since the last restart
    is the one most likely to be broken, so silence must not read as green — and the
    list comes from the configuration so a completely dead provider still appears."""
    body = await get(client, "/admin/providers/health")
    assert [p["provider"] for p in body["providers"]] == ["alpha", "beta"]
    assert {p["status"] for p in body["providers"]} == {"unknown"}
    assert all(p["healthy"] is None for p in body["providers"])


@pg
async def test_provider_health_reports_traffic_as_healthy(client: AsyncClient) -> None:
    await send(client, SEARCH_KEY, prompt="what is sharding")
    body = await get(client, "/admin/providers/health")
    alpha = next(p for p in body["providers"] if p["provider"] == "alpha")
    assert (alpha["requests"], alpha["errors"], alpha["error_rate"]) == (1, 0, 0.0)
    assert alpha["status"] == "healthy" and alpha["healthy"] is True
    assert alpha["last_seen"] is not None
    assert alpha["avg_latency_ms"] is not None


@pg
async def test_provider_health_marks_a_dead_chain_degraded(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    providers.set("alpha", mode="down")
    providers.set("beta", mode="down")
    status, _ = await send(client, SEARCH_KEY, prompt="what is sharding")
    assert status == 502, "premise: the whole chain failed"

    body = await get(client, "/admin/providers/health")
    # The row names the *last* provider attempted, so the failure is attributed there.
    beta = next(p for p in body["providers"] if p["provider"] == "beta")
    assert (beta["requests"], beta["errors"]) == (1, 1)
    assert beta["error_rate"] == 1.0
    assert beta["status"] == "degraded" and beta["healthy"] is False


@pg
async def test_a_survived_failover_shows_up_as_a_fallback_not_an_error(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """The documented under-attribution, made explicit rather than left to be found.

    Alpha is down and beta serves the request, so the row names beta and alpha's
    failure is invisible in `errors`. `fallbacks` is the signal that something in the
    chain is sick; per-attempt accuracy would need a row per attempt.
    See `prism/usage.py:ProviderHealth`.
    """
    providers.set("alpha", mode="down")
    status, _ = await send(client, SEARCH_KEY, prompt="what is sharding")
    assert status == 200, "premise: the fallback served it"

    body = await get(client, "/admin/providers/health")
    alpha = next(p for p in body["providers"] if p["provider"] == "alpha")
    beta = next(p for p in body["providers"] if p["provider"] == "beta")
    assert (alpha["requests"], alpha["errors"]) == (0, 0)
    assert alpha["status"] == "unknown"
    assert (beta["requests"], beta["errors"], beta["fallbacks"]) == (1, 0, 1)
    assert beta["status"] == "healthy"


@pg
async def test_provider_health_says_the_verdict_is_not_enforced(client: AsyncClient) -> None:
    """`enforced: false` is a claim about this build and it must not rot silently.

    The thresholds come from `gateway_config.json`'s `degradation` block, which
    nothing read until this endpoint existed; there is still no circuit breaker, so a
    provider reported degraded is tried first anyway. When a breaker lands, this
    assertion is the thing that has to change with it.
    """
    body = await get(client, "/admin/providers/health")
    assert body["enforced"] is False
    assert body["thresholds"] == {"error_rate": 0.5, "p95_latency_ms": 5000}
    assert body["window_seconds"] == 60


# --------------------------------------------------------------------------
# /admin/keys
# --------------------------------------------------------------------------


@pg
async def test_keys_reports_policy_without_any_key_material(client: AsyncClient) -> None:
    body = await get(client, "/admin/keys")
    assert {k["team"] for k in body["keys"]} == {
        "search", "research", "free-tier", "budget-demo"
    }
    search = next(k for k in body["keys"] if k["team"] == "search")
    assert search["key"] == "prism-sk-search"
    assert search["model_allowlist"] == ["fast"]
    assert search["requests_per_minute"] == 60
    assert search["cache"] == {"enabled": True, "similarity_threshold": 0.92}

    rendered = response_text(body).lower()
    for key in (SEARCH_KEY, RESEARCH_KEY, FREE_KEY):
        assert key.lower() not in rendered
    assert "key_hash" not in rendered and "hash" not in rendered


@pg
async def test_the_budget_counter_reconciles_against_the_log(client: AsyncClient) -> None:
    """`prism/db/models.py` calls `budget_periods` "a cache with a correctness proof".

    This is the proof, run through the API: the counter admission reads and the sum of
    the log rows are two independent paths to this month's spend, and `reconciles`
    says whether they agree. The evaluation guide grades exactly this.
    """
    charged = Decimal(0)
    for prompt in ("what is sharding", "what is raft"):
        _, cost = await send(client, SEARCH_KEY, prompt=prompt)
        charged += cost

    search = next(
        k for k in (await get(client, "/admin/keys", key="search"))["keys"]
    )
    assert Decimal(str(search["spent_usd"])) == charged
    assert Decimal(str(search["logged_cost_usd"])) == charged
    assert search["reconciles"] is True
    assert Decimal(str(search["remaining_usd"])) == Decimal("50") - charged


@pg
async def test_a_cache_hit_does_not_advance_the_reported_spend(client: AsyncClient) -> None:
    """The whole point of charging a hit zero, visible from the admin plane."""
    _, first = await send(client, SEARCH_KEY, prompt="what is sharding")
    await send(client, SEARCH_KEY, prompt="what is sharding")

    search = next(k for k in (await get(client, "/admin/keys", key="search"))["keys"])
    assert Decimal(str(search["spent_usd"])) == first
    stats = await get(client, "/admin/cache/stats", key="search")
    assert stats["hits"] == 1
