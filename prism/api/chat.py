"""`POST /v1/chat/completions` — the data plane.

The route reads as the sequence of decisions it is, in the order
`prism/auth.py:13` documents and this comment restates because this is where it is
actually enforced:

    authenticate ─▶ allowlist ─▶ rate limit ─▶ budget ─▶ resolve ─▶ cache ─▶ upstream
        401           403          429          402                   hit      502

Cheapest and most certain first, and nothing spends a tenant's quota to discover
that the request was never going to be served. Authentication happens in the
`TenantDep` dependency, so it is already done by the time this function is entered.

**The `x-prism-*` headers are built here, from the same context object that becomes
the log row.** `docs/IMPLEMENTATION_GUIDE.md` warns that retrofitting the header
contract "touches every code path", and the reason it does is that the headers and
the log row are the same four facts — which provider served it, whether the cache
answered, whether a fallback was used, what it cost. Deriving both from one object
is what stops the header saying `hit` while the log says `miss`, which is the exact
inconsistency `docs/API_CONTRACT.md:140` grades.

**The cache sits after the rate limit and the budget, not before.** A cache hit
therefore still consumes a request from the key's per-minute quota, and a tenant that
has exhausted its budget gets a 402 even for a question the cache could have answered
for free. Both are deliberate: the rate limit protects *this* gateway's capacity,
which a cache hit still occupies, and a budget is a spend ceiling the tenant asked
for — silently continuing to serve past it, on the grounds that these particular
answers are free, would make the ceiling mean something different depending on cache
contents. It also keeps the pipeline honest in the other direction: the cheap
rejections still run first, so nothing embeds a prompt it was never going to answer.

**Cached answers can be streamed, but streamed answers are not cached.** The
asymmetry is not laziness. Replaying a stored completion as SSE loses nothing — the
chunks are generated from the real body. Going the other way would mean assembling a
completion body out of deltas, and that body would be the gateway's reconstruction
rather than a provider's response: `system_fingerprint`, `logprobs` and any provider
extension present on the non-streaming shape are simply not in the delta stream, so a
later non-streaming caller would receive a subtly poorer object than the provider
would have sent. `prism/providers/base.py:UpstreamCompletion` makes exactly this
argument about never rebuilding a body. It is in the README's Known limitations.

**Streaming and non-streaming diverge as late as possible.** Everything up to and
including the chain resolution is shared, and both paths hand the same
`Dispatcher` a one-attempt callback (`complete` or `open_stream`), so retries,
backoff and failover are literally the same code for both — see
`prism/dispatch.py`. What genuinely differs is only *when the outcome is known*: a
non-streaming response can be metered before its first byte is sent, and a stream
cannot be metered until after its last one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from prism import audit, budget, cache, dispatch, routing
from prism.auth import TenantDep, enforce_allowlist
from prism.config import GatewayConfig
from prism.db.models import RequestStatus, Tenant
from prism.db.session import Database
from prism.deps import (
    ClassifierDep,
    ConfigDep,
    DatabaseDep,
    DispatcherDep,
    EmbedderDep,
    LimiterDep,
    ProviderDep,
    SessionDep,
    SettingsDep,
)
from prism.dispatch import Dispatcher
from prism.errors import RateLimitExceededError, UpstreamError
from prism.money import ZERO_USD, format_usd
from prism.providers.base import (
    DONE_SENTINEL,
    ProviderCallFailed,
    ProviderClient,
    ProviderStream,
    UpstreamCompletion,
)
from prism.schemas import ChatCompletionRequest

logger = logging.getLogger("prism.chat")

router = APIRouter(tags=["data-plane"])

CHAT_PATH = "/v1/chat/completions"

SSE_MEDIA_TYPE = "text/event-stream"


def prism_headers(
    context: audit.LogContext, *, with_cost: bool = True, provider: str | None = None
) -> dict[str, str]:
    """The `x-prism-*` contract from `docs/API_CONTRACT.md:69-76`.

    `provider` overrides the header — and only the header — for a cache hit. The two
    provided documents pull in opposite directions there and both are right about
    their own artifact: `docs/API_CONTRACT.md:73` requires the header to name "the
    upstream provider/model that served it", which for a replayed answer is the
    provider that produced it originally, while `docs/DATA_MODEL.md:60` requires the
    *row* to leave `resolved_provider` null on a cache hit. Honouring both is not a
    contradiction, it is the distinction between where the bytes came from and what
    this request did: no provider was called, so `/admin/usage` grouped by provider
    must not attribute this request to one, and a client reading the header still
    learns whose answer it is holding.

    `x-prism-fallback` is emitted as `false` rather than omitted when no fallback was
    used. The contract only requires it to be `true` when one was, but a header that
    is sometimes absent forces every client to distinguish "missing" from "false",
    and `docs/API_CONTRACT.md:88` requires it to be present on streaming responses
    regardless — so it is always present, everywhere.

    `with_cost=False` is the streaming case: headers are flushed before the first
    token, and the cost is not known until the last one
    (`docs/API_CONTRACT.md:88`). Sending `x-prism-cost-usd: 0` there would be a
    lie that reconciles to the wrong number; omitting it is what the contract asks
    for.
    """
    headers = {
        "x-prism-provider": provider
        or (context.resolved.label if context.resolved else "none"),
        "x-prism-cache": context.cache,
        "x-prism-fallback": "true" if context.fallback else "false",
    }
    if with_cost:
        headers["x-prism-cost-usd"] = format_usd(context.cost_usd)
    return headers


def sse(data: str) -> str:
    """One SSE event carrying `data`.

    The chunk is interpolated as text, never re-serialised from a parsed object: see
    `prism/providers/base.py:ProviderStream`. The blank line is the event terminator
    and is what makes a client's parser emit the event rather than keep buffering.
    """
    return f"data: {data}\n\n"


def record_failure(context: audit.LogContext, exc: dispatch.ChainExhausted) -> None:
    """Copy an exhausted chain's accounting onto the log context.

    `resolved` becomes the target of the *last* attempt, not the primary. The row
    then answers "where did this request end up" rather than "where did it start",
    which is the question worth asking about a failure that walked the whole chain.
    """
    if exc.target is not None:
        context.resolved = exc.target
    context.retries = exc.retries
    context.fallback = exc.fallback


@router.post(CHAT_PATH)
async def chat_completions(
    request: Request,
    payload: ChatCompletionRequest,
    tenant: TenantDep,
    config: ConfigDep,
    settings: SettingsDep,
    session: SessionDep,
    db: DatabaseDep,
    limiter: LimiterDep,
    providers: ProviderDep,
    dispatcher: DispatcherDep,
    classifier: ClassifierDep,
    embedder: EmbedderDep,
) -> Response:
    context = audit.current(request)
    if context is None:  # pragma: no cover - the middleware always creates one
        context = audit.begin(request, request.state.request_id)
    context.identify(tenant)
    context.requested_model = payload.model

    # 403 / 404. Before the rate limit, so a request for a model this key may not
    # use cannot burn the key's quota — see prism/auth.py:22.
    enforce_allowlist(tenant, payload.model, config)

    # 429.
    decision = limiter.try_acquire(tenant.id, tenant.requests_per_minute)
    if not decision.allowed:
        raise RateLimitExceededError(
            f"Rate limit of {decision.limit} requests per minute exceeded for this key. "
            f"Retry in {decision.retry_after:.1f}s."
        )

    # 402. Admission only asks whether any budget remains; see prism/budget.py.
    await budget.enforce_budget(session, tenant)

    # Awaited because `auto` may embed the prompt to classify it. Everything above
    # this line is a rejection check, so no request reaches the embedder — and pays
    # for it — until it is known to be one the gateway will actually serve.
    messages = payload.message_dicts()
    route = await routing.resolve(config, payload.model, messages, classifier)
    context.resolved = route.primary
    context.route_reason = route.reason

    # The cache comes *after* routing, not before, and that ordering is forced: an
    # `auto` request does not know which tier it belongs to until the classifier has
    # spoken, and the tier is part of the cache key. It costs nothing, because the
    # embedding the classifier just computed is the one the cache needs — the
    # request-scoped memo in `prism/deps.py:get_embedder` is what makes the second
    # lookup free rather than a second 4 ms of ONNX.
    scope = cache.scope(
        messages,
        served_as=route.tier or payload.model,
        params=payload.shaping_params(),
    )
    _, question = cache.split_conversation(messages)
    found = await cache.lookup(
        session, tenant, cache_key=scope, question=question, embedder=embedder
    )
    context.cache = found.state
    # Logged, not stored. The reason carries no prompt text (see `cache.Lookup`), but
    # `request_log` has no column for it and adding one would mean a second
    # explanation field alongside `route_reason` for every row, almost all of which
    # would read `no-match`.
    logger.info("cache %s [%s] request=%s", found.state, found.reason, context.request_id)

    if found.hit is not None:
        return await serve_cached(
            hit=found.hit,
            stream=payload.stream,
            route=route,
            context=context,
            config=config,
            session=session,
        )

    if payload.stream:
        return await start_stream(
            payload=payload,
            route=route,
            context=context,
            tenant=tenant,
            config=config,
            db=db,
            providers=providers,
            dispatcher=dispatcher,
        )

    try:
        outcome = await dispatcher.run(
            route.chain,
            lambda target: providers.complete(
                target, payload.upstream_payload(target.model)
            ),
        )
    except dispatch.ChainExhausted as exc:
        record_failure(context, exc)
        # The upstream's own message is deliberately not echoed: it may quote the
        # request or, on a misconfigured provider, the credential we sent
        # (docs/DATA_MODEL.md:44). `docs/API_CONTRACT.md:113` fixes the status.
        raise UpstreamError(
            "No upstream provider could serve this request."
        ) from exc

    completion = outcome.value
    context.resolved = completion.target
    context.retries = outcome.retries
    context.fallback = outcome.fallback
    context.prompt_tokens = completion.prompt_tokens
    context.completion_tokens = completion.completion_tokens
    context.cost_usd = config.price(completion.target.model).cost(
        completion.prompt_tokens, completion.completion_tokens
    )

    # One transaction for the charge and the log row, so spend and audit trail can
    # never disagree; see prism/budget.py:charge.
    await budget.charge(session, tenant, context.cost_usd)
    audit.stage(session, context, status=RequestStatus.OK.value, http_status=200)
    await session.commit()

    # After the commit, and in its own transaction, because a cache write is
    # best-effort and the accounting is not. Sharing the transaction would mean a
    # failed insert rolling back a charge that really happened.
    await remember(
        session,
        tenant,
        scope=scope,
        question=question,
        embedding=found.embedding,
        completion=completion,
        ttl_seconds=settings.cache_ttl_seconds,
    )

    return JSONResponse(
        status_code=200, content=completion.body, headers=prism_headers(context)
    )


async def remember(
    session: AsyncSession,
    tenant: Tenant,
    *,
    scope: str,
    question: str,
    embedding: list[float] | None,
    completion: UpstreamCompletion,
    ttl_seconds: int | None,
) -> None:
    """Write this answer into the cache, and never fail the request over it.

    Swallowing the exception is the same judgement `prism/audit.py:record_rejection`
    makes and for the same reason: this runs after the caller's answer is already
    settled, so an error here cannot be reported to them — it can only turn a served
    request into a 500 for a reason the caller has no stake in. A missing cache entry
    costs one upstream call later; a 500 costs the answer.

    `embedding is None` means the lookup never computed one, which happens exactly
    when the tenant has caching off or misconfigured. Re-embedding here to store an
    entry that tenant will never read would be work for nobody.
    """
    if embedding is None:
        return
    try:
        stored = await cache.store(
            session,
            tenant,
            cache_key=scope,
            question=question,
            embedding=embedding,
            body=completion.body,
            target=completion.target,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            ttl_seconds=ttl_seconds,
        )
        if stored:
            await cache.purge_expired(session, tenant.id)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("failed to write a cache entry for tenant %s", tenant.team)


async def serve_cached(
    *,
    hit: cache.CacheHit,
    stream: bool,
    route: routing.Route,
    context: audit.LogContext,
    config: GatewayConfig,
    session: AsyncSession,
) -> Response:
    """Answer from the cache: no upstream call, no tokens, no cost.

    **Zero cost and zero tokens, deliberately.** The tokens in the entry were paid
    for once, on the request that created it, and were charged then. Charging them
    again would make `/admin/usage` report more spend than the providers will ever
    invoice, which defeats the reconciliation `docs/EVALUATION_GUIDE.md` performs.
    What the cache saved is still recoverable — it is `hit_count × tokens` per entry,
    and that is what `/admin/cache/stats` reports.

    **The row is written before the first byte, even for a stream.** The live
    streaming path defers its row because the cost is unknown until the last chunk;
    here the entire body, its usage and its cost are already known, so there is
    nothing to wait for and deferring would only add a way to lose the row if the
    client disconnects.
    """
    origin = cache.served_target(config, hit, route.primary).label
    # Cleared, not kept: `docs/DATA_MODEL.md:60` wants `resolved_provider` null on a
    # cache hit, and `route.primary` is still sitting there from the resolution above.
    # Leaving it would put a provider in the audit trail that this request never
    # called. The header still names the origin — see `prism_headers`.
    context.resolved = None
    context.prompt_tokens = 0
    context.completion_tokens = 0
    context.cost_usd = ZERO_USD

    await cache.record_hit(session, hit.entry_id)
    audit.stage(session, context, status=RequestStatus.CACHE_HIT.value, http_status=200)
    await session.commit()

    logger.info(
        "served from cache: entry=%s kind=%s similarity=%.4f",
        hit.entry_id,
        hit.kind,
        hit.similarity,
    )

    if not stream:
        return JSONResponse(
            status_code=200,
            content=hit.body,
            headers=prism_headers(context, provider=origin),
        )

    context.streamed = True
    return StreamingResponse(
        replay(hit.body),
        media_type=SSE_MEDIA_TYPE,
        # `with_cost=False` even though the cost *is* known here, and is zero. The
        # contract omits the header on streaming responses; emitting it on cached
        # streams only would make its presence a side channel for whether the cache
        # answered, which `x-prism-cache` already says outright.
        headers={
            **prism_headers(context, with_cost=False, provider=origin),
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def replay(body: dict) -> AsyncIterator[str]:
    """A cached completion as an SSE stream, framed exactly like a live one.

    No database work and no `finally`: `serve_cached` has already committed
    everything this request will ever record, so a client that disconnects halfway
    through a replay leaves nothing behind to reconcile.
    """
    for chunk in cache.replay_chunks(body):
        yield sse(chunk)
    yield sse(DONE_SENTINEL)


async def start_stream(
    *,
    payload: ChatCompletionRequest,
    route: routing.Route,
    context: audit.LogContext,
    tenant: Tenant,
    config: GatewayConfig,
    db: Database,
    providers: ProviderClient,
    dispatcher: Dispatcher,
) -> StreamingResponse:
    """Open the upstream stream, then hand Starlette a generator over it.

    The stream is opened **here**, before the response object exists, and that
    ordering is the whole design of this function. `x-prism-provider` and
    `x-prism-fallback` are flushed with the headers, ahead of the first token, so
    they have to be facts by then — which they are only once an upstream has
    accepted the request. It also means a chain that fails to open at all produces a
    normal JSON 502 with a normal status line, instead of a 200 whose body is a
    single error event.
    """
    stream = await open_with_failover(
        payload=payload,
        route=route,
        context=context,
        providers=providers,
        dispatcher=dispatcher,
    )
    context.streamed = True
    # Tells the exception handlers that this request writes its own row later; see
    # prism/audit.py:LogContext.deferred.
    context.deferred = True

    return StreamingResponse(
        relay(
            stream=stream,
            context=context,
            tenant_id=tenant.id,
            config=config,
            db=db,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers={
            **prism_headers(context, with_cost=False),
            # SSE through a proxy is the classic place buffering appears: nginx will
            # happily accumulate the whole response and deliver it at once, which
            # passes every automated check and fails the only one a human runs
            # (docs/IMPLEMENTATION_GUIDE.md:96).
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def open_with_failover(
    *,
    payload: ChatCompletionRequest,
    route: routing.Route,
    context: audit.LogContext,
    providers: ProviderClient,
    dispatcher: Dispatcher,
) -> ProviderStream:
    """Resolve the chain down to one open stream, or raise a 502."""
    try:
        outcome = await dispatcher.run(
            route.chain,
            lambda target: providers.open_stream(
                target, payload.upstream_payload(target.model, stream=True)
            ),
        )
    except dispatch.ChainExhausted as exc:
        record_failure(context, exc)
        raise UpstreamError(
            "No upstream provider could serve this streaming request."
        ) from exc

    context.resolved = outcome.target
    context.retries = outcome.retries
    context.fallback = outcome.fallback
    return outcome.value


async def relay(
    *,
    stream: ProviderStream,
    context: audit.LogContext,
    tenant_id: int,
    config: GatewayConfig,
    db: Database,
) -> AsyncIterator[str]:
    """Forward the upstream's chunks, then meter what they cost.

    Three things here are deliberate.

    **No accumulation.** Each chunk is yielded as it arrives.
    `docs/API_CONTRACT.md:85` requires it, and the demo criterion at
    `docs/IMPLEMENTATION_GUIDE.md:96` is that a human sees tokens appear.

    **A mid-stream death terminates the stream with an error event.** That is the
    policy `docs/IMPLEMENTATION_GUIDE.md:172` asks to be decided and documented,
    and it is the acceptable behaviour it names; the unacceptable one — restarting
    on another provider and splicing the two outputs — is prevented one level down,
    where such a failure is raised with `retry_same=False, try_next=False`. The
    error event is followed by `[DONE]` anyway, because a client that never receives
    a terminator waits for its own read timeout before showing the user anything,
    turning a clear failure into a hang.

    **The row is written in this generator's own session, in a `finally`.** The
    request's session is gone by the time the body streams — FastAPI closes
    `yield` dependencies before the response is sent — so relying on it would work
    on one version and raise on the next. The `finally` also covers the client
    disconnecting halfway: those tokens were generated and paid for upstream, so
    they are charged and logged rather than lost.
    """
    status = RequestStatus.OK.value
    try:
        try:
            async for chunk in stream:
                yield sse(chunk)
        except ProviderCallFailed as exc:
            status = RequestStatus.UPSTREAM_ERROR.value
            logger.warning("stream failed mid-response: %s", exc)
            yield sse(json.dumps(UpstreamError(
                "The upstream provider stopped responding part-way through this "
                "stream. The response above is incomplete."
            ).body()))
        yield sse(DONE_SENTINEL)
    finally:
        # Never inside the `try` above: a `yield` after this point would be one more
        # chunk after the terminator, and during a client disconnect it would raise
        # "async generator ignored GeneratorExit".
        await stream.aclose()
        context.prompt_tokens = stream.prompt_tokens
        context.completion_tokens = stream.completion_tokens
        context.cost_usd = config.price(stream.target.model).cost(
            stream.prompt_tokens, stream.completion_tokens
        )
        await settle(db, context, tenant_id=tenant_id, status=status)


async def settle(
    db: Database, context: audit.LogContext, *, tenant_id: int, status: str
) -> None:
    """Charge the stream's cost and write its log row, in one transaction, never raising.

    Non-raising for the reason `prism/audit.py` gives: by the time this runs the
    response has already been delivered, so an exception here cannot be reported to
    the caller — it can only replace a served stream with a broken one in the
    server's own logs. `http_status` is recorded as 200 even for a failed stream,
    because 200 is what went out on the wire; the `status` column is where the
    outcome lives, and a row that disagreed with the wire would mislead exactly the
    person debugging it.
    """
    try:
        async with db.session() as session:
            # Re-read the tenant in *this* session rather than carrying the instance
            # over from the request's: that one is detached by now, and touching an
            # expired attribute on it would raise from inside a `finally`.
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None:
                await budget.charge(session, tenant, context.cost_usd)
            audit.stage(session, context, status=status, http_status=200)
            await session.commit()
    except Exception:
        logger.exception(
            "failed to settle streamed request %s (status=%s)", context.request_id, status
        )
