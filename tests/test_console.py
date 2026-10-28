"""The ops console.

Two lanes, like `tests/test_admin.py`. The pure lane tests the rendering helpers
directly, because escaping and the None-to-em-dash rule are the two things most
likely to be quietly wrong and neither needs a database. The Postgres lane drives
real traffic through the data plane and then asserts the page *shows* it — an HTML
page that renders successfully while displaying nothing is the failure mode a
status-code assertion cannot see.

What these tests deliberately do not do is parse the HTML. There is no HTML parser in
the dependency set and adding one to assert on a page this simple would be a worse
trade than substring assertions. The assertions are therefore written against strings
that only appear for the right reason: a team name, a rendered cost, a status token.
"""

from __future__ import annotations

import base64
import datetime as dt
from decimal import Decimal

import pytest
from httpx import AsyncClient

from prism import usage
from prism.api import console
from prism.db.models import RequestStatus
from tests.conftest import ADMIN_TOKEN, BUDGET_DEMO_KEY, FREE_KEY, RESEARCH_KEY, SEARCH_KEY

pg = pytest.mark.postgres

CHAT = "/v1/chat/completions"
CONSOLE = "/console"
BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def basic(user: str = "ops", password: str = ADMIN_TOKEN) -> dict[str, str]:
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def payload(prompt: str = "What is a load balancer?", model: str = "fast", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}


# --------------------------------------------------------------------------
# Rendering helpers. No database.
# --------------------------------------------------------------------------


def test_values_are_escaped_before_they_reach_the_page() -> None:
    """Team names come from the database, so they are not trusted input.

    In any deployment where tenants are self-registered, a team called
    `<script>...` is an attacker-supplied string rendered into an operator's browser
    — and the operator's browser is the one holding the admin token.
    """
    rendered = console._esc('<script>alert("x")</script>')
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert '"' not in rendered  # quote=True, so attribute contexts are safe too


def test_none_renders_as_a_dash_and_not_as_the_word_none() -> None:
    """A rejected request has no provider. "None" in that cell reads as a bug."""
    assert console._esc(None) == "&mdash;"
    assert console._esc(0) == "0"  # and zero is not None
    assert console._esc(False) == "False"


def test_an_empty_table_explains_itself_instead_of_rendering_headers() -> None:
    empty = console._table([("Team", "")], [], empty="No requests in this window.")
    assert "<table>" not in empty
    assert "No requests in this window." in empty


def test_a_row_that_does_not_match_its_headers_raises() -> None:
    """`strict=True` in `_table`. A silently short row is a column that vanishes."""
    with pytest.raises(ValueError):
        console._table([("A", ""), ("B", "")], [["only-one-cell"]], empty="none")


def test_a_threshold_is_shown_at_the_precision_it_was_configured_with() -> None:
    """`numeric(4, 2)` renders as `0.8500` unless something trims it.

    Four decimal places next to a hit rate shown to one implies a precision the seed
    file never expressed.
    """
    assert console._threshold(Decimal("0.8500")) == "0.85"
    assert console._threshold(Decimal("0.92")) == "0.92"
    assert console._threshold(None) == "&mdash;"


def test_the_recent_limit_is_within_what_the_query_layer_allows() -> None:
    assert 0 < console.RECENT_LIMIT <= usage.MAX_LOG_LIMIT


def test_every_logged_status_has_a_pill_class() -> None:
    """A status the console has not learned about must render neutral, not alarming.

    This is the test that fails when a new `RequestStatus` is added without deciding
    how it should look — which is the moment to decide, rather than after a demo
    where a new rejection reason rendered bright red.
    """
    for status in RequestStatus:
        pill = console._status_pill(status.value)
        assert status.value in pill
        assert 'class="pill' in pill
    assert 'class="pill mute"' in console._status_pill("a_status_from_the_future")


# --------------------------------------------------------------------------
# Authentication.
# --------------------------------------------------------------------------


@pg
async def test_the_console_needs_a_credential(client: AsyncClient) -> None:
    response = await client.get(CONSOLE)
    assert response.status_code == 401


@pg
async def test_an_unauthenticated_console_request_challenges_the_browser(
    client: AsyncClient,
) -> None:
    """Without `WWW-Authenticate`, a browser shows a bare 401 and no way to log in."""
    response = await client.get(CONSOLE)
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert 'realm="Prism ops console"' in response.headers["www-authenticate"]


@pg
async def test_the_json_admin_plane_does_not_challenge_the_browser(
    client: AsyncClient,
) -> None:
    """The counterpart. A challenge on the JSON API makes `curl` and XHR misbehave."""
    response = await client.get("/admin/usage")
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


@pg
async def test_basic_auth_with_the_admin_token_is_accepted(client: AsyncClient) -> None:
    assert (await client.get(CONSOLE, headers=basic())).status_code == 200


@pg
async def test_the_basic_username_is_ignored(client: AsyncClient) -> None:
    """There is one shared token and no per-operator identity; the box needs filling."""
    assert (await client.get(CONSOLE, headers=basic(user=""))).status_code == 200
    assert (await client.get(CONSOLE, headers=basic(user="anyone"))).status_code == 200


@pg
async def test_bearer_auth_still_works_so_scripts_need_no_special_case(
    client: AsyncClient,
) -> None:
    assert (await client.get(CONSOLE, headers=BEARER)).status_code == 200


@pg
async def test_the_wrong_token_is_rejected_under_either_scheme(client: AsyncClient) -> None:
    assert (await client.get(CONSOLE, headers=basic(password="wrong"))).status_code == 401
    assert (
        await client.get(CONSOLE, headers={"Authorization": "Bearer wrong"})
    ).status_code == 401


@pg
async def test_a_tenant_virtual_key_does_not_open_the_console(client: AsyncClient) -> None:
    """The two planes are not interchangeable in either direction."""
    assert (await client.get(CONSOLE, headers=auth(SEARCH_KEY))).status_code == 401
    assert (await client.get(CONSOLE, headers=basic(password=SEARCH_KEY))).status_code == 401


@pg
async def test_malformed_credentials_are_rejected_without_raising(
    client: AsyncClient,
) -> None:
    """Undecodable base64 and a colon-less payload must 401, not 500."""
    for header in ("Basic !!!not-base64!!!", f"Basic {base64.b64encode(b'nocolon').decode()}",
                   "Basic", "Digest whatever", ADMIN_TOKEN):
        response = await client.get(CONSOLE, headers={"Authorization": header})
        assert response.status_code == 401, header


# --------------------------------------------------------------------------
# The page itself.
# --------------------------------------------------------------------------


@pg
async def test_the_console_is_html_and_carries_no_external_reference(
    client: AsyncClient,
) -> None:
    """No CDN, no script tag.

    An offline machine must render the page fully styled, and the admin token must
    never be needed by client-side code — see `prism/api/console.py`.
    """
    response = await client.get(CONSOLE, headers=BEARER)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert body.startswith("<!doctype html>")
    assert "<script" not in body
    assert "http://" not in body and "https://" not in body


@pg
async def test_the_console_writes_no_request_log_row(client: AsyncClient) -> None:
    """Operator traffic must not inflate the numbers the operator is reading.

    `prism/main.py:DATA_PLANE_PREFIX` is what guarantees it, and `/console` sits
    outside `/v1/` — but it sits outside `/admin/` too, so the guarantee is worth
    asserting rather than assuming.
    """
    await client.post(CHAT, json=payload(), headers=auth(SEARCH_KEY))
    before = (await client.get("/admin/usage", headers=BEARER)).json()["requests"]
    for _ in range(3):
        assert (await client.get(CONSOLE, headers=BEARER)).status_code == 200
    after = (await client.get("/admin/usage", headers=BEARER)).json()["requests"]
    assert after == before


@pg
async def test_the_console_shows_traffic_it_has_seen(client: AsyncClient) -> None:
    """The page must display the request, not merely return 200 having found it."""
    empty = (await client.get(CONSOLE, headers=BEARER)).text
    assert "No requests logged yet." in empty

    response = await client.post(CHAT, json=payload(), headers=auth(SEARCH_KEY))
    assert response.status_code == 200

    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert "No requests logged yet." not in body
    assert "search" in body
    assert RequestStatus.OK.value in body
    assert "fast" in body  # the alias the client asked for
    assert "alpha/alpha-small" in body  # what actually served it


@pg
async def test_timestamps_are_labelled_utc_and_carry_a_date(client: AsyncClient) -> None:
    """`created_at` is stored tz-aware in UTC, so the value is right either way.

    The label is the point. An operator reading a bare `09:16:53` from a zone several
    hours off concludes the gateway's clock is wrong; and a row from last week that
    renders as a time alone looks like it just happened.
    """
    response = await client.post(CHAT, json=payload(), headers=auth(SEARCH_KEY))
    assert response.status_code == 200

    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert "Time (UTC)" in body
    assert "Last seen (UTC)" in body

    stamp = dt.datetime.now(dt.UTC)
    assert stamp.strftime("%m-%d") in body


@pg
async def test_every_seeded_team_appears_even_with_no_traffic(client: AsyncClient) -> None:
    """`/admin/keys` is driven from the tenant table, and so is this page.

    A console that lists only teams with traffic cannot answer "is budget-demo
    seeded", which is the question the live budget demo depends on.
    """
    body = (await client.get(CONSOLE, headers=BEARER)).text
    for team in ("search", "research", "free-tier", "budget-demo"):
        assert team in body, team


@pg
async def test_the_page_never_carries_key_material_or_a_provider_credential(
    client: AsyncClient,
) -> None:
    """`docs/DATA_MODEL.md:44`. The console renders prefixes, never secrets."""
    await client.post(CHAT, json=payload(), headers=auth(SEARCH_KEY))
    body = (await client.get(CONSOLE, headers=BEARER)).text
    for secret in (SEARCH_KEY, RESEARCH_KEY, FREE_KEY, BUDGET_DEMO_KEY, ADMIN_TOKEN):
        assert secret not in body, secret
    assert "mock-key" not in body


@pg
async def test_the_page_never_carries_prompt_text(client: AsyncClient) -> None:
    """The log does not store prompts, so the console cannot leak one."""
    secret = "explain zarquon indexing to me"
    await client.post(CHAT, json=payload(prompt=secret), headers=auth(SEARCH_KEY))
    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert "zarquon" not in body


@pg
async def test_a_rejected_request_is_shown_as_rejected(client: AsyncClient) -> None:
    """budget-demo's budget is smaller than one request, so this is deterministic.

    The *second* request is the rejected one: the budget is checked against spend
    already recorded, so the first is admitted and is what exhausts it
    (`tests/test_chat.py:test_an_exhausted_budget_is_402_not_429`).
    """
    assert (
        await client.post(CHAT, json=payload(), headers=auth(BUDGET_DEMO_KEY))
    ).status_code == 200
    second = await client.post(
        CHAT, json=payload(prompt="again"), headers=auth(BUDGET_DEMO_KEY)
    )
    assert second.status_code == 402

    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert RequestStatus.REJECTED_BUDGET.value in body
    # And the pill that says the key is at its ceiling, which the live demo relies on.
    assert 'class="pill bad">' in body


@pg
async def test_the_headline_cache_hit_rate_matches_the_admin_api(
    client: AsyncClient,
) -> None:
    """Same rows, same total — that is why `combine_cache_savings` exists.

    Asserted through the API's own number rather than recomputed here, so this test
    fails if either surface starts summing for itself.
    """
    await client.post(CHAT, json=payload(prompt="what is a b-tree?"), headers=auth(SEARCH_KEY))
    await client.post(CHAT, json=payload(prompt="what is a b-tree?"), headers=auth(SEARCH_KEY))

    stats = (await client.get("/admin/cache/stats", headers=BEARER)).json()
    assert stats["hits"] >= 1
    expected = f"{stats['hit_rate'] * 100:.1f}%"

    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert "Cache hit rate" in body
    assert expected in body


@pg
async def test_the_totals_match_the_admin_api(client: AsyncClient) -> None:
    """The cards are the same aggregation `/admin/usage` renders as JSON."""
    for _ in range(3):
        await client.post(CHAT, json=payload(), headers=auth(RESEARCH_KEY))

    total = (await client.get("/admin/usage", headers=BEARER)).json()
    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert f"{total['requests']:,}" in body
    assert f"{total['total_tokens']:,}" in body
    assert f"${Decimal(str(total['cost_usd'])):.6f}" in body


@pg
async def test_a_reconciliation_failure_is_banner_worthy(client: AsyncClient) -> None:
    """The one thing on the page that means "stop and investigate".

    Driven through the real seeded state: every key reconciles, so the banner must be
    absent. Its presence would mean `spent_usd` and the logged costs disagree, and a
    console that showed that quietly in a table cell would be worse than useless.
    """
    await client.post(CHAT, json=payload(), headers=auth(SEARCH_KEY))
    body = (await client.get(CONSOLE, headers=BEARER)).text
    assert "reconciliation" not in body
    assert 'class="pill bad">NO<' not in body


@pg
async def test_the_window_can_be_narrowed_to_exclude_todays_traffic(
    client: AsyncClient,
) -> None:
    """The `from`/`to` parameters are the same ones `/admin/usage` takes.

    A window that ended yesterday must show no requests — proof the dates reach the
    query rather than being accepted and ignored.
    """
    await client.post(CHAT, json=payload(), headers=auth(SEARCH_KEY))
    assert "No requests logged yet." not in (await client.get(CONSOLE, headers=BEARER)).text

    body = (await client.get(f"{CONSOLE}?from=2020-01-01&to=2020-01-02", headers=BEARER)).text
    assert "2020-01-01 to 2020-01-02" in body
    assert "No requests in this window." in body


@pg
async def test_a_malformed_date_is_a_400_and_not_a_500(client: AsyncClient) -> None:
    response = await client.get(f"{CONSOLE}?from=not-a-date", headers=BEARER)
    assert response.status_code == 400
