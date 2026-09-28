"""Virtual-key auth, allowlist enforcement, and the admin plane — over HTTP.

Over HTTP rather than by calling the dependency directly, because half of what is
under test is the status code and the body the exception handler renders, and those
only exist once the request has gone through the app.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from prism.db.models import Tenant, TenantStatus
from prism.db.session import Database
from tests.conftest import RESEARCH_KEY, SEARCH_KEY

pytestmark = pytest.mark.postgres

AUTHED = "/_probe/authed"


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# -- authentication: 401 -------------------------------------------------------


async def test_valid_key_resolves_to_its_tenant(client: AsyncClient) -> None:
    response = await client.post(AUTHED, headers=bearer(SEARCH_KEY))
    assert response.status_code == 200
    assert response.json() == {"team": "search"}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": SEARCH_KEY}, id="no-scheme"),
        pytest.param({"Authorization": "Basic " + SEARCH_KEY}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer"}, id="scheme-only"),
        pytest.param({"Authorization": "Bearer   "}, id="empty-credential"),
        pytest.param({"Authorization": "Bearer definitely-not-a-real-key"}, id="unknown-key"),
    ],
)
async def test_bad_credentials_are_401(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(AUTHED, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


async def test_bearer_scheme_is_case_insensitive(client: AsyncClient) -> None:
    """RFC 7235 makes the scheme token case-insensitive; some clients send `bearer`."""
    response = await client.post(AUTHED, headers={"Authorization": f"bearer {SEARCH_KEY}"})
    assert response.status_code == 200


async def test_smoke_test_expectation_for_an_invalid_key(client: AsyncClient) -> None:
    """scripts/smoke_test.py:103 accepts 401 or 403 here. We return 401."""
    response = await client.post(AUTHED, headers=bearer("definitely-not-a-real-key"))
    assert response.status_code in (401, 403)


async def test_a_disabled_key_is_indistinguishable_from_an_unknown_one(
    client: AsyncClient, seeded: Database
) -> None:
    """docs/DATA_MODEL.md:22 requires a status field; revocation must actually work,
    and must not tell the caller that the key once existed."""
    async with seeded.session() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.team == "search")
            .values(status=TenantStatus.DISABLED.value)
        )
        await session.commit()

    disabled = await client.post(AUTHED, headers=bearer(SEARCH_KEY))
    unknown = await client.post(AUTHED, headers=bearer("prism-sk-nope-000000"))
    assert disabled.status_code == unknown.status_code == 401
    assert disabled.json() == unknown.json()


async def test_the_error_body_never_echoes_the_key(client: AsyncClient) -> None:
    """A rejection body is the easiest place to leak a credential into a log."""
    secret = "prism-sk-leak-me-abcdef"
    response = await client.post(AUTHED, headers=bearer(secret))
    assert secret not in response.text


# -- allowlist: 403, and unknown model: 404 ------------------------------------


async def test_a_model_on_the_allowlist_is_permitted(client: AsyncClient) -> None:
    response = await client.post("/_probe/model/fast", headers=bearer(SEARCH_KEY))
    assert response.status_code == 200


async def test_a_model_off_the_allowlist_is_403(client: AsyncClient) -> None:
    """`search` is allowed only `fast`; `smart` exists but is not granted."""
    response = await client.post("/_probe/model/smart", headers=bearer(SEARCH_KEY))
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "model_not_allowed"


async def test_research_may_use_every_alias(client: AsyncClient) -> None:
    for model in ("fast", "smart", "auto"):
        response = await client.post(f"/_probe/model/{model}", headers=bearer(RESEARCH_KEY))
        assert response.status_code == 200, model


async def test_an_unknown_model_is_404_not_403(client: AsyncClient) -> None:
    """Ordering matters: answering 403 would confirm the model exists.

    scripts/smoke_test.py:107 also requires a 4xx with an `error` body here.
    """
    response = await client.post("/_probe/model/no-such-model-xyz", headers=bearer(SEARCH_KEY))
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"


async def test_authentication_precedes_the_allowlist(client: AsyncClient) -> None:
    """A bad key asking for a forbidden model gets 401, not 403.

    Otherwise the allowlist of a tenant could be probed without a valid key.
    """
    response = await client.post("/_probe/model/smart", headers=bearer("bogus"))
    assert response.status_code == 401
