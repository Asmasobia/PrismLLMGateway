"""Seeding: fidelity to data/seed_keys.json, idempotency, and key hygiene."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from prism.config import ConfigError, GatewayConfig
from prism.db.models import Tenant, TenantStatus
from prism.db.session import Database
from prism.keys import hash_virtual_key
from prism.seed import seed_tenants
from tests.conftest import SEARCH_KEY, SEED_KEYS

pytestmark = pytest.mark.postgres


async def test_seeds_all_four_tenants(database: Database, config: GatewayConfig) -> None:
    async with database.session() as session:
        result = await seed_tenants(session, SEED_KEYS, config=config)
    assert sorted(result.created) == ["budget-demo", "free-tier", "research", "search"]
    assert result.updated == []


async def test_raw_keys_are_never_stored(tenants: dict[str, Tenant]) -> None:
    """The security property the whole hashing design exists for.

    A dump of `tenants` must not contain a usable credential for any team.
    """
    raw_keys = {
        t["virtual_key"]
        for t in json.loads(Path(SEED_KEYS).read_text(encoding="utf-8"))["tenants"]
    }
    stored = " ".join(
        f"{t.team} {t.key_hash} {t.key_prefix}" for t in tenants.values()
    )
    for key in raw_keys:
        assert key not in stored


async def test_key_hash_matches_the_seed_file(tenants: dict[str, Tenant]) -> None:
    assert tenants["search"].key_hash == hash_virtual_key(SEARCH_KEY)


async def test_policy_fields_round_trip(tenants: dict[str, Tenant]) -> None:
    search = tenants["search"]
    assert search.monthly_budget_usd == Decimal("50")
    assert search.requests_per_minute == 60
    assert search.tokens_per_minute == 100_000
    assert search.model_allowlist == ["fast"]
    assert search.cache_enabled is True
    assert search.cache_similarity_threshold == Decimal("0.92")
    assert search.status == TenantStatus.ACTIVE.value


async def test_tiny_budget_survives_the_numeric_scale(tenants: dict[str, Tenant]) -> None:
    """The budget-demo tenant is the reason MONEY has 10 decimal places.

    At a coarser scale this rounds to 0, the tenant looks like it has no budget at
    all rather than a nearly-exhausted one, and the budget_exceeded demo stops
    demonstrating what it claims to.
    """
    assert tenants["budget-demo"].monthly_budget_usd == Decimal("0.00001")
    assert tenants["budget-demo"].monthly_budget_usd > 0


async def test_threshold_is_null_when_the_cache_is_disabled(
    tenants: dict[str, Tenant],
) -> None:
    research = tenants["research"]
    assert research.cache_enabled is False
    assert research.cache_similarity_threshold is None


async def test_free_tier_keeps_its_own_threshold(tenants: dict[str, Tenant]) -> None:
    """Thresholds are per tenant, not global — 0.85 here, 0.92 for search."""
    assert tenants["free-tier"].cache_similarity_threshold == Decimal("0.85")


async def test_seeding_twice_updates_rather_than_duplicating(
    database: Database, config: GatewayConfig
) -> None:
    async with database.session() as session:
        await seed_tenants(session, SEED_KEYS, config=config)
    async with database.session() as session:
        again = await seed_tenants(session, SEED_KEYS, config=config)
        count = (await session.execute(select(func.count()).select_from(Tenant))).scalar_one()
    assert again.created == []
    assert len(again.updated) == 4
    assert count == 4


async def test_an_allowlist_naming_an_unknown_model_is_rejected(
    database: Database, config: GatewayConfig, tmp_path: Path
) -> None:
    """Caught at seed time, because at request time it looks like a gateway bug."""
    bad = tmp_path / "seed.json"
    bad.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "team": "typo",
                        "virtual_key": "prism-sk-typo-000000",
                        "monthly_budget_usd": 1,
                        "rate_limit": {"requests_per_minute": 10},
                        "model_allowlist": ["fastt"],
                        "semantic_cache": {"enabled": False},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    async with database.session() as session:
        with pytest.raises(ConfigError, match="neither a configured alias"):
            await seed_tenants(session, bad, config=config)


async def test_cache_enabled_without_a_threshold_is_rejected(
    database: Database, config: GatewayConfig, tmp_path: Path
) -> None:
    bad = tmp_path / "seed.json"
    bad.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "team": "half-configured",
                        "virtual_key": "prism-sk-half-000000",
                        "monthly_budget_usd": 1,
                        "rate_limit": {"requests_per_minute": 10},
                        "model_allowlist": ["fast"],
                        "semantic_cache": {"enabled": True},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    async with database.session() as session:
        with pytest.raises(ConfigError, match="similarity_threshold"):
            await seed_tenants(session, bad, config=config)
