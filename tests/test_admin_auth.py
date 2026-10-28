"""The admin plane's token guard."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import ADMIN_TOKEN, SEARCH_KEY

pytestmark = pytest.mark.postgres


async def test_the_admin_token_is_accepted(client: AsyncClient) -> None:
    response = await client.get(
        "/_probe/admin", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )
    assert response.status_code == 200


async def test_a_missing_token_is_401(client: AsyncClient) -> None:
    assert (await client.get("/_probe/admin")).status_code == 401


async def test_a_virtual_key_cannot_reach_the_admin_plane(client: AsyncClient) -> None:
    """The planes are separate credentials, not one credential with two scopes.

    A tenant key that could read `/admin/usage` would expose every other team's
    spend, which is the multi-tenancy boundary the whole gateway rests on.
    """
    response = await client.get(
        "/_probe/admin", headers={"Authorization": f"Bearer {SEARCH_KEY}"}
    )
    assert response.status_code == 401


async def test_a_near_miss_token_is_rejected(client: AsyncClient) -> None:
    response = await client.get(
        "/_probe/admin", headers={"Authorization": f"Bearer {ADMIN_TOKEN}x"}
    )
    assert response.status_code == 401
