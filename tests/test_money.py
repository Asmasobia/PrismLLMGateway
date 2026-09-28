"""Money is only correct if one number reaches three places unchanged.

`docs/API_CONTRACT.md:140` grades the sum of `x-prism-cost-usd` headers against the
usage API, so the header, the `request_log` row and the budget counter must all
carry the same value. These tests guard the two ways that can quietly stop being
true: a scale mismatch between the helper and the column, and a formatter that
emits something a client parses differently than we wrote it.
"""

from __future__ import annotations

from decimal import Decimal

from prism.db.models import MONEY
from prism.money import MONEY_SCALE, format_usd, quantize_usd


def test_money_scale_matches_the_column() -> None:
    """The duplication in prism/money.py is guarded here rather than by a comment.

    `MONEY_SCALE` exists so the cost can be rounded before it is charged, formatted
    or stored. If someone widens the column and forgets the constant, every value
    would be truncated on the way in and the header would stop matching the row —
    a discrepancy in the tenth decimal place that no manual check would catch.
    """
    assert MONEY.scale == MONEY_SCALE


def test_quantize_rounds_half_away_from_zero_like_postgres() -> None:
    # Python's default is ROUND_HALF_EVEN, which would give ...0002 here.
    assert quantize_usd(Decimal("0.00000000015")) == Decimal("0.0000000002")
    assert quantize_usd(Decimal("0.00000000025")) == Decimal("0.0000000003")


def test_format_never_uses_scientific_notation() -> None:
    """`str(Decimal('2E-5'))` is `2E-5`, which is not what belongs in a header."""
    tiny = Decimal("0.00002")
    assert format_usd(tiny) == "0.0000200000"
    assert "e" not in format_usd(tiny).lower()


def test_formatted_costs_sum_to_the_stored_total() -> None:
    """The reconciliation property, in miniature.

    Thirty identical requests: summing what the client saw in the headers must equal
    thirty times what was stored. Truncating the header to six decimals — the width
    of the example in the contract — breaks this.
    """
    unit = quantize_usd(Decimal("0.15") * 7 / 1_000_000 + Decimal("0.60") * 41 / 1_000_000)
    client_side = sum(Decimal(format_usd(unit)) for _ in range(30))
    assert client_side == unit * 30
