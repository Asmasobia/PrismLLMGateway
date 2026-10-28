"""Engine and session lifecycle.

Wrapped in a small class rather than module-level globals so tests can stand up a
second database without monkeypatching an import, and so shutdown actually
disposes the pool instead of leaving asyncpg connections for the interpreter to
garbage-collect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from prism.db.models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            # Sized for the load test: it fires bursts well above the rate limit,
            # and every rejected request still writes a log row. A pool that
            # cannot absorb the burst turns a rate-limit test into a pool-timeout
            # test and measures the wrong thing.
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            # Without this, every attribute read after commit triggers a lazy
            # refresh — which in async SQLAlchemy raises MissingGreenlet rather
            # than quietly doing IO. Objects are read after commit constantly
            # (a Tenant outliving the auth session, for one), so it must be off.
            expire_on_commit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def create_schema(self) -> None:
        """Create any missing tables.

        `create_all`, not Alembic. Alembic earns its keep when a schema has to
        change without losing data that already exists — there is none yet, and a
        migration chain written before the first schema is stable is a chain of
        migrations you end up squashing. The README's Known limitations records
        this as deferred, not forgotten.
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_schema(self) -> None:
        """Drop every table. For tests and for `scripts/init_db.py --reset`."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
