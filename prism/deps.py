"""FastAPI dependencies for shared, app-scoped resources.

Everything long-lived (the engine, the parsed config) hangs off `app.state` and is
reached through a dependency rather than a module global. That is what lets a test
build a second app against a throwaway database in the same process, and it is
what makes the wiring visible in each route's signature instead of implicit in an
import.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from prism.config import GatewayConfig
from prism.db.session import Database
from prism.dispatch import Dispatcher
from prism.embeddings import MemoEmbedder
from prism.providers.base import ProviderClient
from prism.ratelimit import SlidingWindowLimiter
from prism.routing import Classifier
from prism.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_config(request: Request) -> GatewayConfig:
    return request.app.state.config


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_limiter(request: Request) -> SlidingWindowLimiter:
    """The process-wide rate limiter.

    App-scoped, not request-scoped — the whole point is that it is shared state.
    Reached through a dependency anyway so a test can build a second app with its
    own limiter and a controllable clock, instead of reaching into a module global
    and leaking window state between tests.
    """
    return request.app.state.limiter


def get_dispatcher(request: Request) -> Dispatcher:
    """The retry/failover policy, built once from the config at startup.

    App-scoped for the same reason as the limiter, and injectable for a sharper one:
    a test that exercised the real backoff would spend the real 600 ms per failover,
    every run, forever. See `prism/dispatch.py`.
    """
    return request.app.state.dispatcher


def get_provider_client(request: Request) -> ProviderClient:
    """The provider adapter. One per process; see `prism/providers/http.py`."""
    return request.app.state.provider_client


def get_embedder(request: Request) -> MemoEmbedder:
    """A per-request memo in front of the process-wide embedding model.

    App-scoped model, request-scoped memo. The model holds an ONNX session and is
    built once (see `prism/main.py`); the memo exists so that the two consumers that
    embed during one request — the difficulty router and the semantic cache — do not
    each pay for the same string. FastAPI caches a dependency's result within a
    request, so every route and sub-dependency that asks for this gets *the same*
    memo, which is the entire mechanism.
    """
    return MemoEmbedder(request.app.state.embedder)


EmbedderDep = Annotated[MemoEmbedder, Depends(get_embedder)]


def get_classifier(request: Request, embedder: EmbedderDep) -> Classifier:
    """The difficulty classifier, bound to this request's embedding memo."""
    return request.app.state.classifier.bind(embedder)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed when the request ends.

    Deliberately does *not* wrap the request in a single transaction. The data
    plane needs to commit a rejection's log row and then return an error; a
    request-scoped transaction that rolls back on error would erase exactly the
    audit trail `docs/DATA_MODEL.md:57` requires for rejected requests.
    """
    db: Database = request.app.state.db
    async with db.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ConfigDep = Annotated[GatewayConfig, Depends(get_config)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
LimiterDep = Annotated[SlidingWindowLimiter, Depends(get_limiter)]
ProviderDep = Annotated[ProviderClient, Depends(get_provider_client)]
DispatcherDep = Annotated[Dispatcher, Depends(get_dispatcher)]
ClassifierDep = Annotated[Classifier, Depends(get_classifier)]
