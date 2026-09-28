"""Read-only aggregations over `request_log`, `cache_entries` and the tenant table.

`docs/DATA_MODEL.md:14` calls the Usage Record "a query over `request_log`" rather
than a table, and the class docstring in `prism/db/models.py` gives the reason: a
usage row written alongside every log row is a dual write, and dual writes drift.
This module is that query. Nothing here writes, and nothing here caches — an admin
endpoint answering from a stale aggregate is the failure mode the design avoided.

**Everything is derived from what the gateway logged, not from what it hoped.** The
one number that is *not* derived here is `spent_usd`, which `prism/budget.py`
maintains as a denormalised counter for cheap admission; `/admin/keys` reports both
it and the log-derived total so the two can be compared, which is the
reconciliation `docs/EVALUATION_GUIDE.md` grades.

**Three deliberate boundaries.**

*Status is partitioned, not filtered.* `requests` counts every logged row including
rejections (`docs/DATA_MODEL.md:57` requires them to be logged, so leaving them out
of the count would make the API disagree with the log it reads). `served`,
`rejected` and `failed` partition that total, so an operator can see immediately why
a client-side count of successful calls is lower than `requests` instead of
suspecting the meter.

*Tokens are what was purchased.* A cache hit's row carries zero tokens and zero cost
on purpose (`prism/api/chat.py:serve_cached`), so these totals are what the
providers will invoice. The tokens a client *received* from cache replays are a
different quantity and live in `cache_savings`.

*Grouping is by the log row's own denormalised `team` / `key_prefix`, not by a join
to `tenants`.* Those columns exist so a row stays readable after a tenant is
deleted; grouping by them means a deleted tenant's traffic still appears, under the
name it had, instead of vanishing from the month it happened in.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from prism.config import DegradationPolicy, GatewayConfig
from prism.db.models import BudgetPeriod, CacheEntry, RequestLog, RequestStatus, Tenant, utcnow
from prism.errors import InvalidRequestError, NotFoundError
from prism.keys import hash_virtual_key
from prism.money import ZERO_USD, quantize_usd

#: The three-way partition of `RequestStatus`. Exhaustive by construction —
#: `tests/test_usage.py` asserts every member of the enum appears in exactly one
#: tuple, so adding a status without deciding which bucket it belongs to fails a
#: test rather than silently disappearing from `/admin/usage`.
SERVED_STATUSES: tuple[str, ...] = (
    RequestStatus.OK.value,
    RequestStatus.CACHE_HIT.value,
)
REJECTED_STATUSES: tuple[str, ...] = (
    RequestStatus.REJECTED_AUTH.value,
    RequestStatus.REJECTED_ALLOWLIST.value,
    RequestStatus.REJECTED_RATE_LIMIT.value,
    RequestStatus.REJECTED_BUDGET.value,
    RequestStatus.INVALID_REQUEST.value,
)
FAILED_STATUSES: tuple[str, ...] = (
    RequestStatus.UPSTREAM_ERROR.value,
    RequestStatus.INTERNAL_ERROR.value,
)

#: Newest-first log pages. A cap exists because `limit` comes from a query string
#: and an unbounded one turns a single admin request into a full table read.
DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 500


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Window:
    """A closed range of UTC calendar days.

    `to_date` is **inclusive**, which is not the obvious choice for a range but is
    the only one that makes the contract's own example correct: `from=2026-07-01&
    to=2026-07-31` is asking for the month of July, and an exclusive end would
    silently drop the 31st. The half-open `[start, end)` pair below is what actually
    reaches SQL, because comparing a timestamp against midnight-the-next-day is the
    only form that includes every instant of the last day without depending on the
    column's precision.
    """

    from_date: dt.date
    to_date: dt.date

    @property
    def start(self) -> dt.datetime:
        return dt.datetime.combine(self.from_date, dt.time.min, tzinfo=dt.UTC)

    @property
    def end(self) -> dt.datetime:
        following = self.to_date + dt.timedelta(days=1)
        return dt.datetime.combine(following, dt.time.min, tzinfo=dt.UTC)

    def contains(self, moment: dt.datetime) -> bool:
        return self.start <= moment.astimezone(dt.UTC) < self.end


def resolve_window(
    from_date: dt.date | None,
    to_date: dt.date | None,
    *,
    now: dt.datetime | None = None,
) -> Window:
    """Fill in the half of the window the caller left out.

    The default is the **current UTC budget period** — the first of the month
    through today — rather than "the last 30 days". That is deliberate: the number
    an operator almost always wants from `/admin/usage` is the one that can be
    compared against the tenant's monthly budget, and a rolling 30-day window
    produces a total that looks like spend against that budget but is not.
    `BudgetPeriod.period_for` is the same function budget admission uses, so the two
    cannot drift apart.
    """
    moment = (now or utcnow()).astimezone(dt.UTC)
    resolved_to = to_date if to_date is not None else moment.date()
    resolved_from = from_date if from_date is not None else BudgetPeriod.period_for(moment)
    if resolved_from > resolved_to:
        raise InvalidRequestError(
            f"'from' ({resolved_from.isoformat()}) is after 'to' "
            f"({resolved_to.isoformat()}).",
            param="from",
        )
    return Window(resolved_from, resolved_to)


def _in_window(window: Window) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (RequestLog.created_at >= window.start, RequestLog.created_at < window.end)


# --------------------------------------------------------------------------- #
# Selecting a key
# --------------------------------------------------------------------------- #


async def find_tenant(session: AsyncSession, selector: str) -> Tenant:
    """Resolve the `key=` query parameter to exactly one tenant.

    **The deviation from `docs/API_CONTRACT.md:119` is here, and it is on purpose.**
    The contract's example echoes a whole virtual key (`prism-sk-search-1a2b3c`) as
    the `key` field, which implies the operator puts a live credential in a query
    string — where it lands in the web server's access log, in shell history, in the
    browser's address bar and in the ops console's own URL, none of which are places
    a secret can be revoked from. So the selector accepts, in order:

    1. the team name (`search`) — unique, and the recommended form;
    2. the non-secret key prefix (`prism-sk-search`), which is what
       `prism/keys.py:key_prefix` stores for exactly this purpose;
    3. the full virtual key, resolved by hash so a client written literally against
       the contract still works.

    Only the prefix is ever echoed back. Accepting form 3 is compatibility, not
    endorsement, and the README says so.

    A selector matching more than one tenant is a 400 naming the ambiguity rather
    than a silently-picked first row: two keys sharing a prefix is unusual, and
    quietly reporting one team's spend under a query the operator believes covers
    both is worse than an error.
    """
    matches = (
        (
            await session.execute(
                select(Tenant)
                .where(
                    or_(
                        Tenant.team == selector,
                        Tenant.key_prefix == selector,
                        Tenant.key_hash == hash_virtual_key(selector),
                    )
                )
                .order_by(Tenant.team)
            )
        )
        .scalars()
        .all()
    )
    if not matches:
        raise NotFoundError(
            f"No key matches {selector!r}. Pass a team name, a key prefix "
            "(e.g. 'prism-sk-search'), or the virtual key itself.",
            code="key_not_found",
            param="key",
        )
    if len(matches) > 1:
        teams = ", ".join(sorted(t.team for t in matches))
        raise InvalidRequestError(
            f"{selector!r} matches {len(matches)} keys ({teams}). "
            "Use the team name, which is unique.",
            param="key",
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Usage:
    """Totals for one key — or, when `key` is None, for a whole selection."""

    key: str | None
    team: str | None
    requests: int
    served: int
    cache_hits: int
    rejected: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _counted(*statuses: str) -> ColumnElement[int]:
    """`count(*) FILTER (WHERE status IN (...))`.

    One grouped query with conditional aggregates rather than one query per bucket:
    the buckets have to be counted over exactly the same set of rows, and five
    separate queries against a table that is being written to concurrently can
    legitimately disagree with each other.
    """
    return func.count().filter(RequestLog.status.in_(statuses))


def _usage_columns() -> tuple[ColumnElement, ...]:
    return (
        func.count().label("requests"),
        _counted(*SERVED_STATUSES).label("served"),
        _counted(RequestStatus.CACHE_HIT.value).label("cache_hits"),
        _counted(*REJECTED_STATUSES).label("rejected"),
        _counted(*FAILED_STATUSES).label("failed"),
        func.coalesce(func.sum(RequestLog.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(RequestLog.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(RequestLog.cost_usd), 0).label("cost_usd"),
    )


async def usage_by_key(
    session: AsyncSession, *, window: Window, tenant_id: int | None = None
) -> list[Usage]:
    """One row per key with traffic in the window, alphabetically by team.

    Rows whose `team` is NULL are kept, not dropped. Those are the requests that
    never identified a tenant — 401s from an unknown or malformed key — and they are
    the single most useful thing in this API when someone reports that their
    integration "doesn't work". They group into one trailing bucket with a null key.
    """
    statement: Select = (
        select(RequestLog.key_prefix, RequestLog.team, *_usage_columns())
        .where(*_in_window(window))
        .group_by(RequestLog.key_prefix, RequestLog.team)
        # NULLs sort last in Postgres ASC, so the anonymous bucket lands at the end.
        .order_by(RequestLog.team)
    )
    if tenant_id is not None:
        statement = statement.where(RequestLog.tenant_id == tenant_id)
    rows = (await session.execute(statement)).all()
    return [
        Usage(
            key=row.key_prefix,
            team=row.team,
            requests=row.requests,
            served=row.served,
            cache_hits=row.cache_hits,
            rejected=row.rejected,
            failed=row.failed,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            cost_usd=quantize_usd(Decimal(row.cost_usd)),
        )
        for row in rows
    ]


def combine(
    rows: Sequence[Usage], *, key: str | None = None, team: str | None = None
) -> Usage:
    """Sum per-key rows into one total.

    Summed in Python from the rows already fetched rather than issued as a second
    `GROUP BY ()` query, so the total is arithmetically guaranteed to equal the
    breakdown shown next to it. A separate query would be a second read of a moving
    table, and a total that does not add up to its own rows is the kind of thing that
    destroys trust in a usage API.
    """
    return Usage(
        key=key,
        team=team,
        requests=sum(r.requests for r in rows),
        served=sum(r.served for r in rows),
        cache_hits=sum(r.cache_hits for r in rows),
        rejected=sum(r.rejected for r in rows),
        failed=sum(r.failed for r in rows),
        prompt_tokens=sum(r.prompt_tokens for r in rows),
        completion_tokens=sum(r.completion_tokens for r in rows),
        cost_usd=quantize_usd(sum((r.cost_usd for r in rows), start=ZERO_USD)),
    )


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #


async def recent_logs(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_LOG_LIMIT,
    tenant_id: int | None = None,
    window: Window | None = None,
) -> list[RequestLog]:
    """The newest `limit` log rows, optionally for one key.

    `request_id` breaks ties on `created_at`. Two rows written in the same
    microsecond would otherwise come back in whatever order the executor felt like,
    which makes a "newest 50" page non-deterministic and makes a test of it flake.
    """
    statement = (
        select(RequestLog)
        .order_by(RequestLog.created_at.desc(), RequestLog.request_id.desc())
        .limit(limit)
    )
    if tenant_id is not None:
        statement = statement.where(RequestLog.tenant_id == tenant_id)
    if window is not None:
        statement = statement.where(*_in_window(window))
    return list((await session.execute(statement)).scalars().all())


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CacheSavings:
    """What one tenant's cache has done, and what it holds.

    Two independent sources, deliberately reported side by side:

    * `hits` / `misses` come from `request_log` and are **windowed** — they describe
      traffic in the period asked about.
    * `entries` and the saved-token figures come from `cache_entries.hit_count` and
      are **lifetime** — an entry counts every hit it has ever served, including hits
      from before the window and hits whose log rows have been pruned.

    Deriving both from one source is not possible without either storing per-hit rows
    (a second write on the hot path, for a number nobody reads per-request) or
    recording the serving entry on the log row (a column that means nothing for the
    99% of rows that are not cache hits). Labelling which is which is the honest fix.
    """

    key: str
    team: str
    cache_enabled: bool
    similarity_threshold: Decimal | None
    lookups: int
    hits: int
    misses: int
    entries: int
    prompt_tokens_saved: int
    completion_tokens_saved: int
    cost_saved_usd: Decimal

    @property
    def hit_rate(self) -> float:
        """Hits over lookups, where a *lookup* is a request that reached the cache.

        The denominator is not "all requests". A request rejected at the rate limiter
        or the budget never consulted the cache at all (`prism/api/chat.py:22`
        explains why the cache sits behind both), so counting it as a miss would make
        a tenant's hit rate fall every time it got rate-limited — a number that moves
        for reasons unrelated to the thing it claims to measure.
        """
        return round(self.hits / self.lookups, 4) if self.lookups else 0.0

    @property
    def tokens_saved(self) -> int:
        return self.prompt_tokens_saved + self.completion_tokens_saved


async def _hit_counts(
    session: AsyncSession, *, window: Window, tenant_id: int | None
) -> dict[int, tuple[int, int]]:
    """`{tenant_id: (hits, misses)}` over requests that actually reached the cache."""
    statement = (
        select(
            RequestLog.tenant_id,
            func.count().filter(RequestLog.cache == "hit").label("hits"),
            func.count().filter(RequestLog.cache == "miss").label("misses"),
        )
        .where(
            *_in_window(window),
            RequestLog.tenant_id.is_not(None),
            RequestLog.status.in_(SERVED_STATUSES),
        )
        .group_by(RequestLog.tenant_id)
    )
    if tenant_id is not None:
        statement = statement.where(RequestLog.tenant_id == tenant_id)
    return {row.tenant_id: (row.hits, row.misses) for row in (await session.execute(statement))}


async def _entry_savings(
    session: AsyncSession, config: GatewayConfig, *, tenant_id: int | None
) -> dict[int, tuple[int, int, int, Decimal]]:
    """`{tenant_id: (entries, prompt_tokens, completion_tokens, cost)}` saved so far.

    Grouped by `(tenant_id, served_model)` in SQL and priced in Python, because the
    price of a model lives in `prism/config.py` and not in the database — see the
    module docstring in `prism/db/models.py` for why prices are not a table. That
    also means the saving is valued at *today's* price list rather than the price at
    the time of the hit, which is the same simplification every "you saved $X"
    counter makes and is stated here rather than implied.
    """
    statement = (
        select(
            CacheEntry.tenant_id,
            CacheEntry.served_model,
            func.count().label("entries"),
            func.coalesce(
                func.sum(CacheEntry.hit_count * CacheEntry.prompt_tokens), 0
            ).label("prompt_tokens"),
            func.coalesce(
                func.sum(CacheEntry.hit_count * CacheEntry.completion_tokens), 0
            ).label("completion_tokens"),
        )
        .group_by(CacheEntry.tenant_id, CacheEntry.served_model)
    )
    if tenant_id is not None:
        statement = statement.where(CacheEntry.tenant_id == tenant_id)

    totals: dict[int, tuple[int, int, int, Decimal]] = {}
    for row in await session.execute(statement):
        entries, prompt_tokens, completion_tokens, cost = totals.get(
            row.tenant_id, (0, 0, 0, ZERO_USD)
        )
        saved = ZERO_USD
        if row.served_model is not None:
            try:
                saved = config.price(row.served_model).cost(
                    row.prompt_tokens, row.completion_tokens
                )
            except NotFoundError:
                # A model that has since been dropped from the price list. The tokens
                # are still real and still reported; only their dollar value is
                # unknowable, and inventing one would corrupt the total.
                saved = ZERO_USD
        totals[row.tenant_id] = (
            entries + row.entries,
            prompt_tokens + row.prompt_tokens,
            completion_tokens + row.completion_tokens,
            cost + saved,
        )
    return totals


async def cache_savings(
    session: AsyncSession,
    config: GatewayConfig,
    *,
    window: Window,
    tenant_id: int | None = None,
) -> list[CacheSavings]:
    """Per-key cache statistics, including keys that have never had a hit.

    Driven from the tenant table rather than from the log, so a tenant with caching
    switched off appears with zeroes and its `cache_enabled: false` visible. The
    alternative — listing only keys with cache traffic — hides the single most common
    explanation for "why is my hit rate zero".
    """
    tenants_query = select(Tenant).order_by(Tenant.team)
    if tenant_id is not None:
        tenants_query = tenants_query.where(Tenant.id == tenant_id)
    tenants = (await session.execute(tenants_query)).scalars().all()

    counts = await _hit_counts(session, window=window, tenant_id=tenant_id)
    savings = await _entry_savings(session, config, tenant_id=tenant_id)

    result: list[CacheSavings] = []
    for tenant in tenants:
        hits, misses = counts.get(tenant.id, (0, 0))
        entries, prompt_saved, completion_saved, cost = savings.get(
            tenant.id, (0, 0, 0, ZERO_USD)
        )
        result.append(
            CacheSavings(
                key=tenant.key_prefix,
                team=tenant.team,
                cache_enabled=tenant.cache_enabled,
                similarity_threshold=tenant.cache_similarity_threshold,
                lookups=hits + misses,
                hits=hits,
                misses=misses,
                entries=entries,
                prompt_tokens_saved=prompt_saved,
                completion_tokens_saved=completion_saved,
                cost_saved_usd=quantize_usd(cost),
            )
        )
    return result


def combine_cache_savings(rows: Sequence[CacheSavings]) -> CacheSavings:
    """Sum per-key cache rows into one total, for the same reason `combine` exists.

    Two callers need this figure — `/admin/cache/stats` and the ops console — and the
    headline cache hit rate is the number most likely to be quoted out loud. Summing
    it in each caller is how the API and the console end up disagreeing about it.

    `cache_enabled` on the total answers "is the cache on anywhere", which is the only
    question the field can answer once keys are mixed; `similarity_threshold` is None
    because tenants have different ones and averaging thresholds would invent a value
    no lookup ever used.
    """
    return CacheSavings(
        key=None,
        team=None,
        cache_enabled=any(row.cache_enabled for row in rows),
        similarity_threshold=None,
        lookups=sum(row.lookups for row in rows),
        hits=sum(row.hits for row in rows),
        misses=sum(row.misses for row in rows),
        entries=sum(row.entries for row in rows),
        prompt_tokens_saved=sum(row.prompt_tokens_saved for row in rows),
        completion_tokens_saved=sum(row.completion_tokens_saved for row in rows),
        cost_saved_usd=quantize_usd(sum((row.cost_saved_usd for row in rows), start=ZERO_USD)),
    )


# --------------------------------------------------------------------------- #
# Provider health
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderHealth:
    """What recent traffic says about one provider.

    **Observed, not enforced.** `docs/API_CONTRACT.md:161` asks for "whether the
    gateway currently considers it healthy", and the honest answer for this build is
    that the gateway forms an opinion and then does not act on it: there is no
    circuit breaker, so a provider reported unhealthy here is still tried first by
    `prism/dispatch.py` and still costs each request its retry budget before failover.
    The thresholds are the ones `gateway_config.json` already supplied under
    `degradation` and which nothing had read until now, so this endpoint at least
    makes the configured policy visible — and the gap between visible and enforced is
    in the README's Known limitations rather than papered over.

    **Errors are under-attributed, and it is structural.** A provider that failed and
    was successfully failed over from leaves a row naming the provider that
    *succeeded* (`prism/api/chat.py:record_failure` only overwrites the target when
    the whole chain is exhausted), so its failure appears in `fallbacks` and
    `retries`, not in `errors`. Per-attempt accuracy would need a row per attempt;
    until then `fallbacks` rising while `errors` stays flat is the signal that
    something upstream in a chain is sick.
    """

    provider: str
    requests: int
    errors: int
    fallbacks: int
    retries: int
    avg_latency_ms: int | None
    p95_latency_ms: int | None
    last_seen: dt.datetime | None
    policy: DegradationPolicy

    @property
    def error_rate(self) -> float:
        return round(self.errors / self.requests, 4) if self.requests else 0.0

    @property
    def healthy(self) -> bool | None:
        """`None` means "no traffic to judge from", which is not the same as healthy.

        Collapsing the two would report a provider nobody has called since the last
        restart as green, which is precisely the provider most likely to be broken.
        """
        if not self.requests:
            return None
        too_many_errors = self.error_rate > self.policy.error_rate_threshold
        too_slow = (
            self.p95_latency_ms is not None
            and self.p95_latency_ms > self.policy.p95_latency_ms
        )
        return not (too_many_errors or too_slow)

    @property
    def status(self) -> str:
        return {True: "healthy", False: "degraded", None: "unknown"}[self.healthy]


async def provider_health(
    session: AsyncSession,
    config: GatewayConfig,
    *,
    window_seconds: int | None = None,
    now: dt.datetime | None = None,
) -> list[ProviderHealth]:
    """Per-provider health over the last `window_seconds` of traffic.

    The provider list comes from the configuration, not from the log, so a provider
    that has never been called still appears — with `status: unknown`. Deriving the
    list from observed traffic would make a completely dead provider silently absent
    from the health endpoint.

    Latency is the gateway's own end-to-end measurement (`request_log.latency_ms`)
    and therefore includes queueing, retries and backoff, not just the upstream's
    generation time. It is the right number for "is this chain slow for a caller",
    and the wrong one for "is this provider slow" on a request that retried twice
    before succeeding; that is why `retries` is reported next to it.
    """
    policy = config.degradation
    seconds = window_seconds if window_seconds is not None else policy.window_seconds
    since = (now or utcnow()).astimezone(dt.UTC) - dt.timedelta(seconds=seconds)

    statement = (
        select(
            RequestLog.resolved_provider.label("provider"),
            func.count().label("requests"),
            _counted(RequestStatus.UPSTREAM_ERROR.value).label("errors"),
            func.count().filter(RequestLog.fallback.is_(True)).label("fallbacks"),
            func.coalesce(func.sum(RequestLog.retries), 0).label("retries"),
            func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
            func.percentile_cont(0.95)
            .within_group(RequestLog.latency_ms)
            .label("p95_latency_ms"),
            func.max(RequestLog.created_at).label("last_seen"),
        )
        .where(
            RequestLog.created_at >= since,
            RequestLog.resolved_provider.is_not(None),
        )
        .group_by(RequestLog.resolved_provider)
    )
    observed = {row.provider: row for row in (await session.execute(statement))}

    health: list[ProviderHealth] = []
    for name in sorted(config.provider_names):
        row = observed.get(name)
        health.append(
            ProviderHealth(
                provider=name,
                requests=row.requests if row else 0,
                errors=row.errors if row else 0,
                fallbacks=row.fallbacks if row else 0,
                retries=row.retries if row else 0,
                # `is not None`, not a truthiness test: an average of 0 ms is a real
                # measurement (an in-process fake upstream produces them), and
                # reporting it as "no data" would hide traffic that did happen.
                avg_latency_ms=(
                    round(row.avg_latency_ms) if row and row.avg_latency_ms is not None else None
                ),
                p95_latency_ms=(
                    round(row.p95_latency_ms) if row and row.p95_latency_ms is not None else None
                ),
                last_seen=row.last_seen if row else None,
                policy=policy,
            )
        )
    return health


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KeyState:
    """A tenant's policy and its standing this month, with no key material.

    `spent_usd` is the `budget_periods` counter that admission reads;
    `logged_cost_usd` is the same month summed straight from `request_log`. They are
    two paths to one number, and `prism/db/models.py` calls the counter "a cache with
    a correctness proof" — showing both is how an operator checks that claim without
    writing SQL, and is the reconciliation `docs/EVALUATION_GUIDE.md` asks for.
    """

    key: str
    team: str
    status: str
    monthly_budget_usd: Decimal
    spent_usd: Decimal
    logged_cost_usd: Decimal
    requests_per_minute: int
    tokens_per_minute: int | None
    model_allowlist: list[str]
    cache_enabled: bool
    similarity_threshold: Decimal | None

    @property
    def remaining_usd(self) -> Decimal:
        """Never negative. Budget admission can overshoot by a concurrent burst's
        own cost (`prism/budget.py:28`), and rendering that as a negative balance
        invites the reading that the tenant owes money rather than that its ceiling
        has been reached."""
        return max(self.monthly_budget_usd - self.spent_usd, ZERO_USD)

    @property
    def reconciles(self) -> bool:
        return self.spent_usd == self.logged_cost_usd


async def key_states(
    session: AsyncSession, *, tenant_id: int | None = None, now: dt.datetime | None = None
) -> list[KeyState]:
    """Every key, its policy, and its spend this month."""
    period = BudgetPeriod.period_for(now)
    window = Window(period, (now or utcnow()).astimezone(dt.UTC).date())

    tenants_query = select(Tenant).order_by(Tenant.team)
    if tenant_id is not None:
        tenants_query = tenants_query.where(Tenant.id == tenant_id)
    tenants = (await session.execute(tenants_query)).scalars().all()

    spent = {
        row.tenant_id: row.spent_usd
        for row in await session.execute(
            select(BudgetPeriod.tenant_id, BudgetPeriod.spent_usd).where(
                BudgetPeriod.period_start == period
            )
        )
    }
    logged = {
        row.tenant_id: row.cost_usd
        for row in await session.execute(
            select(
                RequestLog.tenant_id,
                func.coalesce(func.sum(RequestLog.cost_usd), 0).label("cost_usd"),
            )
            .where(*_in_window(window), RequestLog.tenant_id.is_not(None))
            .group_by(RequestLog.tenant_id)
        )
    }

    return [
        KeyState(
            key=tenant.key_prefix,
            team=tenant.team,
            status=tenant.status,
            monthly_budget_usd=quantize_usd(tenant.monthly_budget_usd),
            spent_usd=quantize_usd(spent.get(tenant.id, ZERO_USD)),
            logged_cost_usd=quantize_usd(Decimal(logged.get(tenant.id, ZERO_USD))),
            requests_per_minute=tenant.requests_per_minute,
            tokens_per_minute=tenant.tokens_per_minute,
            model_allowlist=list(tenant.model_allowlist),
            cache_enabled=tenant.cache_enabled,
            similarity_threshold=tenant.cache_similarity_threshold,
        )
        for tenant in tenants
    ]


__all__ = [
    "DEFAULT_LOG_LIMIT",
    "FAILED_STATUSES",
    "MAX_LOG_LIMIT",
    "REJECTED_STATUSES",
    "SERVED_STATUSES",
    "CacheSavings",
    "KeyState",
    "ProviderHealth",
    "Usage",
    "Window",
    "cache_savings",
    "combine",
    "combine_cache_savings",
    "find_tenant",
    "key_states",
    "provider_health",
    "recent_logs",
    "resolve_window",
    "usage_by_key",
]
