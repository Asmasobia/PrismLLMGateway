"""The request journal: one `request_log` row per data-plane request, always.

`docs/DATA_MODEL.md:64` requires a row for rejected requests too — "a rejection
that leaves no trace is a rejection you cannot explain to the team whose traffic it
was". That single sentence is what makes this module necessary, because a rejection
does not reach the route:

    401  raised inside `require_tenant`, a dependency — the route never runs
    403  raised inside the route, before any upstream call
    429  raised inside the route
    402  raised inside the route
    422  raised by FastAPI's own body validation — the route never runs
    502  raised after the chain is exhausted

Only three of those six are in a position to write their own row. So the facts are
accumulated on `request.state` as they become known, and **one** function turns
them into a row — called from the route on success, and from the exception handlers
otherwise. The alternative, a `try/except` per failure mode in the route, cannot
see the two cases that never enter the route at all.

**Two sessions, on purpose.** On the success path the row is written in the
*request's* session and committed together with the budget charge, so spend and log
can never disagree. On a rejection the request's session is already closed by the
time an exception handler runs — FastAPI tears down `yield` dependencies before
handlers — so the handler opens its own short-lived session. Nothing is charged on
that path, so there is nothing to keep in one transaction with it.

**A failure to log never changes the response.** `record_rejection` swallows its
own errors into the server log. Turning a clean 402 into a 500 because the audit
insert failed would replace a correct answer with an incorrect one, and would do it
precisely when the database is already unhealthy.

Prompt and response bodies are deliberately **not** stored. `docs/DATA_MODEL.md:80`
makes that a documented design decision either way: storing them would put every
tenant's user data in the gateway's own database, which then needs a retention
policy, an access boundary and a deletion path. The fields kept here are enough to
debug traffic — who, what model, what it cost, how long, why it was refused.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from prism.config import ResolvedTarget
from prism.db.models import RequestLog, RequestStatus
from prism.db.session import Database
from prism.money import ZERO_USD, quantize_usd

logger = logging.getLogger("prism.audit")

#: `PrismError.error_type` to the `status` vocabulary of `docs/DATA_MODEL.md:70`.
#: One table, so a new error type cannot quietly log as something else — anything
#: absent here falls back to `invalid_request`, which is visible in the console
#: rather than silently mislabelled as a success.
STATUS_FOR_ERROR_TYPE: dict[str, str] = {
    "authentication_error": RequestStatus.REJECTED_AUTH.value,
    "model_not_allowed": RequestStatus.REJECTED_ALLOWLIST.value,
    "rate_limit_exceeded": RequestStatus.REJECTED_RATE_LIMIT.value,
    "budget_exceeded": RequestStatus.REJECTED_BUDGET.value,
    "upstream_error": RequestStatus.UPSTREAM_ERROR.value,
    "not_found_error": RequestStatus.INVALID_REQUEST.value,
    "invalid_request_error": RequestStatus.INVALID_REQUEST.value,
}

#: Where the log context is parked. A private-ish name on `request.state` rather
#: than a `ContextVar`, because the lifetime we want is exactly the request's and
#: `request.state` already has it — a ContextVar would need explicit resetting and
#: leaks across tasks in a streaming response.
STATE_ATTR = "prism_log"


@dataclass
class LogContext:
    """Everything known about one in-flight request, so far.

    Mutable and filled in progressively. Each field is set at the moment it becomes
    a fact, which is why a 403 still logs the requested model but not the resolved
    one: at rejection time there was no resolved model, and inventing one would put
    a provider in the audit trail that was never called.
    """

    request_id: str
    started: float = field(default_factory=time.monotonic)

    tenant_id: int | None = None
    team: str | None = None
    key_prefix: str | None = None

    requested_model: str | None = None
    resolved: ResolvedTarget | None = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = ZERO_USD

    cache: str = "miss"
    fallback: bool = False
    streamed: bool = False
    route_reason: str | None = None
    retries: int = 0

    #: Set once a row has been written, so the success path and an exception
    #: handler can never both write one. The primary key would catch a double
    #: write as an IntegrityError, but catching it here means the *first* row
    #: survives rather than the request failing on the way out.
    written: bool = False

    #: A streaming response writes its own row when the stream ends, long after
    #: the route has returned. This tells the handlers to leave it alone.
    deferred: bool = False

    def identify(self, tenant) -> None:
        """Copy the tenant's identifying fields. Never the key itself, hashed or not."""
        self.tenant_id = tenant.id
        self.team = tenant.team
        self.key_prefix = tenant.key_prefix

    def latency_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)

    def to_row(self, *, status: str, http_status: int) -> RequestLog:
        return RequestLog(
            request_id=self.request_id,
            tenant_id=self.tenant_id,
            team=self.team,
            key_prefix=self.key_prefix,
            requested_model=self.requested_model,
            resolved_provider=self.resolved.provider.name if self.resolved else None,
            resolved_model=self.resolved.model if self.resolved else None,
            status=status,
            http_status=http_status,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=quantize_usd(self.cost_usd),
            cache=self.cache,
            fallback=self.fallback,
            streamed=self.streamed,
            route_reason=self.route_reason,
            retries=self.retries,
            latency_ms=self.latency_ms(),
        )


def begin(request: Request, request_id: str) -> LogContext:
    """Start a context for a data-plane request and park it on `request.state`."""
    context = LogContext(request_id=request_id)
    setattr(request.state, STATE_ATTR, context)
    return context


def current(request: Request) -> LogContext | None:
    """The context for this request, or None if it is not a data-plane request.

    Returning None is how the admin plane and the health probes stay out of the
    request log: they never get a context, so the shared exception handlers have
    nothing to write.
    """
    return getattr(request.state, STATE_ATTR, None)


def stage(session: AsyncSession, context: LogContext, *, status: str, http_status: int) -> None:
    """Add the row to `session` without committing. The caller owns the transaction."""
    if context.written:
        return
    session.add(context.to_row(status=status, http_status=http_status))
    context.written = True


async def record_rejection(
    db: Database, context: LogContext, *, status: str, http_status: int
) -> None:
    """Write a rejection's row in its own transaction, never raising.

    Used from the exception handlers, where the request's session is gone and where
    an exception would replace a correct error response with a 500.
    """
    if context.written or context.deferred:
        return
    try:
        async with db.session() as session:
            stage(session, context, status=status, http_status=http_status)
            await session.commit()
    except Exception:
        context.written = False
        logger.exception(
            "failed to write request_log row for %s (status=%s)",
            context.request_id,
            status,
        )


def status_for(error_type: str) -> str:
    """Map an error type to a log status, defaulting visibly rather than silently."""
    return STATUS_FOR_ERROR_TYPE.get(error_type, RequestStatus.INVALID_REQUEST.value)


__all__ = [
    "STATUS_FOR_ERROR_TYPE",
    "LogContext",
    "begin",
    "current",
    "record_rejection",
    "stage",
    "status_for",
]
