"""Liveness and readiness.

Unauthenticated by design: a health probe that needs a credential is a health
probe that reports "unhealthy" the moment the credential rotates. Neither endpoint
returns anything a caller could not learn by sending a request — no provider names,
no base URLs, no counts.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from prism import __version__
from prism.deps import ConfigDep, SessionDep

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: is the process up and serving? No dependencies touched.

    Deliberately does not check the database. Liveness and readiness answer
    different questions, and conflating them means an orchestrator restarts a
    perfectly healthy gateway because Postgres blinked — which loses in-flight
    streams and makes the outage worse.
    """
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
async def readyz(session: SessionDep, config: ConfigDep) -> dict[str, object]:
    """Readiness: can this instance actually serve a request right now?"""
    await session.execute(text("SELECT 1"))
    return {
        "status": "ready",
        "version": __version__,
        # Counts only. Naming providers here would publish the upstream topology
        # to anyone who can reach the port.
        "providers": len(config.provider_names),
        "aliases": len(config.alias_names),
        "priced_models": len(config.priced_models),
    }
