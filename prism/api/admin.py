"""The admin plane: usage, logs, cache statistics, provider health, keys.

Thin by design. Every route here resolves a window, resolves an optional key, calls
one function in `prism/usage.py`, and renders the result — the aggregation lives
there so the same queries can back a server-rendered ops console without going
through HTTP to reach them.

**The guard is on the router, not on the routes.** `dependencies=[Depends(require_admin)]`
means a new endpoint added to this file is authenticated by construction. The
per-route alternative (`admin: AdminDep` in each signature) is one forgotten
parameter away from publishing every tenant's spend to the internet, and that is not
a mistake worth leaving available.

**No route here writes a `request_log` row.** `prism/main.py:53` only opens an audit
context for `/v1/`, so operator traffic cannot inflate the counts this API reports —
an admin endpoint that appeared in its own output would make every total depend on
how often someone refreshed the console.

**Money is rendered two different ways, on purpose.** Aggregates
(`/admin/usage`, `/admin/keys`, `/admin/cache/stats`) are JSON *numbers*: they exist
to be summed, charted and compared, `docs/API_CONTRACT.md:134` shows a number, and
the totals are computed as exact `Decimal` in Postgres and converted once here at
the boundary. Per-request costs in `/admin/logs` are *strings*, produced by the same
`format_usd` that wrote the `x-prism-cost-usd` header the client saw — so an
operator chasing a reconciliation gap can diff the two byte for byte instead of
arguing about the eleventh decimal place. The argument in `prism/money.py` about
scientific notation is about headers read by shells and spreadsheets; a JSON number
is read by a parser, for which `2e-05` is unambiguous.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from prism import usage
from prism.auth import require_admin
from prism.db.models import RequestLog, Tenant
from prism.deps import ConfigDep, SessionDep
from prism.money import format_usd, quantize_usd

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

KeyQuery = Annotated[
    str | None,
    Query(
        description=(
            "Team name (recommended), key prefix, or the virtual key itself. "
            "Omit for all keys."
        )
    ),
]
FromQuery = Annotated[
    dt.date | None,
    Query(alias="from", description="First UTC day to include. Defaults to the 1st."),
]
ToQuery = Annotated[
    dt.date | None,
    Query(description="Last UTC day to include, inclusive. Defaults to today."),
]


def _usd(amount: Decimal) -> float:
    return float(quantize_usd(amount))


async def _selected(session, key: str | None) -> Tenant | None:
    """Resolve the optional `key=` parameter, or None for "every key"."""
    return await usage.find_tenant(session, key) if key else None


def _render_usage(row: usage.Usage) -> dict[str, object]:
    return {
        "key": row.key,
        "team": row.team,
        "requests": row.requests,
        # `requests` is every logged row; these three partition it. Reported because
        # a client counting its own successful calls gets `served`, not `requests`,
        # and the difference should be visible rather than look like a meter bug.
        "served": row.served,
        "rejected": row.rejected,
        "failed": row.failed,
        "cache_hits": row.cache_hits,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_tokens": row.total_tokens,
        "cost_usd": _usd(row.cost_usd),
    }


@router.get("/usage")
async def get_usage(
    session: SessionDep,
    key: KeyQuery = None,
    from_: FromQuery = None,
    to: ToQuery = None,
) -> dict[str, object]:
    """Spend and token totals over a window — `docs/API_CONTRACT.md:119`.

    The contract's field names are all present at the top level, so a client written
    against the example keeps working. Three things are added or changed, and each is
    a deliberate deviation rather than drift:

    * **`key` is a selector, not a secret** — see `prism/usage.py:find_tenant`. The
      value echoed back is always the non-secret prefix.
    * **`key` is optional.** Omitted, the top level totals every key and `by_key`
      breaks it down. That is the shape an ops console needs, and returning a
      differently-shaped body depending on whether a filter was passed would force
      every consumer to branch.
    * **`served` / `rejected` / `failed`** partition `requests`.

    `prompt_tokens`, `completion_tokens` and `cost_usd` are what the *providers* were
    asked for. A cache hit contributes zero to all three, so these totals reconcile
    against a provider invoice — but they are therefore *lower* than a client's own
    sum of the `usage` blocks it received, because a replayed cache hit carries the
    original response's token counts in its body. The difference is exactly the saved
    tokens reported by `/admin/cache/stats`. `scripts/load_test.py:123` prints the
    client-side figures for this comparison, so the discrepancy is one someone will
    hit; it is a property of caching, not a discrepancy in the meter.
    """
    window = usage.resolve_window(from_, to)
    tenant = await _selected(session, key)
    rows = await usage.usage_by_key(
        session, window=window, tenant_id=tenant.id if tenant else None
    )
    total = usage.combine(
        rows,
        key=tenant.key_prefix if tenant else None,
        team=tenant.team if tenant else None,
    )
    return {
        **_render_usage(total),
        "from": window.from_date.isoformat(),
        "to": window.to_date.isoformat(),
        "by_key": [_render_usage(row) for row in rows],
    }


def _render_log(row: RequestLog) -> dict[str, object]:
    """One `request_log` row, in the field order `docs/DATA_MODEL.md:64` lists them.

    **`key` replaces the document's `virtual_key`.** The raw key is not stored at all
    (`prism/db/models.py:Tenant` explains why), so there is nothing to render under
    that name; what is stored is the non-secret prefix, and calling the field
    `virtual_key` while returning a prefix would be worse than renaming it.

    `http_status` and `streamed` are extra columns this schema keeps beyond the
    document's list — the first because a log that records `ok` without recording
    what went out on the wire cannot answer "what did the client actually see", the
    second because a streamed request and a buffered one fail in different ways.
    """
    return {
        "request_id": row.request_id,
        "key": row.key_prefix,
        "team": row.team,
        "requested_model": row.requested_model,
        "resolved_provider": row.resolved_provider,
        "resolved_model": row.resolved_model,
        "status": row.status,
        "http_status": row.http_status,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "cost_usd": format_usd(row.cost_usd),
        "cache": row.cache,
        "fallback": row.fallback,
        "streamed": row.streamed,
        "route_reason": row.route_reason,
        "retries": row.retries,
        "latency_ms": row.latency_ms,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/logs")
async def get_logs(
    session: SessionDep,
    key: KeyQuery = None,
    limit: Annotated[int, Query(ge=1, le=usage.MAX_LOG_LIMIT)] = usage.DEFAULT_LOG_LIMIT,
) -> dict[str, object]:
    """Recent request log entries, newest first — `docs/API_CONTRACT.md:144`.

    Deliberately **not** windowed by `from`/`to`. "Recent" and "a date range" are
    different questions and a debugging tool should answer the first one without an
    argument; `/admin/usage` is where date arithmetic belongs. `limit` is capped
    server-side, because it arrives in a query string and an uncapped one turns one
    admin request into a full scan of the log.

    Prompts and responses are not here, and cannot be: `prism/audit.py` never records
    them. `docs/DATA_MODEL.md:78` asks that body logging be a documented decision —
    it was declined for the log, and the one place bodies *are* stored is the semantic
    cache, which is opt-in per tenant and covered in the README.
    """
    tenant = await _selected(session, key)
    rows = await usage.recent_logs(
        session, limit=limit, tenant_id=tenant.id if tenant else None
    )
    return {
        "key": tenant.key_prefix if tenant else None,
        "team": tenant.team if tenant else None,
        "limit": limit,
        "count": len(rows),
        "entries": [_render_log(row) for row in rows],
    }


def _render_cache(row: usage.CacheSavings) -> dict[str, object]:
    return {
        "key": row.key,
        "team": row.team,
        "cache_enabled": row.cache_enabled,
        "similarity_threshold": (
            float(row.similarity_threshold) if row.similarity_threshold is not None else None
        ),
        "lookups": row.lookups,
        "hits": row.hits,
        "misses": row.misses,
        "hit_rate": row.hit_rate,
        "entries": row.entries,
        "prompt_tokens_saved": row.prompt_tokens_saved,
        "completion_tokens_saved": row.completion_tokens_saved,
        "tokens_saved": row.tokens_saved,
        "cost_saved_usd": _usd(row.cost_saved_usd),
    }


@router.get("/cache/stats")
async def get_cache_stats(
    session: SessionDep,
    config: ConfigDep,
    key: KeyQuery = None,
    from_: FromQuery = None,
    to: ToQuery = None,
) -> dict[str, object]:
    """Hits, misses, hit rate, and what the cache saved — `docs/API_CONTRACT.md:153`.

    The contract asks for "hits, misses, and hit rate — overall or per key"; this
    answers both at once, for the same reason `/admin/usage` does.

    Two additions worth naming. **`tokens_saved` and `cost_saved_usd`** are the
    payoff the whole feature exists for, and they are recoverable precisely because a
    cache hit is charged zero: the tokens live on the entry, multiplied by its
    `hit_count`. **`cache_enabled` per key** is there because two of the four seeded
    tenants have caching off, and without that field their zero hit rate looks like a
    broken cache instead of a configuration.

    The two halves of this response have different time bases and
    `prism/usage.py:CacheSavings` explains why: hits and misses are windowed, entries
    and savings are lifetime. The response labels them rather than blending them into
    one number that means neither.
    """
    window = usage.resolve_window(from_, to)
    tenant = await _selected(session, key)
    rows = await usage.cache_savings(
        session, config, window=window, tenant_id=tenant.id if tenant else None
    )

    # Totalled by `usage.combine_cache_savings` rather than here, so the ops console
    # cannot compute a different headline hit rate from the same rows.
    total = usage.combine_cache_savings(rows)
    return {
        "key": tenant.key_prefix if tenant else None,
        "from": window.from_date.isoformat(),
        "to": window.to_date.isoformat(),
        "lookups": total.lookups,
        "hits": total.hits,
        "misses": total.misses,
        "hit_rate": total.hit_rate,
        # Lifetime, not windowed — see the docstring above.
        "entries": total.entries,
        "prompt_tokens_saved": total.prompt_tokens_saved,
        "completion_tokens_saved": total.completion_tokens_saved,
        "tokens_saved": total.tokens_saved,
        "cost_saved_usd": _usd(total.cost_saved_usd),
        "by_key": [_render_cache(row) for row in rows],
    }


@router.get("/providers/health")
async def get_provider_health(
    session: SessionDep,
    config: ConfigDep,
    window_seconds: Annotated[int | None, Query(ge=1, le=86_400)] = None,
) -> dict[str, object]:
    """Per-provider error rate, latency and verdict — `docs/API_CONTRACT.md:161`.

    `enforced: false` is the most important field in this response. The verdict is
    computed from the `degradation` thresholds in `gateway_config.json`, which until
    this endpoint existed were parsed and never read; nothing routes on it, because
    this build has no circuit breaker. Reporting a verdict while quietly implying it
    changes behaviour would be the dishonest version of this endpoint.

    Nothing here names a base URL or a credential — `docs/DATA_MODEL.md:44`. Provider
    *names* are already visible to any caller through `x-prism-provider`, so listing
    them behind the admin token discloses nothing new.
    """
    policy = config.degradation
    seconds = window_seconds if window_seconds is not None else policy.window_seconds
    rows = await usage.provider_health(session, config, window_seconds=seconds)
    return {
        "window_seconds": seconds,
        "enforced": False,
        "thresholds": {
            "error_rate": policy.error_rate_threshold,
            "p95_latency_ms": policy.p95_latency_ms,
        },
        "providers": [
            {
                "provider": row.provider,
                "status": row.status,
                "healthy": row.healthy,
                "requests": row.requests,
                "errors": row.errors,
                "error_rate": row.error_rate,
                "fallbacks": row.fallbacks,
                "retries": row.retries,
                "avg_latency_ms": row.avg_latency_ms,
                "p95_latency_ms": row.p95_latency_ms,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            }
            for row in rows
        ],
    }


@router.get("/keys")
async def get_keys(session: SessionDep, key: KeyQuery = None) -> dict[str, object]:
    """Every key's policy and its standing this month.

    Read-only. `docs/PRISM_PROBLEM_STATEMENT.md:186` lists `POST /admin/keys` as good
    to have and it is not built; this is the half that costs nothing and that the ops
    console and the live budget demo both need — a viewer has to be able to see that
    `budget-demo` really is at its ceiling before the 402 lands.

    No key material, hashed or otherwise: `key` is the prefix. `spent_usd` and
    `logged_cost_usd` are the two independent paths to this month's spend and
    `reconciles` says whether they agree — see `prism/usage.py:KeyState`.
    """
    tenant = await _selected(session, key)
    states = await usage.key_states(session, tenant_id=tenant.id if tenant else None)
    return {
        "keys": [
            {
                "key": state.key,
                "team": state.team,
                "status": state.status,
                "monthly_budget_usd": _usd(state.monthly_budget_usd),
                "spent_usd": _usd(state.spent_usd),
                "remaining_usd": _usd(state.remaining_usd),
                "logged_cost_usd": _usd(state.logged_cost_usd),
                "reconciles": state.reconciles,
                "requests_per_minute": state.requests_per_minute,
                "tokens_per_minute": state.tokens_per_minute,
                "model_allowlist": state.model_allowlist,
                "cache": {
                    "enabled": state.cache_enabled,
                    "similarity_threshold": (
                        float(state.similarity_threshold)
                        if state.similarity_threshold is not None
                        else None
                    ),
                },
            }
            for state in states
        ]
    }
