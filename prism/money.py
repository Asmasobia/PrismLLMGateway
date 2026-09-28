"""One representation of money, shared by the header, the log row, and the budget.

`docs/API_CONTRACT.md:140` requires the sum of the `x-prism-cost-usd` headers a
client observed to reconcile *exactly* with what the usage API reports. That is
only true if the number in the header is the same number that was stored — so
rounding happens **once**, here, before the cost is either charged or rendered.

The failure this prevents is subtle and would have passed a casual test:
`ModelPrice.cost()` returns a Decimal with up to 28 significant digits, the
`NUMERIC(18,10)` column silently rounds it on the way in, and a header formatted
from the unrounded value then differs from the stored value in the eleventh
decimal place. Thirty requests later the load test's client-side total no longer
matches the usage API and there is nothing in either number to point at why.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Decimal places kept for money. Must equal the scale of `MONEY` in
#: `prism/db/models.py`; `tests/test_money.py` asserts they agree rather than
#: trusting a comment to stay true.
MONEY_SCALE = 10

_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)


def quantize_usd(amount: Decimal) -> Decimal:
    """Round to the stored scale, the way Postgres would.

    `ROUND_HALF_UP` rather than Python's default `ROUND_HALF_EVEN`, because
    Postgres `NUMERIC` rounds half away from zero. Using banker's rounding here
    would mean the value we charge and the value the column stores can differ on
    an exact half — rare, but it would show up as an unexplainable one-quantum
    drift in reconciliation.
    """
    return amount.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def format_usd(amount: Decimal) -> str:
    """Render for the `x-prism-cost-usd` header.

    Fixed-point with every stored digit, never scientific notation. A cost of
    2e-5 formatted by `str()` on some paths becomes `2E-5`, and
    `scripts/load_test.py:59` parses this header with `float()` — which accepts
    `2E-5`, but a shell or spreadsheet reading the header often does not. Fixed
    notation is unambiguous everywhere and, because it carries all ten stored
    digits, summing the headers reproduces the stored total exactly.
    """
    return f"{quantize_usd(amount):.{MONEY_SCALE}f}"


ZERO_USD = Decimal(0)

__all__ = ["MONEY_SCALE", "ZERO_USD", "format_usd", "quantize_usd"]
