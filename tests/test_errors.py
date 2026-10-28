"""The error contract, asserted against the table in docs/API_CONTRACT.md:106-113.

This is the cheapest test in the suite and one of the most useful: the mapping is
graded, it is easy to change by accident, and nothing else would notice.
"""

from __future__ import annotations

import pytest

from prism.errors import (
    AuthenticationError,
    BudgetExceededError,
    ModelNotAllowedError,
    NotFoundError,
    PrismError,
    RateLimitExceededError,
    UpstreamError,
)

# (exception, status, type) — transcribed from the contract, not from the code.
CONTRACT = [
    (AuthenticationError, 401, "authentication_error"),
    (ModelNotAllowedError, 403, "model_not_allowed"),
    (RateLimitExceededError, 429, "rate_limit_exceeded"),
    (BudgetExceededError, 402, "budget_exceeded"),  # contract permits 429 or 402
    (NotFoundError, 404, "not_found_error"),
    (UpstreamError, 502, "upstream_error"),
]


@pytest.mark.parametrize(("exc_class", "status", "error_type"), CONTRACT)
def test_status_and_type_match_the_contract(
    exc_class: type[PrismError], status: int, error_type: str
) -> None:
    exc = exc_class("boom")
    assert exc.status_code == status
    assert exc.error_type == error_type


def test_body_is_openai_shaped() -> None:
    body = BudgetExceededError("Monthly budget of $5.00 exhausted for this key").body()
    assert set(body) == {"error"}
    assert body["error"]["message"].startswith("Monthly budget")
    assert body["error"]["type"] == "budget_exceeded"
    # docs/API_CONTRACT.md:96-102 shows code mirroring type.
    assert body["error"]["code"] == "budget_exceeded"


def test_code_can_be_narrowed_without_changing_type() -> None:
    body = NotFoundError("nope", code="model_not_found", param="model").body()
    assert body["error"]["type"] == "not_found_error"
    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["param"] == "model"


def test_every_case_is_distinguishable_from_the_body() -> None:
    """docs/API_CONTRACT.md:115 — 'each case must be distinguishable from the body'."""
    types = [exc_class("x").error_type for exc_class, _, _ in CONTRACT]
    assert len(set(types)) == len(types)
