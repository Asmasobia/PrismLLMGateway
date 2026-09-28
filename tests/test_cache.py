"""The semantic cache: keying, guards, storage, and serving.

Three lanes, and the split is the same one `docs/DESIGN_NOTES.md` argues for.

**Pure functions, no server.** Keying, the literal and polarity guards, volatility
and SSE replay are ordinary text functions and are tested as such.

**Plumbing, against Postgres, with `FakeEmbedder`.** Scoping, tenant isolation,
thresholds, TTL, hit accounting and the HTTP path. The fake is lexical, so the pairs
below are *lexically* similar on purpose — the point of these tests is that the
gateway does the right thing with a similarity number, not that the number is
semantically right.

**Quality, marked `model`, against the vendored artifact.** The one lane that can
show a paraphrase hitting and a near-miss missing, and the one that re-derives the two
guard cosines `docs/DESIGN_NOTES.md` cites as the reason the guards exist at all.

Every guard test asserts its own premise: first that the pair really does clear the
threshold, then that the lookup still misses. Without the first assertion a guard test
passes trivially the day the similarity drifts below the threshold, and would then be
proving nothing while still being green.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from prism import budget, cache
from prism.config import GatewayConfig, Provider, ResolvedTarget
from prism.db.models import CacheEntry, RequestLog, RequestStatus, Tenant
from prism.db.session import Database
from prism.embeddings import FakeEmbedder, MemoEmbedder, cosine
from prism.providers.fake import FakeProviderClient
from tests.conftest import FREE_KEY, RESEARCH_KEY, SEARCH_KEY

CHAT = "/v1/chat/completions"

#: `data/sample_requests.jsonl`, verbatim. Duplicated here for the same reason the
#: keys are duplicated in `conftest.py`: if the provided pack changes, these tests
#: should fail rather than quietly follow along.
A1 = "How do I reset my password on the dashboard?"
A2 = "What are the steps to reset my dashboard password?"
A3 = "How do I reset my two-factor authentication on the dashboard?"
B1 = "What is the refund policy for annual plans?"
B2 = "If I bought an annual plan, can I get my money back?"
NO_CACHE = "What is the current status of the payments service?"

#: A pair that differs only in an amount, long enough to clear 0.92 under the lexical
#: `FakeEmbedder`. The guard tests assert that premise before asserting the rejection.
AMOUNT = "please convert the sum of {n} USD into indian rupees using the standard mid market rate"


# ==========================================================================
# Keying
# ==========================================================================


def test_normalise_ignores_whitespace_and_case() -> None:
    assert cache.digest("  Reset   my\npassword ") == cache.digest("reset my password")
    assert cache.digest("reset my password") != cache.digest("reset my passwords")


def test_split_conversation_returns_prefix_and_final_question() -> None:
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "what is a gateway?"},
        {"role": "assistant", "content": "a reverse proxy for models"},
        {"role": "user", "content": "and what about caching?"},
    ]
    prefix, question = cache.split_conversation(messages)
    assert question == "and what about caching?"
    assert [m["role"] for m in prefix] == ["system", "user", "assistant"]


def test_split_conversation_flattens_multimodal_content() -> None:
    """The same flattening the router uses, because it is literally the same function."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "https://example.test/x.png"}},
                {"type": "text", "text": "in one line"},
            ],
        }
    ]
    _, question = cache.split_conversation(messages)
    assert question == "describe this in one line"


def test_split_conversation_with_no_user_turn_yields_empty_question() -> None:
    prefix, question = cache.split_conversation([{"role": "system", "content": "hi"}])
    assert question == ""
    assert len(prefix) == 1


def test_scope_is_the_bare_tier_for_a_simple_request() -> None:
    """Readable in psql and in the console when there is nothing else to pin down."""
    messages = [{"role": "user", "content": A1}]
    assert cache.scope(messages, served_as="fast") == "fast"


def test_scope_separates_tiers() -> None:
    messages = [{"role": "user", "content": A1}]
    assert cache.scope(messages, served_as="fast") != cache.scope(messages, served_as="smart")


def test_scope_separates_conversations_with_the_same_final_question() -> None:
    """The reason a follow-up cannot leak across contexts."""
    tail = {"role": "user", "content": "and what about caching?"}
    one = [{"role": "user", "content": "explain gateways"}, tail]
    two = [{"role": "user", "content": "explain databases"}, tail]
    assert cache.scope(one, served_as="fast") != cache.scope(two, served_as="fast")


def test_scope_separates_sampling_parameters() -> None:
    messages = [{"role": "user", "content": A1}]
    bare = cache.scope(messages, served_as="fast")
    short = cache.scope(messages, served_as="fast", params={"max_tokens": 10})
    long = cache.scope(messages, served_as="fast", params={"max_tokens": 1000})
    assert len({bare, short, long}) == 3


def test_scope_is_stable_across_parameter_ordering() -> None:
    """A client that serialises its JSON differently must not get its own namespace."""
    messages = [{"role": "user", "content": A1}]
    first = cache.scope(messages, served_as="fast", params={"temperature": 0, "top_p": 1})
    second = cache.scope(messages, served_as="fast", params={"top_p": 1, "temperature": 0})
    assert first == second


def test_scope_fits_the_column() -> None:
    """`cache_key` is String(64); an overlong key would be a database error, not a miss."""
    messages = [{"role": "user", "content": "x"}] * 40
    long_alias = "a" * 120
    assert len(cache.scope(messages, served_as=long_alias, params={"seed": 7})) <= 64
    assert len(cache.scope([{"role": "user", "content": "x"}], served_as=long_alias)) <= 64


# ==========================================================================
# Guards, as pure functions
# ==========================================================================


def test_literals_extracts_numbers_ids_months_and_currency() -> None:
    found = dict(cache.literals("Refund INV-2024-001 for 250 USD raised in March"))
    assert "250" in found
    assert "usd" in found
    assert "march" in found
    assert "inv-2024-001" in found


def test_literals_counts_repeats() -> None:
    """A multiset, not a set: "add 2 and 3" and "add 2 and 2" have the same values."""
    assert cache.literals("add 2 and 3") != cache.literals("add 2 and 2")


def test_literals_includes_short_quoted_spans() -> None:
    """Deliberately unlike `routing._QUOTED`, which only drops *long* quoted spans."""
    a = cache.literals("what does 'connection reset' mean")
    b = cache.literals("what does 'connection refused' mean")
    assert a != b


def test_literal_match_rejects_a_changed_amount() -> None:
    query = cache.literals("refund 100 USD to the customer")
    assert not cache.literal_match(query, "refund 200 USD to the customer")
    assert cache.literal_match(query, "please refund 100 USD back to the customer")


@pytest.mark.parametrize(
    "text",
    ["I cannot log in", "it doesn't work", "there is no invoice", "it can't be undone"],
)
def test_negated_detects_cues_including_contractions(text: str) -> None:
    assert cache.negated(text)


def test_negated_is_false_for_a_plain_question() -> None:
    assert not cache.negated("How do I reset my password on the dashboard?")


def test_negated_is_a_boolean_not_a_count() -> None:
    """"cannot" and "can not" tokenize differently and must still compare equal."""
    assert cache.negated("I cannot log in") == cache.negated("I can not log in")


# ==========================================================================
# Volatility, on the write path
# ==========================================================================


def test_the_provided_no_cache_fixture_is_recognised_as_volatile() -> None:
    """`data/sample_requests.jsonl:req_no_cache`, which the pack asks us to decide about."""
    assert cache.is_volatile(NO_CACHE)


@pytest.mark.parametrize("text", [A1, A2, A3, B1, B2])
def test_the_cacheable_fixtures_are_not_volatile(text: str) -> None:
    """A false positive here silently disables the cache for a demoed prompt."""
    assert not cache.is_volatile(text)


def test_bare_now_is_not_treated_as_volatile() -> None:
    """Excluded on purpose: "how do I do X now?" is a perfectly cacheable question."""
    assert not cache.is_volatile("How do I rotate my API key now?")
    assert cache.is_volatile("What is failing right now?")


def test_is_cacheable_response_rejects_errors_and_empty_choices() -> None:
    assert cache.is_cacheable_response({"choices": [{"message": {"content": "hi"}}]})
    assert not cache.is_cacheable_response({"error": {"message": "nope"}, "choices": [1]})
    assert not cache.is_cacheable_response({"choices": []})
    assert not cache.is_cacheable_response("not a body")


# ==========================================================================
# Replay
# ==========================================================================


def test_replay_chunks_mirror_the_mock_providers_shape() -> None:
    """The wire shape of `scripts/mock_provider.py:185`, chunk for chunk."""
    body = {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 17,
        "model": "alpha-small",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "one two three"},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }
    chunks = [json.loads(c) for c in cache.replay_chunks(body)]

    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert all(c["id"] == "chatcmpl-abc" and c["model"] == "alpha-small" for c in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    # The words, reassembled: a client concatenating deltas gets the original text.
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text.strip() == "one two three"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == body["usage"]


def test_replay_chunks_survive_a_body_with_no_content() -> None:
    """A tool-call-only completion. Still a valid stream, just an empty one."""
    chunks = [json.loads(c) for c in cache.replay_chunks({"choices": [{"message": {}}]})]
    assert len(chunks) == 2
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_served_target_prefers_the_stored_origin(config: GatewayConfig) -> None:
    hit = cache.CacheHit(
        entry_id=1, body={}, similarity=1.0, kind="exact",
        served_provider="beta", served_model="beta-small",
        prompt_tokens=1, completion_tokens=1,
    )
    fallback = ResolvedTarget(config.provider("alpha"), "alpha-small")
    assert cache.served_target(config, hit, fallback).label == "beta/beta-small"


def test_served_target_falls_back_when_the_provider_is_gone(config: GatewayConfig) -> None:
    """An entry outlives a config change. It is still servable; the header just can't
    name a provider this gateway no longer has."""
    hit = cache.CacheHit(
        entry_id=1, body={}, similarity=1.0, kind="exact",
        served_provider="retired", served_model="retired-small",
        prompt_tokens=1, completion_tokens=1,
    )
    fallback = ResolvedTarget(config.provider("alpha"), "alpha-small")
    assert cache.served_target(config, hit, fallback) is fallback


# ==========================================================================
# Against Postgres
# ==========================================================================


pg = pytest.mark.postgres


async def tenant_named(session, team: str) -> Tenant:
    return (await session.execute(select(Tenant).where(Tenant.team == team))).scalar_one()


async def put(
    session,
    tenant: Tenant,
    text: str,
    *,
    embedder,
    scope: str = "fast",
    provider: str = "alpha",
    model: str = "alpha-small",
    ttl_seconds: int | None = None,
    now: dt.datetime | None = None,
    body: dict | None = None,
) -> bool:
    """Store one entry, embedding it the way the request path would."""
    written = await cache.store(
        session,
        tenant,
        cache_key=scope,
        question=text,
        embedding=await embedder.embed_one(text),
        body=body or {"choices": [{"message": {"role": "assistant", "content": f"re: {text}"}}]},
        target=ResolvedTarget(
            Provider(name=provider, base_url="http://upstream.test", api_key="test-key"), model
        ),
        prompt_tokens=7,
        completion_tokens=11,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    await session.commit()
    return written


@pg
async def test_an_identical_prompt_hits_without_embedding_anything(seeded: Database) -> None:
    """The exact fast path: an index lookup, no ONNX, no scan.

    The assertion that matters is `embedded == []`. A hit that quietly embedded the
    prompt would still be a hit, and the cheapest path in the gateway would have
    silently stopped being cheap.
    """
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=FakeEmbedder())

        memo = MemoEmbedder(FakeEmbedder())
        # Whitespace and case differ; the question does not.
        found = await cache.lookup(
            session, tenant, cache_key="fast",
            question="  how do I RESET my password on the dashboard?  ", embedder=memo,
        )
    assert found.hit is not None
    assert found.hit.kind == "exact"
    assert found.hit.similarity == 1.0
    assert memo.embedded == []


@pg
async def test_a_paraphrase_above_the_threshold_hits_semantically(seeded: Database) -> None:
    embedder = FakeEmbedder()
    stored = "how do I reset my password on the dashboard"
    asked = "how do I reset my password on the dashboard please"
    similarity = cosine(await embedder.embed_one(stored), await embedder.embed_one(asked))
    assert similarity > 0.92, "premise: this pair must clear the search tenant's threshold"

    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, stored, embedder=embedder)
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=asked, embedder=embedder
        )
    assert found.hit is not None
    assert found.hit.kind == "semantic"
    assert found.hit.similarity == pytest.approx(similarity)
    assert "threshold=0.9200" in found.reason


@pg
async def test_below_the_threshold_is_a_miss(seeded: Database) -> None:
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder)
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=A2, embedder=embedder
        )
    assert found.hit is None
    assert found.reason.startswith("no-match")
    # The vector comes back so the caller can store without embedding twice.
    assert found.embedding is not None


@pg
async def test_the_literal_guard_rejects_a_changed_amount(seeded: Database) -> None:
    embedder = FakeEmbedder()
    stored, asked = AMOUNT.format(n=100), AMOUNT.format(n=200)
    similarity = cosine(await embedder.embed_one(stored), await embedder.embed_one(asked))
    assert similarity > 0.92, "premise: similarity alone would serve this"

    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, stored, embedder=embedder)
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=asked, embedder=embedder
        )
    assert found.hit is None
    assert "rejected=literal:1" in found.reason


@pg
async def test_the_polarity_guard_rejects_a_negated_paraphrase(seeded: Database) -> None:
    embedder = FakeEmbedder()
    stored = "does the annual plan include priority support for every seat on the team account"
    asked = "does the annual plan not include priority support for every seat on the team account"
    similarity = cosine(await embedder.embed_one(stored), await embedder.embed_one(asked))
    assert similarity > 0.92, "premise: similarity alone would serve this"

    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, stored, embedder=embedder)
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=asked, embedder=embedder
        )
    assert found.hit is None
    assert "rejected=polarity:1" in found.reason


@pg
async def test_a_rejected_candidate_does_not_stop_the_scan(seeded: Database) -> None:
    """A guard rejection is not a verdict on the whole cache.

    An implementation that took the single nearest neighbour and then applied the
    guards would miss here, because the nearest entry is the one the literal guard
    throws out.
    """
    embedder = FakeEmbedder()
    decoy, asked = AMOUNT.format(n=200), AMOUNT.format(n=100)
    exact_but_reworded = (
        "please convert the sum of 100 USD into indian rupees using the standard market rate"
    )
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, exact_but_reworded, embedder=embedder)
        await put(session, tenant, decoy, embedder=embedder)  # newer, and nearer
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=asked, embedder=embedder
        )
    assert found.hit is not None
    assert "rejected=literal:1" in found.reason


@pg
async def test_one_tenants_entry_is_invisible_to_another(seeded: Database) -> None:
    """`docs/PRISM_PROBLEM_STATEMENT.md:66`: a cross-tenant hit is a data leak.

    Both tenants have caching enabled, both ask the identical question, and the
    second still misses — so the isolation is the key, not a coincidence of
    configuration.
    """
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        search = await tenant_named(session, "search")
        free = await tenant_named(session, "free-tier")
        assert free.cache_enabled

        await put(session, search, A1, embedder=embedder)
        found = await cache.lookup(
            session, free, cache_key="fast", question=A1, embedder=embedder
        )
    assert found.hit is None


@pg
async def test_a_different_scope_is_a_miss(seeded: Database) -> None:
    """A `fast` answer is not a valid `smart` answer even for the identical prompt."""
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder, scope="fast")
        found = await cache.lookup(
            session, tenant, cache_key="smart", question=A1, embedder=embedder
        )
    assert found.hit is None


@pg
async def test_a_tenant_with_caching_off_never_reads_or_writes(seeded: Database) -> None:
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "research")
        assert not tenant.cache_enabled

        assert await put(session, tenant, A1, embedder=embedder) is False
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=A1, embedder=embedder
        )
        rows = (await session.execute(select(func.count(CacheEntry.id)))).scalar_one()
    assert found.hit is None
    assert found.reason == "disabled"
    assert found.embedding is None  # nothing was embedded for a tenant that opted out
    assert rows == 0


@pg
async def test_an_enabled_tenant_with_no_threshold_misses_loudly(seeded: Database) -> None:
    """`prism/db/models.py:151` leaves the column nullable so this stays visible.

    The gateway declines to invent a threshold: the alternative is a number nobody
    chose guarding a data-leak boundary.
    """
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder)
        tenant.cache_similarity_threshold = None
        await session.commit()

        found = await cache.lookup(
            session, tenant, cache_key="fast", question=A1, embedder=embedder
        )
    assert found.hit is None
    assert found.reason == "no-threshold"


@pg
async def test_a_time_sensitive_prompt_is_served_but_never_stored(seeded: Database) -> None:
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        assert await put(session, tenant, NO_CACHE, embedder=embedder) is False
        rows = (await session.execute(select(func.count(CacheEntry.id)))).scalar_one()
    assert rows == 0


@pg
async def test_an_error_body_is_never_stored(seeded: Database) -> None:
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        written = await put(
            session, tenant, A1, embedder=embedder,
            body={"error": {"message": "upstream said no"}},
        )
        rows = (await session.execute(select(func.count(CacheEntry.id)))).scalar_one()
    assert written is False
    assert rows == 0


@pg
async def test_storing_the_same_prompt_twice_writes_one_row(seeded: Database) -> None:
    """`ON CONFLICT DO NOTHING` on `uq_cache_exact`: a retrying client is not an error."""
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder)
        await put(session, tenant, A1, embedder=embedder)
        rows = (await session.execute(select(func.count(CacheEntry.id)))).scalar_one()
    assert rows == 1


@pg
async def test_record_hit_counts_and_timestamps(seeded: Database) -> None:
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder)
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=A1, embedder=embedder
        )
        assert found.hit is not None
        await cache.record_hit(session, found.hit.entry_id)
        await cache.record_hit(session, found.hit.entry_id)
        await session.commit()

        entry = (await session.execute(select(CacheEntry))).scalar_one()
        assert entry.hit_count == 2
        assert entry.last_hit_at is not None


@pg
async def test_an_expired_entry_is_neither_served_nor_kept(seeded: Database) -> None:
    embedder = FakeEmbedder()
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        # Written two hours ago with a one-minute TTL: expired by now.
        await put(session, tenant, A1, embedder=embedder, ttl_seconds=60, now=past)

        found = await cache.lookup(
            session, tenant, cache_key="fast", question=A1, embedder=embedder
        )
        assert found.hit is None

        purged = await cache.purge_expired(session, tenant.id)
        await session.commit()
        rows = (await session.execute(select(func.count(CacheEntry.id)))).scalar_one()
    assert purged == 1
    assert rows == 0


@pg
async def test_an_entry_with_no_ttl_is_never_expired(seeded: Database) -> None:
    """`expires_at IS NULL` is the default, and must stay serveable."""
    embedder = FakeEmbedder()
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder, ttl_seconds=None)
        found = await cache.lookup(
            session, tenant, cache_key="fast", question=A1, embedder=embedder
        )
        purged = await cache.purge_expired(session, tenant.id)
    assert found.hit is not None
    assert purged == 0


# ==========================================================================
# Over HTTP, end to end
# ==========================================================================


@pg
async def test_a_repeated_request_is_served_from_the_cache(
    client: AsyncClient, providers: FakeProviderClient, seeded: Database
) -> None:
    """The whole feature, from the client's side.

    Four things have to agree: the header, the absence of a second upstream call, the
    `request_log` row, and the body. Asserting only the header would pass against a
    gateway that called upstream anyway and then lied about it.
    """
    body = {"model": "fast", "messages": [{"role": "user", "content": A1}]}
    first = await client.post(CHAT, json=body, headers={"Authorization": f"Bearer {SEARCH_KEY}"})
    assert first.status_code == 200
    assert first.headers["x-prism-cache"] == "miss"

    second = await client.post(CHAT, json=body, headers={"Authorization": f"Bearer {SEARCH_KEY}"})
    assert second.status_code == 200
    assert second.headers["x-prism-cache"] == "hit"
    # Free, and reported as free: docs/API_CONTRACT.md:70.
    assert Decimal(second.headers["x-prism-cost-usd"]) == Decimal("0")
    # The header names the provider that actually produced the bytes, on the first call.
    assert second.headers["x-prism-provider"] == first.headers["x-prism-provider"]
    # Byte-identical, including the completion id: this is the stored response, not a
    # second generation that happens to look the same.
    assert second.json() == first.json()
    # One upstream call for two requests.
    assert len(providers.calls) == 1

    async with seeded.session() as session:
        rows = (
            (await session.execute(select(RequestLog).order_by(RequestLog.created_at)))
            .scalars()
            .all()
        )
        entry = (await session.execute(select(CacheEntry))).scalar_one()
    assert [r.status for r in rows] == [RequestStatus.OK.value, RequestStatus.CACHE_HIT.value]
    assert [r.cache for r in rows] == ["miss", "hit"]
    # Nothing charged twice, and nothing counted twice.
    assert rows[1].cost_usd == Decimal("0")
    assert (rows[1].prompt_tokens, rows[1].completion_tokens) == (0, 0)
    assert entry.hit_count == 1
    # `docs/DATA_MODEL.md:60`: null on a cache hit, because no provider was called.
    # The header above still names the origin — the two are deliberately different,
    # see the `provider=` paragraph in `prism_headers`.
    assert (rows[1].resolved_provider, rows[1].resolved_model) == (None, None)
    assert rows[0].resolved_provider is not None


@pg
async def test_a_cache_hit_does_not_advance_the_budget(
    client: AsyncClient, seeded: Database
) -> None:
    """The reconciliation `docs/EVALUATION_GUIDE.md` performs is against provider spend.

    Charging a cached answer again would make `/admin/usage` report more than the
    providers will ever invoice.
    """
    body = {"model": "fast", "messages": [{"role": "user", "content": B1}]}
    headers = {"Authorization": f"Bearer {SEARCH_KEY}"}
    await client.post(CHAT, json=body, headers=headers)

    async with seeded.session() as session:
        after_first = await budget.spent_this_period(
            session, await tenant_named(session, "search")
        )

    await client.post(CHAT, json=body, headers=headers)

    async with seeded.session() as session:
        after_second = await budget.spent_this_period(
            session, await tenant_named(session, "search")
        )
    assert after_first > Decimal("0")
    assert after_second == after_first


@pg
async def test_a_cached_answer_can_be_streamed(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """A Good-To-Have that keeps the demo coherent: the cache still works for clients
    that stream, and a replay is indistinguishable from a live stream except for the
    header that is there to distinguish it."""
    headers = {"Authorization": f"Bearer {SEARCH_KEY}"}
    prompt = {"role": "user", "content": B2}
    first = await client.post(
        CHAT, json={"model": "fast", "messages": [prompt]}, headers=headers
    )
    content = first.json()["choices"][0]["message"]["content"]

    streamed = await client.post(
        CHAT, json={"model": "fast", "messages": [prompt], "stream": True}, headers=headers
    )
    assert streamed.status_code == 200
    assert streamed.headers["x-prism-cache"] == "hit"
    assert streamed.headers["content-type"].startswith("text/event-stream")
    # Streaming responses carry no cost header, cached or not.
    assert "x-prism-cost-usd" not in streamed.headers

    events = [
        line[len("data: ") :]
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    replayed = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert replayed.strip() == content.strip()
    assert len(providers.calls) == 1  # the stream came from the cache


@pg
async def test_a_streamed_answer_is_served_but_not_stored(
    client: AsyncClient, seeded: Database
) -> None:
    """The documented asymmetry, pinned so it cannot change silently.

    Rebuilding a completion body out of deltas would mean storing the gateway's
    reconstruction rather than a provider's response — see the module docstring in
    `prism/api/chat.py`. It is in the README's Known limitations.
    """
    headers = {"Authorization": f"Bearer {SEARCH_KEY}"}
    body = {"model": "fast", "messages": [{"role": "user", "content": A3}], "stream": True}
    response = await client.post(CHAT, json=body, headers=headers)
    assert response.status_code == 200

    async with seeded.session() as session:
        rows = (await session.execute(select(func.count(CacheEntry.id)))).scalar_one()
    assert rows == 0


@pg
async def test_a_tenant_with_caching_off_repeats_the_upstream_call(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    body = {"model": "fast", "messages": [{"role": "user", "content": A1}]}
    headers = {"Authorization": f"Bearer {RESEARCH_KEY}"}
    for _ in range(2):
        response = await client.post(CHAT, json=body, headers=headers)
        assert response.headers["x-prism-cache"] == "miss"
    assert len(providers.calls) == 2


@pg
async def test_differing_sampling_parameters_do_not_share_an_answer(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """`max_tokens: 10` is a different question, and gets its own namespace."""
    headers = {"Authorization": f"Bearer {SEARCH_KEY}"}
    messages = [{"role": "user", "content": A1}]
    await client.post(CHAT, json={"model": "fast", "messages": messages}, headers=headers)
    capped = await client.post(
        CHAT, json={"model": "fast", "messages": messages, "max_tokens": 10}, headers=headers
    )
    assert capped.headers["x-prism-cache"] == "miss"
    assert len(providers.calls) == 2


@pg
async def test_a_follow_up_does_not_hit_across_conversations(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """Identical final question, different history: no leak, and no false hit."""
    headers = {"Authorization": f"Bearer {SEARCH_KEY}"}
    tail = {"role": "user", "content": "and what should I do next?"}

    async def ask(first_turn: str):
        return await client.post(
            CHAT,
            json={
                "model": "fast",
                "messages": [
                    {"role": "user", "content": first_turn},
                    {"role": "assistant", "content": "here is some background"},
                    tail,
                ],
            },
            headers=headers,
        )

    assert (await ask("my password is expired")).headers["x-prism-cache"] == "miss"
    assert (await ask("my invoice is missing")).headers["x-prism-cache"] == "miss"
    # And the same conversation, asked twice, does hit.
    assert (await ask("my invoice is missing")).headers["x-prism-cache"] == "hit"
    assert len(providers.calls) == 2


@pg
async def test_a_cache_hit_still_consumes_rate_limit_quota(
    client: AsyncClient, providers: FakeProviderClient
) -> None:
    """Documented in `prism/api/chat.py`: the limiter protects capacity a hit still uses.

    The free-tier key allows ten requests a minute. The identical prompt is sent eleven
    times: one goes upstream, nine are cache hits, and the eleventh is refused. A
    gateway that let cache hits bypass the limiter would serve all eleven.
    """
    headers = {"Authorization": f"Bearer {FREE_KEY}"}
    body = {"model": "fast", "messages": [{"role": "user", "content": B1}]}
    statuses = [
        (await client.post(CHAT, json=body, headers=headers)).status_code for _ in range(11)
    ]
    assert statuses == [200] * 10 + [429]
    assert len(providers.calls) == 1


# ==========================================================================
# The real model: quality, and the evidence for the guards
# ==========================================================================


@pytest.mark.model
async def test_the_fixture_pairs_behave_as_designed_at_the_demoed_threshold(
    real_embedder,
) -> None:
    """The numbers in `docs/DESIGN_NOTES.md`, re-derived, and the behaviour they imply.

    At the `search` tenant's 0.92: pair A hits with room, the 2FA near-miss misses with
    room, and pair B is the documented WARN. This test is the reason those three
    sentences can be said out loud.
    """
    embedder = MemoEmbedder(real_embedder)

    async def sim(a: str, b: str) -> float:
        return cosine(await embedder.embed_one(a), await embedder.embed_one(b))

    assert await sim(A1, A2) == pytest.approx(0.9825, abs=0.002)
    assert await sim(B1, B2) == pytest.approx(0.8693, abs=0.002)
    assert await sim(A1, A3) == pytest.approx(0.8487, abs=0.002)
    assert await sim(A2, A3) == pytest.approx(0.8462, abs=0.002)
    assert await sim(A1, NO_CACHE) == pytest.approx(0.4847, abs=0.002)
    assert await sim(B1, NO_CACHE) == pytest.approx(0.6090, abs=0.002)

    # And the consequences at 0.92, which is what the demo stands on.
    assert await sim(A1, A2) > 0.92
    assert await sim(A1, A3) < 0.92
    assert await sim(A2, A3) < 0.92


@pytest.mark.model
async def test_similarity_alone_would_serve_the_wrong_answer(real_embedder) -> None:
    """The measured case for both guards, in one test.

    `docs/DESIGN_NOTES.md` cites a negation pair at 0.9620 and an amount pair at
    0.9149 as the reason cosine is not sufficient. Both are re-derived here, and then
    the guards are shown to reject them — so the guards are justified by a
    measurement in this repository rather than by a plausible story.
    """
    embedder = MemoEmbedder(real_embedder)

    negation = (
        "Can I cancel my annual plan?",
        "Can I not cancel my annual plan?",
    )
    amounts = (
        "What is the refund on a 100 USD charge?",
        "What is the refund on a 200 USD charge?",
    )

    negation_similarity = cosine(*await embedder.embed(list(negation)))
    amount_similarity = cosine(*await embedder.embed(list(amounts)))

    # Both are near-identical to the model, and one clears the demoed threshold.
    assert negation_similarity > 0.92
    assert amount_similarity > 0.85

    # And both are rejected, by different guards.
    assert cache.negated(negation[1]) != cache.negated(negation[0])
    assert not cache.literal_match(cache.literals(amounts[0]), amounts[1])


@pytest.mark.model
@pg
async def test_the_real_model_paraphrase_demo(seeded: Database, real_embedder) -> None:
    """A1 is stored, A2 is asked, A2 hits; A3 is asked, A3 misses.

    Everything in one test on purpose: the near-miss is only interesting *because* the
    paraphrase hit against the same stored entry at the same threshold.
    """
    embedder = MemoEmbedder(real_embedder)
    async with seeded.session() as session:
        tenant = await tenant_named(session, "search")
        await put(session, tenant, A1, embedder=embedder)

        paraphrase = await cache.lookup(
            session, tenant, cache_key="fast", question=A2, embedder=embedder
        )
        near_miss = await cache.lookup(
            session, tenant, cache_key="fast", question=A3, embedder=embedder
        )
    assert paraphrase.hit is not None
    assert paraphrase.hit.kind == "semantic"
    assert paraphrase.hit.similarity > 0.92
    assert near_miss.hit is None
