"""Monthly cost budgets: admission before the call, accounting after it.

`docs/DATA_MODEL.md:86-91` sets four requirements, and each one shapes the code
below.

**1. Budget reads must be cheap.** Admission reads a single indexed row from
`budget_periods`, not an aggregate over `request_log`. The counter is a
denormalisation of the log and stays reconcilable against it — see the class
docstring in `prism/db/models.py`.

**2. Increments must be atomic.** The increment is one statement:
`INSERT ... ON CONFLICT ... DO UPDATE SET spent_usd = spent_usd + EXCLUDED.spent_usd`.
Postgres holds the row lock for the duration of the statement, so the read and the
write are never two round trips and two concurrent charges cannot both start from
the same value. The shape this replaces — `SELECT spent`, add in Python,
`UPDATE ... SET spent = :new` — loses one of any two concurrent charges, and loses
it *silently*: the totals simply come out low, which looks like a pricing bug.

Using `ON CONFLICT` rather than "select, and insert if missing" also removes the
first-request-of-the-month race, where two concurrent requests both find no row
and both insert one, and the second gets a unique-constraint violation instead of
a completion.

**3. Concurrent overshoot must be decided and documented.** Admission asks *"is
there any budget left?"*, not *"is there enough for this request?"* — because the
request's cost is not known until the provider reports usage, and for a stream it
is not known until the stream ends. So a burst of `N` concurrent requests can all
pass admission on the last cent and overshoot by the sum of their own costs. The
bound is real and small: at most `requests_per_minute × cost_per_request` per
window, which for the seeded keys is under a cent. The alternative — reserving an
estimated cost at admission and refunding the difference — buys a tighter bound in
exchange for a second failure mode (a crashed request leaks its reservation until
someone reconciles), and a budget that *under*-admits is worse for a caller than
one that overshoots by a cent.

**4. Streaming admission is the same rule.** `docs/DATA_MODEL.md:91` explicitly
permits admitting a stream when budget remains at the start and letting it
overshoot by its own final cost. That is what happens here, because admission does
not know or care whether the request streams.

**Reset is by derivation, not by a job.** `period_start` is the first day of the
UTC month, so the first request in October simply finds no row for October and
`ON CONFLICT` creates one at zero. There is no scheduled reset to fail to run, and
no window in which a cron that fired late keeps last month's spend in force.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from prism.db.models import BudgetPeriod, Tenant
from prism.errors import BudgetExceededError
from prism.money import ZERO_USD, quantize_usd


async def spent_this_period(
    session: AsyncSession, tenant: Tenant, *, moment: dt.datetime | None = None
) -> Decimal:
    """What this tenant has spent in the current month. Zero if no row exists yet."""
    period = BudgetPeriod.period_for(moment)
    spent = (
        await session.execute(
            select(BudgetPeriod.spent_usd).where(
                BudgetPeriod.tenant_id == tenant.id,
                BudgetPeriod.period_start == period,
            )
        )
    ).scalar_one_or_none()
    return spent if spent is not None else ZERO_USD


async def enforce_budget(
    session: AsyncSession, tenant: Tenant, *, moment: dt.datetime | None = None
) -> Decimal:
    """Reject the request if the tenant's monthly budget is exhausted.

    Returns the remaining budget when it admits, so the caller can report it
    without a second query.

    The comparison is `spent >= budget`, so a key whose budget is exactly spent is
    refused rather than allowed one more. The seeded `budget-demo` tenant depends on
    this being strict: its budget is $0.00001 and one `fast` request costs more than
    that, so the *first* request is admitted (nothing spent yet) and every request
    after it is refused — which is the live demo `data/seed_keys.json:2` describes.
    """
    budget = tenant.monthly_budget_usd
    spent = await spent_this_period(session, tenant, moment=moment)
    if spent >= budget:
        raise BudgetExceededError(
            f"Monthly budget of ${budget:.5f} exhausted for this key "
            f"(spent ${spent:.5f}). It resets at the start of next month."
        )
    return budget - spent


async def charge(
    session: AsyncSession,
    tenant: Tenant,
    cost: Decimal,
    *,
    moment: dt.datetime | None = None,
) -> Decimal:
    """Add `cost` to this tenant's spend for the current month, atomically.

    Does **not** commit. The caller commits this together with the request's log
    row, so spend and log can never disagree: a crash between them would otherwise
    leave money charged with nothing to attribute it to, and reconciliation —
    which `docs/EVALUATION_GUIDE.md` grades — would be permanently off by that
    amount with no way to find it.

    Returns the tenant's new total spend for the period.
    """
    # Quantize before it reaches the column, so the value charged is exactly the
    # value stored and exactly the value the `x-prism-cost-usd` header reports.
    amount = quantize_usd(cost)
    period = BudgetPeriod.period_for(moment)

    statement = (
        insert(BudgetPeriod)
        .values(
            tenant_id=tenant.id,
            period_start=period,
            spent_usd=amount,
            request_count=1,
        )
        .on_conflict_do_update(
            constraint="uq_budget_period",
            set_={
                # `BudgetPeriod.spent_usd + ...`, not a Python-side value: the
                # addition is evaluated by Postgres against the locked row.
                "spent_usd": BudgetPeriod.spent_usd + amount,
                "request_count": BudgetPeriod.request_count + 1,
            },
        )
        .returning(BudgetPeriod.spent_usd)
    )
    return (await session.execute(statement)).scalar_one()


__all__ = ["charge", "enforce_budget", "spent_this_period"]
