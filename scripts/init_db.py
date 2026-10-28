#!/usr/bin/env python
"""Create Prism's schema and seed the tenants from `data/seed_keys.json`.

Not part of the provided pack — this is Prism's own operator entry point. It is
separate from the serving process on purpose: `prism.main` verifies the schema but
never creates it, so a typo in `PRISM_DATABASE_URL` cannot silently produce a
second, empty database that the gateway then serves 401s from.

    python scripts/init_db.py            # create missing tables, upsert tenants
    python scripts/init_db.py --reset    # DROP every Prism table first
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prism.config import ConfigError, load_gateway_config  # noqa: E402
from prism.db.session import Database  # noqa: E402
from prism.seed import seed_tenants  # noqa: E402
from prism.settings import load_settings  # noqa: E402


async def main(reset: bool) -> int:
    settings = load_settings()
    try:
        config = load_gateway_config(settings.gateway_config_path, settings.pricing_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    db = Database(settings.database_url)
    try:
        if reset:
            # Guarded rather than silent: this is the one destructive path in the
            # repository and it must never be something you can trigger by
            # habit-typing the command you always run.
            print("This DROPS every Prism table, including request_log and usage.")
            if input("Type 'reset' to continue: ").strip() != "reset":
                print("aborted")
                return 1
            await db.drop_schema()
            print("dropped all Prism tables")

        await db.create_schema()
        print("schema created (existing tables left untouched)")

        async with db.session() as session:
            result = await seed_tenants(session, settings.seed_path, config=config)
    except ConfigError as exc:
        print(f"seed error: {exc}", file=sys.stderr)
        return 2
    finally:
        await db.dispose()

    if result.created:
        print(f"created {len(result.created)} tenant(s): {', '.join(result.created)}")
    if result.updated:
        print(f"updated {len(result.updated)} tenant(s): {', '.join(result.updated)}")
    print(
        f"{result.total} tenant(s) ready. "
        f"{len(config.provider_names)} provider(s), {len(config.alias_names)} alias(es), "
        f"{len(config.priced_models)} priced model(s)."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true", help="drop all Prism tables before creating them"
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().reset)))
