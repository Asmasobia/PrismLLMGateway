"""Schema-level guarantees.

The centrepiece is `test_concurrent_increments_are_all_counted`, which is the test
that justifies choosing Postgres. `docs/DATA_MODEL.md:88` requires budget
increments to be atomic — "two concurrent requests must both be counted" — and
that is a claim about the storage engine, not about application code. It is worth
proving on day 1, before anything is built on top of it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from prism.db.models import BudgetPeriod, CacheEntry, RequestLog, RequestStatus, Tenant
from prism.db.session import Database

pytestmark = pytest.mark.postgres

COST = Decimal("0.0000140000")
CONCURRENCY = 40


async def _period(database: Database, tenant_id: int) -> BudgetPeriod:
    async with database.session() as session:
        period = BudgetPeriod(
            tenant_id=tenant_id, period_start=BudgetPeriod.period_for(), spent_usd=Decimal("0")
        )
        session.add(period)
        await session.commit()
        return period


async def test_concurrent_increments_are_all_counted(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The atomic increment: `UPDATE ... SET spent = spent + ? RETURNING spent`.

    Postgres holds the row lock for the duration of the statement, so the read and
    the write are never two round trips and no update can be lost — even though
    forty coroutines on forty connections issue it simultaneously.
    """
    period = await _period(seeded, tenants["search"].id)

    async def charge() -> Decimal:
        async with seeded.session() as session:
            spent = (
                await session.execute(
                    update(BudgetPeriod)
                    .where(BudgetPeriod.id == period.id)
                    .values(
                        spent_usd=BudgetPeriod.spent_usd + COST,
                        request_count=BudgetPeriod.request_count + 1,
                    )
                    .returning(BudgetPeriod.spent_usd)
                )
            ).scalar_one()
            await session.commit()
            return spent

    await asyncio.gather(*(charge() for _ in range(CONCURRENCY)))

    async with seeded.session() as session:
        row = (
            await session.execute(select(BudgetPeriod).where(BudgetPeriod.id == period.id))
        ).scalar_one()
    assert row.spent_usd == COST * CONCURRENCY
    assert row.request_count == CONCURRENCY


async def test_read_then_write_loses_updates(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The bug the atomic increment avoids, made deterministic.

    Each task reads the balance, yields, then writes `read + cost`. Every task
    reads the same starting value, so all but one increment is discarded. This is
    what "cheap budget reads" turns into if you implement them as SELECT-then-UPDATE
    — and no amount of retrying makes it correct.
    """
    period = await _period(seeded, tenants["search"].id)

    async def racy_charge() -> None:
        async with seeded.session() as session:
            current = (
                await session.execute(
                    select(BudgetPeriod.spent_usd).where(BudgetPeriod.id == period.id)
                )
            ).scalar_one()
            await asyncio.sleep(0.05)  # forces the interleaving a real load also finds
            await session.execute(
                update(BudgetPeriod)
                .where(BudgetPeriod.id == period.id)
                .values(spent_usd=current + COST)
            )
            await session.commit()

    await asyncio.gather(*(racy_charge() for _ in range(10)))

    async with seeded.session() as session:
        spent = (
            await session.execute(
                select(BudgetPeriod.spent_usd).where(BudgetPeriod.id == period.id)
            )
        ).scalar_one()
    assert spent < COST * 10, "expected lost updates; the race did not reproduce"


async def test_a_tenant_can_have_only_one_period_row_per_month(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """Without this constraint, a concurrent first request for the month creates two
    counters and the budget silently doubles."""
    from sqlalchemy.exc import IntegrityError

    tenant_id = tenants["search"].id
    await _period(seeded, tenant_id)
    with pytest.raises(IntegrityError):
        await _period(seeded, tenant_id)


def test_period_start_is_the_first_of_the_utc_month() -> None:
    moment = dt.datetime(2026, 9, 13, 23, 30, tzinfo=dt.UTC)
    assert BudgetPeriod.period_for(moment) == dt.date(2026, 9, 1)


def test_period_start_uses_utc_not_local_time() -> None:
    """A moment that is still September in UTC but already October somewhere else
    must land in the September period, or the usage API stops being reproducible."""
    late = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.UTC)
    assert BudgetPeriod.period_for(late) == dt.date(2026, 9, 1)
    early_next = dt.datetime(2026, 10, 1, 1, 0, tzinfo=dt.UTC)
    assert BudgetPeriod.period_for(early_next) == dt.date(2026, 10, 1)


async def test_a_rejection_can_be_logged_without_a_tenant(database: Database) -> None:
    """docs/DATA_MODEL.md:57 — the log includes rejected requests, and a 401 has no
    identified tenant. The FK must therefore be nullable."""
    async with database.session() as session:
        session.add(
            RequestLog(
                request_id="req-401",
                tenant_id=None,
                team=None,
                key_prefix=None,
                requested_model="fast",
                status=RequestStatus.REJECTED_AUTH.value,
                http_status=401,
            )
        )
        await session.commit()

    async with database.session() as session:
        row = (
            await session.execute(select(RequestLog).where(RequestLog.request_id == "req-401"))
        ).scalar_one()
    assert row.cost_usd == Decimal("0")
    assert row.cache == "miss"
    assert row.fallback is False
    assert row.created_at.tzinfo is not None, "timestamps must be timezone-aware"


async def test_two_tenants_may_cache_the_same_prompt_independently(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The multi-tenancy boundary, at the schema level.

    Identical prompt, identical hash, two tenants: both rows must be storable, and
    the uniqueness constraint must not collapse them into one shared entry.
    """
    async with seeded.session() as session:
        for team in ("search", "free-tier"):
            session.add(
                CacheEntry(
                    tenant_id=tenants[team].id,
                    cache_key="fast",
                    prompt_hash="a" * 64,
                    prompt_text="What is a message queue?",
                    embedding=[0.1] * 8,
                    response_body={"choices": []},
                )
            )
        await session.commit()

    async with seeded.session() as session:
        rows = (
            await session.execute(
                select(CacheEntry).where(CacheEntry.prompt_hash == "a" * 64)
            )
        ).scalars().all()
    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {tenants["search"].id, tenants["free-tier"].id}


async def test_the_same_prompt_under_two_aliases_is_two_entries(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """A `fast` answer is not a valid `smart` answer, so the alias is part of the key."""
    async with seeded.session() as session:
        for alias in ("fast", "smart"):
            session.add(
                CacheEntry(
                    tenant_id=tenants["research"].id,
                    cache_key=alias,
                    prompt_hash="b" * 64,
                    prompt_text="Explain consensus.",
                    embedding=[0.2] * 8,
                    response_body={"choices": []},
                )
            )
        await session.commit()

    async with seeded.session() as session:
        count = len(
            (
                await session.execute(
                    select(CacheEntry).where(CacheEntry.prompt_hash == "b" * 64)
                )
            )
            .scalars()
            .all()
        )
    assert count == 2


async def test_deleting_a_tenant_removes_its_cache_but_keeps_its_logs(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """Two different retention decisions, both deliberate.

    Cache entries are worthless without their tenant, so they cascade. Log rows are
    the audit trail and the accounting record, so they survive with a null tenant
    rather than disappearing when a key is deleted.
    """
    tenant_id = tenants["search"].id
    async with seeded.session() as session:
        session.add(
            CacheEntry(
                tenant_id=tenant_id,
                cache_key="fast",
                prompt_hash="c" * 64,
                prompt_text="x",
                embedding=[0.0] * 8,
                response_body={},
            )
        )
        session.add(
            RequestLog(
                request_id="req-keep",
                tenant_id=tenant_id,
                team="search",
                key_prefix="prism-sk-search",
                requested_model="fast",
                status=RequestStatus.OK.value,
                http_status=200,
                cost_usd=COST,
            )
        )
        await session.commit()

        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one()
        await session.delete(tenant)
        await session.commit()

    async with seeded.session() as session:
        caches = (
            await session.execute(
                select(CacheEntry).where(CacheEntry.prompt_hash == "c" * 64)
            )
        ).scalars().all()
        log = (
            await session.execute(
                select(RequestLog).where(RequestLog.request_id == "req-keep")
            )
        ).scalar_one()
    assert caches == []
    assert log.tenant_id is None
    assert log.team == "search"  # denormalised copy keeps the row readable
    assert log.cost_usd == COST
