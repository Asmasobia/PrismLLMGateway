"""Shared fixtures.

Two rules this suite follows:

1. **Never touch the real database.** Every Postgres test runs against a separate
   `<db>_test` database, created on demand, with the schema dropped and rebuilt per
   test. A suite that can corrupt the database you demo from is a suite you stop
   running.
2. **Skip, do not fail, when Postgres is absent.** The unit tests (config, pricing,
   key hashing, the error table) need no server and must stay runnable anywhere,
   including a CI box with no database and no network.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from prism.auth import TenantDep, enforce_allowlist, require_admin
from prism.config import GatewayConfig, load_gateway_config
from prism.db.models import Tenant
from prism.db.session import Database
from prism.deps import ConfigDep
from prism.dispatch import Dispatcher
from prism.embeddings import FakeEmbedder
from prism.main import create_app
from prism.providers.fake import FakeProviderClient
from prism.ratelimit import SlidingWindowLimiter
from prism.routing import LengthClassifier
from prism.seed import seed_tenants
from prism.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONFIG = ROOT / "data" / "gateway_config.sample.json"
PRICING = ROOT / "data" / "model_pricing.json"
SEED_KEYS = ROOT / "data" / "seed_keys.json"

#: Mirrors data/seed_keys.json. Duplicated here on purpose: if someone edits the
#: provided pack, the tests should fail loudly rather than silently follow along.
SEARCH_KEY = "prism-sk-search-1a2b3c"
RESEARCH_KEY = "prism-sk-research-4d5e6f"
#: 10 requests per minute — the smallest seeded limit, so a rate-limit test needs
#: eleven requests rather than sixty-one.
FREE_KEY = "prism-sk-free-7g8h9i"
#: $0.00001 monthly budget, less than one request costs. See data/seed_keys.json:2.
BUDGET_DEMO_KEY = "prism-sk-budget-demo-0j1k2l"
ADMIN_TOKEN = "test-admin-token"

#: The development database, from the environment when it is set. Tests never use
#: it directly — they use `<name>_test`, derived below — but they do connect to it
#: once, as the only database guaranteed to exist, in order to CREATE the other.
DEV_DATABASE_URL = os.environ.get(
    "PRISM_DATABASE_URL", "postgresql+asyncpg://prism:prism@localhost:5433/prism"
)


def _test_database_url() -> str:
    head, _, name = DEV_DATABASE_URL.rpartition("/")
    return f"{head}/{name}_test"


def _test_database_name() -> str:
    return _test_database_url().rpartition("/")[2]


# --------------------------------------------------------------------------
# Unit-level fixtures: no server, no network.
# --------------------------------------------------------------------------


@pytest.fixture
def config() -> GatewayConfig:
    """The provided sample config, parsed. Read-only, so sharing is safe."""
    return load_gateway_config(SAMPLE_CONFIG, PRICING)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at the test database and a stand-in model cache.

    The model cache only has to *exist* for startup's fail-fast check; nothing in
    day 1 loads the model, so an empty tmp directory is the honest stand-in. The
    real model is exercised by the cache slice.
    """
    return Settings(
        database_url=_test_database_url(),
        admin_token=ADMIN_TOKEN,
        embedding_cache_dir=tmp_path,
        gateway_config_path=SAMPLE_CONFIG,
        pricing_path=PRICING,
        seed_path=SEED_KEYS,
    )


# --------------------------------------------------------------------------
# Postgres fixtures.
# --------------------------------------------------------------------------


#: Result of the one-and-only connection probe: `None` once Postgres is known good,
#: a reason string once it is known bad, and absent until we have tried.
#:
#: Cached because a connect attempt against a port with nothing listening costs
#: about four seconds on Windows. Probing per test turned a clean skip on a
#: database-less machine into a three-minute run — the tests were being skipped
#: correctly and still made the suite useless.
_PG_PROBE: dict[str, str | None] = {}


async def _ensure_test_database() -> None:
    """Create `prism_test` if it does not exist.

    `CREATE DATABASE` cannot run inside a transaction, hence AUTOCOMMIT. Done here
    rather than in a setup script so a fresh clone can run `pytest` as its first
    command.
    """
    name = _test_database_name()
    admin = create_async_engine(DEV_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
                )
            ).scalar()
            if not exists:
                # The name is derived from our own configuration, not from request
                # input, and CREATE DATABASE takes no bind parameters — hence the
                # interpolation, with quoting so an unusual name still parses.
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """A Database on a freshly built schema, skipped if Postgres is unreachable."""
    if "reason" not in _PG_PROBE:
        try:
            await _ensure_test_database()
            _PG_PROBE["reason"] = None
        except Exception as exc:  # noqa: BLE001 - any failure here means "no server"
            _PG_PROBE["reason"] = (
                f"Postgres unreachable at {DEV_DATABASE_URL.rpartition('@')[2]} "
                f"({type(exc).__name__}). Start it with ./scripts/pg.sh start."
            )
    if _PG_PROBE["reason"]:
        pytest.skip(_PG_PROBE["reason"])

    db = Database(settings.database_url)
    # Drop first: a previous run that crashed mid-test leaves rows behind, and a
    # test that passes only because of leftover state is worse than no test.
    await db.drop_schema()
    await db.create_schema()
    try:
        yield db
    finally:
        await db.drop_schema()
        await db.dispose()


@pytest.fixture
async def seeded(database: Database, config: GatewayConfig) -> Database:
    async with database.session() as session:
        await seed_tenants(session, SEED_KEYS, config=config)
    return database


class FakeClock:
    """A clock the test moves by hand.

    The rate limiter's window is sixty seconds. Testing that a window rolls over by
    sleeping through it would add a minute to the suite per assertion; testing it by
    reaching into the limiter's internals would test the internals instead of the
    behaviour. Injecting the clock tests the real code path at whatever time the
    test says it is.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def providers() -> FakeProviderClient:
    """An in-process upstream. See prism/providers/fake.py for why it is not a mock."""
    return FakeProviderClient()


@pytest.fixture
def limiter(clock: FakeClock) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(clock=clock)


class RecordingSleeper:
    """Records the delays the dispatcher asked for instead of serving them.

    The real policy backs off 200 ms then 400 ms, so a single failover test that
    exhausted its retries would add 600 ms to every future run of the suite. The
    delays are still asserted on — see tests/test_dispatch.py — they are just not
    lived through.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def dispatcher(config: GatewayConfig, sleeper: RecordingSleeper) -> Dispatcher:
    """The real dispatcher with the real policy, minus the waiting.

    `jitter=lambda: 1.0` pins the sampled backoff to its ceiling, so the recorded
    delays are the nominal 0.2 s / 0.4 s rather than a random fraction of them.
    """
    return Dispatcher(config.retry, sleep=sleeper, jitter=lambda: 1.0)


def _probe_app(
    settings: Settings,
    config: GatewayConfig,
    database: Database,
    providers: FakeProviderClient,
    limiter: SlidingWindowLimiter,
    dispatcher: Dispatcher,
) -> FastAPI:
    """The real app plus three probe routes.

    The probes predate `POST /v1/chat/completions` and are kept now that it exists:
    they exercise the auth dependency and the error contract through HTTP without
    also exercising routing, metering and the provider call, so a failure in one of
    those does not make the auth tests fail for the wrong reason.
    """
    app = create_app(
        settings,
        config=config,
        database=database,
        provider_client=providers,
        limiter=limiter,
        dispatcher=dispatcher,
        # No artifact and no ONNX in the general suite: `settings.embedding_cache_dir`
        # is an empty tmp directory, and loading 65 MB of runtime per test app would
        # add minutes to a run that currently takes seconds.
        embedder=FakeEmbedder(),
        # The length baseline, so these tests keep asserting on the routing behaviour
        # they were written against. Semantic routing is graded by
        # `scripts/routing_eval.py` and wired-up-ness is proved by the one test that
        # builds an app with a SemanticClassifier — mixing quality assertions into
        # every chat test would make an exemplar edit break the metering tests.
        classifier=LengthClassifier(),
        verify_schema=False,
    )

    @app.post("/_probe/authed")
    async def _authed(tenant: TenantDep) -> dict[str, str]:
        return {"team": tenant.team}

    @app.post("/_probe/model/{model}")
    async def _model(model: str, tenant: TenantDep, cfg: ConfigDep) -> dict[str, str]:
        enforce_allowlist(tenant, model, cfg)
        return {"team": tenant.team, "model": model}

    @app.get("/_probe/admin", dependencies=[Depends(require_admin)])
    async def _admin() -> dict[str, bool]:
        return {"admin": True}

    return app


@pytest.fixture
async def client(
    settings: Settings,
    config: GatewayConfig,
    seeded: Database,
    providers: FakeProviderClient,
    limiter: SlidingWindowLimiter,
    dispatcher: Dispatcher,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client against the real app, with lifespan actually run.

    The lifespan matters: it is what populates `app.state`, so a test that skipped
    it would exercise a differently-wired app than production.
    """
    app = _probe_app(settings, config, seeded, providers, limiter, dispatcher)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://prism.test") as c:
            yield c


# --------------------------------------------------------------------------
# The vendored embedding model.
# --------------------------------------------------------------------------


#: Built once per session, not per test: the artifact is 65 MB of ONNX and the first
#: inference allocates arenas. Per-test construction turned the `model` lane from
#: seconds into minutes for no additional coverage — the model is stateless.
@pytest.fixture(scope="session")
def real_embedder():
    """The real embedder, or a skip. Never a download.

    Skipped rather than failed when `PRISM_MODEL_CACHE` is unset or empty, for the
    same reason the Postgres lane skips: these tests must not be the thing that
    stops someone running the suite on a fresh checkout. The gateway itself does the
    opposite and refuses to start — a *served* request that silently lost semantic
    routing is a different kind of problem from a test that could not run.
    """
    # The same two sources, in the same order, that `prism.settings` uses: the
    # process environment wins, `.env` is the fallback. Reading only `os.environ`
    # here skipped the whole lane on a machine where the model *was* present,
    # because the variable lives in `.env` — a green run that proved nothing.
    from dotenv import dotenv_values

    cache = os.environ.get("PRISM_MODEL_CACHE") or dotenv_values(ROOT / ".env").get(
        "PRISM_MODEL_CACHE"
    )
    if not cache or not Path(cache).is_dir() or not any(Path(cache).iterdir()):
        pytest.skip(
            "PRISM_MODEL_CACHE is unset or empty; the vendored embedding model is "
            "required for tests marked `model`. See the README."
        )
    from prism.embeddings import FastEmbedEmbedder

    return FastEmbedEmbedder(Path(cache))


@pytest.fixture
async def tenants(seeded: Database) -> dict[str, Tenant]:
    """Seeded tenants keyed by team, for tests that assert on stored rows."""
    from sqlalchemy import select

    async with seeded.session() as session:
        rows = (await session.execute(select(Tenant))).scalars().all()
    return {t.team: t for t in rows}
