"""Liveness and readiness."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.postgres


async def test_healthz_needs_no_credential(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readyz_reports_the_loaded_config(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ready",
        "version": body["version"],
        "providers": 2,
        "aliases": 3,
        "priced_models": 4,
    }


async def test_readyz_does_not_publish_the_upstream_topology(client: AsyncClient) -> None:
    """Counts, not names. A probe should not tell the internet who we call."""
    text = (await client.get("/readyz")).text
    for leak in ("alpha", "beta", "localhost:9001", "mock-key"):
        assert leak not in text


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["x-request-id"]


async def test_a_caller_cannot_choose_the_request_id(client: AsyncClient) -> None:
    """The id is the `request_log` primary key, so a caller may not supply it.

    This replaces an earlier test that asserted the opposite. Echoing an inbound
    `x-request-id` reads as a courtesy for trace correlation, and it was — until the
    id became a primary key. A caller replaying one id then either collides on
    insert, turning a request that was served into a 500, or writes a row under a
    value some other tenant chose. Correlation is not worth handing out the key.
    """
    response = await client.get("/healthz", headers={"x-request-id": "trace-abc"})
    assert response.headers["x-request-id"] != "trace-abc"
    assert len(response.headers["x-request-id"]) == 32
