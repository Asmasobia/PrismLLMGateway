"""Monthly budgets: the atomic increment, the derived reset, the documented overshoot.

The one property here that is hard to get right and easy to *appear* to get right is
atomicity. `SELECT spent` / add in Python / `UPDATE ... SET spent = :new` passes every
sequential test in this file and loses charges under concurrency — silently, as totals
that come out low, which reads like a pricing bug rather than a race.

`test_concurrent_charges_are_never_lost` is the test that separates them, and it is
also the reason this project uses Postgres rather than SQLite: SQLite serialises
writers, so the broken shape would pass there too (`CLAUDE.md`, Environment).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from prism.budget import charge, enforce_budget, spent_this_period
from prism.db.models import BudgetPeriod, Tenant
from prism.db.session import Database
from prism.errors import BudgetExceededError

#: Module-wide, even though the last two tests are pure date arithmetic: keeping the
#: budget month's definition next to the code that resets by deriving it is worth more
#: than having those two run in the no-database lane.
pytestmark = pytest.mark.postgres

CENT = Decimal("0.01")


async def commit_charge(db: Database, tenant: Tenant, amount: Decimal) -> None:
    """One charge in its own session and transaction, as a request would do it."""
    async with db.session() as session:
        await charge(session, tenant, amount)
        await session.commit()


async def period_row(db: Database, tenant: Tenant, period: dt.date) -> BudgetPeriod:
    async with db.session() as session:
        return (
            await session.execute(
                select(BudgetPeriod).where(
                    BudgetPeriod.tenant_id == tenant.id,
                    BudgetPeriod.period_start == period,
                )
            )
        ).scalar_one()


# -- accumulation -----------------------------------------------------------


async def test_the_first_charge_of_a_month_creates_its_own_row(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """No period row is pre-created, by anything, ever.

    Pre-creating rows would need a job that runs at every month boundary for every
    tenant — one more thing to fail silently. `ON CONFLICT` makes the first charge
    of the month create the row, so a tenant that never spends never has one.
    """
    tenant = tenants["search"]
    async with seeded.session() as session:
        assert await spent_this_period(session, tenant) == 0

    await commit_charge(seeded, tenant, CENT)

    row = await period_row(seeded, tenant, BudgetPeriod.period_for())
    assert row.spent_usd == CENT
    assert row.request_count == 1


async def test_charges_accumulate_and_count(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    tenant = tenants["search"]
    for _ in range(3):
        await commit_charge(seeded, tenant, CENT)

    row = await period_row(seeded, tenant, BudgetPeriod.period_for())
    assert row.spent_usd == Decimal("0.03")
    assert row.request_count == 3


async def test_tenants_are_billed_separately(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    await commit_charge(seeded, tenants["search"], CENT)
    async with seeded.session() as session:
        assert await spent_this_period(session, tenants["research"]) == 0


async def test_a_charge_is_quantized_before_it_is_stored(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The value charged must be the value the header reported.

    Passing an unrounded Decimal to the column would let Postgres round it — with its
    own rule, at its own moment — and the stored total would drift from the sum of the
    headers by fractions the reconciliation check does not forgive. See prism/money.py.
    """
    async with seeded.session() as session:
        total = await charge(session, tenants["search"], Decimal("0.000000000149"))
        await session.commit()
    assert total == Decimal("0.0000000001")


# -- atomicity --------------------------------------------------------------


async def test_concurrent_charges_are_never_lost(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """Twenty concurrent charges, twenty separate transactions, one exact total.

    Each `charge` is a single `INSERT ... ON CONFLICT DO UPDATE`, so Postgres holds
    the row lock for the whole read-modify-write and the additions serialise. Replace
    it with a select-then-update and this fails by a random amount every run.

    Twenty is deliberate: the pool holds twenty connections (prism/db/session.py:33),
    so all twenty are genuinely in flight rather than queued two at a time.
    """
    tenant = tenants["search"]
    await asyncio.gather(*(commit_charge(seeded, tenant, CENT) for _ in range(20)))

    row = await period_row(seeded, tenant, BudgetPeriod.period_for())
    assert row.spent_usd == Decimal("0.20")
    assert row.request_count == 20


async def test_the_first_charge_of_a_month_does_not_race_itself(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The insert race, isolated: concurrent charges with *no* row to conflict on.

    "Select, and insert if missing" survives the accumulate case and dies here — two
    requests both find nothing, both insert, and the loser gets a unique-constraint
    violation instead of a completion. That failure is worst at the start of a month,
    which is exactly when nobody is watching for it.
    """
    tenant = tenants["research"]
    await asyncio.gather(*(commit_charge(seeded, tenant, CENT) for _ in range(8)))
    assert (await period_row(seeded, tenant, BudgetPeriod.period_for())).spent_usd == Decimal(
        "0.08"
    )


# -- admission --------------------------------------------------------------


async def test_admission_passes_while_any_budget_remains(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    tenant = tenants["search"]
    async with seeded.session() as session:
        assert await enforce_budget(session, tenant) == tenant.monthly_budget_usd


async def test_an_exactly_spent_budget_is_refused(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """`spent >= budget`, not `>`.

    With `>`, every key gets one free request past its budget forever — which for a
    key doing one expensive request per month means the budget never binds at all.
    """
    tenant = tenants["search"]
    await commit_charge(seeded, tenant, tenant.monthly_budget_usd)
    async with seeded.session() as session:
        with pytest.raises(BudgetExceededError):
            await enforce_budget(session, tenant)


async def test_the_rejection_message_names_no_provider_and_no_key(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    tenant = tenants["search"]
    await commit_charge(seeded, tenant, tenant.monthly_budget_usd)
    async with seeded.session() as session:
        with pytest.raises(BudgetExceededError) as caught:
            await enforce_budget(session, tenant)
    message = str(caught.value)
    assert "mock-key" not in message
    assert tenant.key_hash not in message
    # It does say when relief comes, because the caller's next question is "for how
    # long?" and a budget rejection is the one 4xx that answers it with a date.
    assert "next month" in message


async def test_overshoot_is_bounded_by_one_burst_not_unbounded(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The documented trade-off, pinned so nobody discovers it in production.

    Admission asks "is there any budget left", not "is there enough" — the cost is
    unknown until the provider reports usage, and for a stream until it ends. So a
    burst can all pass on the last cent and overshoot by the sum of its own costs.

    What must remain true is that the overshoot is *one burst*, not unbounded: once
    the charges land, admission refuses. A key cannot keep overspending by retrying.
    """
    tenant = tenants["search"]
    await commit_charge(seeded, tenant, tenant.monthly_budget_usd - Decimal("0.000001"))

    async def admit() -> Decimal:
        # A session each, because that is what five concurrent requests have. Sharing
        # one AsyncSession across gathered coroutines is a different bug entirely.
        async with seeded.session() as session:
            return await enforce_budget(session, tenant)

    remaining = await asyncio.gather(*(admit() for _ in range(5)))
    # All five passed, on the last fraction of a cent, each seeing the same tiny
    # remainder. This is the overshoot, and it is admitted rather than hidden.
    assert all(r == Decimal("0.000001") for r in remaining)

    await asyncio.gather(*(commit_charge(seeded, tenant, CENT) for _ in range(5)))

    async with seeded.session() as session:
        with pytest.raises(BudgetExceededError):
            await enforce_budget(session, tenant)
        overshoot = await spent_this_period(session, tenant) - tenant.monthly_budget_usd
    # Bounded by what that one burst actually cost — never by how long the month has
    # left to run.
    assert 0 < overshoot <= CENT * 5


# -- the derived reset ------------------------------------------------------


async def test_a_new_month_starts_at_zero_without_a_reset_job(
    seeded: Database, tenants: dict[str, Tenant]
) -> None:
    """The reason there is no cron.

    `period_start` is derived from the timestamp, so September's row is simply not
    October's row. A scheduled reset would have to run once per month, on time, to
    keep this true — and if it ran late, last month's spend would keep blocking a
    paid-up tenant with nothing in the logs to explain why.
    """
    tenant = tenants["search"]
    september = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.UTC)
    october = dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.UTC)

    async with seeded.session() as session:
        await charge(session, tenant, tenant.monthly_budget_usd, moment=september)
        await session.commit()

    async with seeded.session() as session:
        with pytest.raises(BudgetExceededError):
            await enforce_budget(session, tenant, moment=september)
        # Same tenant, same exhausted budget, one second into the next month.
        assert await enforce_budget(session, tenant, moment=october) > 0
        assert await spent_this_period(session, tenant, moment=october) == 0


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.UTC), dt.date(2026, 9, 1)),
        (dt.datetime(2026, 9, 30, 23, 59, 59, tzinfo=dt.UTC), dt.date(2026, 9, 1)),
        (dt.datetime(2026, 12, 31, 23, 59, 59, tzinfo=dt.UTC), dt.date(2026, 12, 1)),
        (dt.datetime(2027, 1, 1, 0, 0, tzinfo=dt.UTC), dt.date(2027, 1, 1)),
    ],
)
def test_the_period_is_the_first_of_the_utc_month(
    moment: dt.datetime, expected: dt.date
) -> None:
    """UTC, not local time. A month boundary that moves with the server's timezone
    would give a tenant two partial Septembers if the host were ever relocated."""
    assert BudgetPeriod.period_for(moment) == expected


def test_an_offset_timestamp_is_converted_before_the_month_is_taken() -> None:
    """The assertion the parametrized cases above cannot make.

    All of those are already UTC, so they would pass even if the conversion were
    dropped. One minute past midnight in a +05:30 zone is still *September* in UTC,
    and a gateway that read the local date would bill it to October — moving a
    tenant's spend into a period they cannot reconcile against their own logs.
    """
    plus_five_thirty = dt.timezone(dt.timedelta(hours=5, minutes=30))
    local_october = dt.datetime(2026, 10, 1, 1, 0, tzinfo=plus_five_thirty)
    assert BudgetPeriod.period_for(local_october) == dt.date(2026, 9, 1)
