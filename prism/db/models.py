"""The schema.

Four tables for the seven entities in `docs/DATA_MODEL.md`, and the collapsing is
deliberate:

* **Provider, Model Alias and Model Price are not tables.** They are deployment
  configuration, not tenant data — they have no per-request mutable state and no
  foreign keys pointing at them. Putting them in Postgres would mean a migration
  to change a price and a join on the hot path to read one. They live in
  `prism.config`, loaded and validated at startup.
* **Usage Record is not a table either.** It is a query over `request_log`.
  Writing an aggregate row alongside every log row is a dual write, and dual
  writes drift: the moment one succeeds and the other doesn't, the usage API and
  the log disagree and there is no way to say which is right.
  `docs/EVALUATION_GUIDE.md` grades exactly that reconciliation, so `request_log`
  is the single source of truth and usage is derived from it.

`budget_periods` is the one deliberate exception to "derive it". `docs/DATA_MODEL.md:86`
requires budget reads to be cheap — scanning the log on every admission is not —
so it is a denormalised counter, incremented atomically, and reconcilable against
`request_log` at any time. It is a cache with a correctness proof, not a second
source of truth.
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Money. 18 digits total, 10 after the point.
#
# The scale is not arbitrary: the budget-demo tenant has a monthly budget of
# $0.00001 (1e-5) and one `fast` request costs on the order of 1e-5, so anything
# coarser than ~1e-8 would round that tenant's spend to zero and the
# budget_exceeded demo would never fire. 10 decimal places leaves three orders of
# magnitude of headroom below the smallest number the system has to represent.
MONEY = Numeric(18, 10)

# 384 is the output dimension of BAAI/bge-small-en-v1.5, measured and recorded in
# docs/DESIGN_NOTES.md. Stored as a plain float array rather than a pgvector column:
# pgvector is an extension, and depending on one narrows the schema to deployments
# that have it installed, for a corpus that is per-tenant and small. Similarity is
# computed in Python over a per-tenant scan instead — the cost of that choice, and
# the point at which it stops being affordable, are in the README's Known limitations.
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now.

    `datetime.utcnow()` returns a *naive* datetime that claims to be local time,
    which compares wrongly against anything tz-aware and silently shifts by the
    machine's offset when serialised. Every timestamp in this schema is
    `DateTime(timezone=True)` and every Python-side default comes from here.
    """
    return dt.datetime.now(dt.UTC)


class RequestStatus(enum.StrEnum):
    """The `status` vocabulary from `docs/DATA_MODEL.md:63`.

    Stored as a string rather than a Postgres ENUM type: adding a value to a
    native enum needs `ALTER TYPE`, and the failure-mode vocabulary is exactly the
    thing most likely to grow as the gateway learns new ways to fail.
    """

    OK = "ok"
    CACHE_HIT = "cache_hit"
    REJECTED_AUTH = "rejected_auth"
    REJECTED_ALLOWLIST = "rejected_allowlist"
    REJECTED_RATE_LIMIT = "rejected_rate_limit"
    REJECTED_BUDGET = "rejected_budget"
    UPSTREAM_ERROR = "upstream_error"
    INVALID_REQUEST = "invalid_request"
    # Not in the document's list, which ends in "...". A request that failed for a
    # reason the gateway did not anticipate still has to appear in the log, and
    # filing it under one of the labels above would misattribute a gateway bug to a
    # tenant's traffic.
    INTERNAL_ERROR = "internal_error"


class TenantStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Tenant(Base):
    """A virtual key and its policy.

    The raw key is **not stored.** `key_hash` holds SHA-256 of the key and lookup
    hashes the presented key to find the row, so a database dump does not hand
    over working credentials — the same reason `docs/DATA_MODEL.md:44` gives for
    provider keys.

    SHA-256 rather than bcrypt/argon2 on purpose. Slow hashes exist to defend
    *low-entropy human-chosen* secrets against offline guessing. A virtual key is
    high-entropy and machine-generated, so there is no dictionary to run; what a
    slow hash would buy instead is ~100 ms added to every single request on the
    hot path. Fast hash, high-entropy secret.

    `key_prefix` keeps the human-readable head of the key (`prism-sk-search`) so
    an admin can tell tenants apart in the console without the gateway being able
    to reproduce the secret.
    """

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TenantStatus.ACTIVE.value
    )
    monthly_budget_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    requests_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    # Good-to-have in the data model; carried because seed_keys.json supplies it.
    tokens_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # JSONB, not a join table: the allowlist is read on every request and never
    # queried *across* tenants, so there is nothing a relational shape would buy.
    model_allowlist: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    cache_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Nullable by design: the data model only requires a threshold when the cache
    # is enabled, and a default here would hide a misconfigured tenant.
    cache_similarity_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    budget_periods: Mapped[list[BudgetPeriod]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE.value

    def allows(self, requested_model: str) -> bool:
        return requested_model in self.model_allowlist

    def __repr__(self) -> str:
        # No key material, hashed or otherwise.
        return f"Tenant(team={self.team!r}, status={self.status!r})"


class BudgetPeriod(Base):
    """Accumulated spend for one tenant in one calendar month (UTC).

    Exists so budget admission is a single indexed row read instead of an
    aggregate over the whole log (`docs/DATA_MODEL.md:86`). The increment is
    `UPDATE ... SET spent_usd = spent_usd + :cost RETURNING spent_usd`, which is
    atomic in Postgres because the row lock is held for the duration of the
    statement — the read and the write are never two round trips, so two
    concurrent requests cannot both read the same starting value.
    """

    __tablename__ = "budget_periods"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_start", name="uq_budget_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    spent_usd: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="0", default=Decimal("0")
    )
    request_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    tenant: Mapped[Tenant] = relationship(back_populates="budget_periods")

    @staticmethod
    def period_for(moment: dt.datetime | None = None) -> dt.date:
        """First day of the UTC month containing `moment`.

        UTC, not local time: a gateway whose budget month rolls over at the
        server's midnight gives different answers in different deployments and
        makes the usage API non-reproducible.
        """
        moment = moment or utcnow()
        return moment.astimezone(dt.UTC).date().replace(day=1)


class RequestLog(Base):
    """One row per data-plane request, **including rejected ones**.

    `docs/DATA_MODEL.md:57` is explicit about that: a rejection that leaves no
    trace is a rejection you cannot explain to the team whose traffic it was. It
    also means rows exist with no tenant at all (a 401 has no identified tenant),
    hence the nullable FK.
    """

    __tablename__ = "request_log"
    __table_args__ = (
        # The two access patterns: "this tenant's recent traffic" (admin logs and
        # usage window) and "reconcile a month". Both are covered by one
        # composite index; created_at descending matches the natural read order.
        Index("ix_request_log_tenant_time", "tenant_id", "created_at"),
        Index("ix_request_log_time", "created_at"),
    )

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    # Denormalised copies so a log row stays readable after a tenant is deleted,
    # and so nothing has to join to render the admin log. Never the raw key.
    team: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_prefix: Mapped[str | None] = mapped_column(String(32), nullable=True)

    requested_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))

    cache: Mapped[str] = mapped_column(String(8), nullable=False, default="miss")
    fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    streamed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    route_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class CacheEntry(Base):
    """A semantic cache entry, scoped to one tenant.

    `tenant_id` is part of the identity of an entry, not metadata on it
    (`docs/DATA_MODEL.md:104`). Every lookup filters on it, so one team can never
    be served another team's response — the isolation boundary is in the schema,
    not in a `WHERE` clause someone might forget.

    `prompt_hash` gives an exact-match fast path: an identical prompt is answered
    by an index lookup with no embedding and no vector scan at all. The semantic
    path only runs when the literal path misses.
    """

    __tablename__ = "cache_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cache_key", "prompt_hash", name="uq_cache_exact"),
        Index("ix_cache_tenant_key", "tenant_id", "cache_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    # The alias or model the entry was created under. Part of the key because a
    # `fast` answer is not a valid `smart` answer.
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)

    # DOUBLE_PRECISION, not Numeric: money needs exactness, an embedding does
    # not, and the cosines in docs/DESIGN_NOTES.md were measured in float64.
    # Numeric here would also mean 384 Decimal conversions per row per lookup.
    embedding: Mapped[list[float]] = mapped_column(
        ARRAY(DOUBLE_PRECISION), nullable=False
    )

    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    served_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    served_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    last_hit_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
