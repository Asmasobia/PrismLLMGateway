"""Load `data/seed_keys.json` into the `tenants` table.

Idempotent, keyed on `team`. Re-running after editing a budget or rotating a key
updates the row rather than failing on a unique constraint — which matters because
this runs on every `scripts/init_db.py`, including against a database that already
has traffic logged against those tenants.

Why tenants are in Postgres at all, when providers and prices are not: a tenant
has mutable state that must survive a restart (`docs/DATA_MODEL.md:113`) and rows
that point at it (`request_log`, `cache_entries`, `budget_periods`). Providers and
prices have neither.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prism.config import ConfigError, GatewayConfig, strip_comments
from prism.db.models import Tenant, TenantStatus
from prism.keys import hash_virtual_key, key_prefix


@dataclass
class SeedResult:
    created: list[str]
    updated: list[str]

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated)


def _threshold(entry: dict) -> Decimal | None:
    cache = entry.get("semantic_cache", {})
    if not cache.get("enabled"):
        return None
    raw = cache.get("similarity_threshold")
    if raw is None:
        raise ConfigError(
            f"Tenant {entry.get('team')!r} enables the semantic cache without a "
            "similarity_threshold; docs/DATA_MODEL.md:19 requires one when enabled."
        )
    # via str(): see load_prices for why a float must never go straight to Decimal.
    return Decimal(str(raw))


async def seed_tenants(
    session: AsyncSession, path: Path, *, config: GatewayConfig | None = None
) -> SeedResult:
    """Insert or update every tenant in the seed file.

    Passing `config` additionally validates each allowlist entry against the
    loaded aliases and models. Worth doing at seed time rather than request time:
    an allowlist naming a model that does not exist produces a tenant that gets a
    404 for a model it was explicitly granted, and that reads as a gateway bug
    rather than a config typo.
    """
    raw = strip_comments(json.loads(path.read_text(encoding="utf-8")))
    entries = raw.get("tenants", [])
    if not entries:
        raise ConfigError(f"No tenants found in {path}.")

    result = SeedResult(created=[], updated=[])
    for entry in entries:
        try:
            team = entry["team"]
            virtual_key = entry["virtual_key"]
            allowlist = list(entry["model_allowlist"])
            rpm = int(entry["rate_limit"]["requests_per_minute"])
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"Malformed tenant entry {entry!r}: missing {exc}") from exc

        if config is not None:
            for model in allowlist:
                if not config.is_known_target(model):
                    raise ConfigError(
                        f"Tenant {team!r} is allowed model {model!r}, which is neither "
                        "a configured alias nor a priced model."
                    )

        existing = (
            await session.execute(select(Tenant).where(Tenant.team == team))
        ).scalar_one_or_none()

        tenant = existing or Tenant(team=team)
        tenant.key_hash = hash_virtual_key(virtual_key)
        tenant.key_prefix = key_prefix(virtual_key)
        tenant.status = entry.get("status", TenantStatus.ACTIVE.value)
        tenant.monthly_budget_usd = Decimal(str(entry["monthly_budget_usd"]))
        tenant.requests_per_minute = rpm
        tenant.tokens_per_minute = entry.get("rate_limit", {}).get("tokens_per_minute")
        tenant.model_allowlist = allowlist
        tenant.cache_enabled = bool(entry.get("semantic_cache", {}).get("enabled", False))
        tenant.cache_similarity_threshold = _threshold(entry)

        if existing is None:
            session.add(tenant)
            result.created.append(team)
        else:
            result.updated.append(team)

    await session.commit()
    return result
