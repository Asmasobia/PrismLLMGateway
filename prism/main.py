"""Application assembly.

`create_app` is a factory, not a module-level `app = FastAPI()`. Two reasons that
matter beyond style:

* a test can build an app against a throwaway database in the same process, with
  no environment mutation and no import-order tricks;
* nothing connects to Postgres or reads a config file at *import* time, so
  `import prism.main` stays free of side effects.

Uvicorn still gets a module-level `app` at the bottom, built from the environment.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from prism import __version__, audit
from prism.api import admin, chat, console, health
from prism.config import GatewayConfig, load_gateway_config
from prism.db.models import RequestStatus
from prism.db.session import Database
from prism.dispatch import Dispatcher
from prism.embeddings import Embedder, FastEmbedEmbedder
from prism.errors import (
    InvalidRequestError,
    PrismError,
    prism_error_handler,
    unhandled_error_handler,
)
from prism.providers.base import ProviderClient
from prism.providers.http import HttpProviderClient
from prism.ratelimit import SlidingWindowLimiter
from prism.routing import Classifier, SemanticClassifier
from prism.settings import Settings, load_settings

logger = logging.getLogger("prism")

REQUEST_ID_HEADER = "x-request-id"

#: Paths that get a `request_log` row. The admin plane and the health probes
#: deliberately do not: the log is the audit trail for *tenant* traffic, and mixing
#: operator requests into it would corrupt every count the usage API derives from it.
DATA_PLANE_PREFIX = "/v1/"


async def _assert_schema_ready(db: Database) -> None:
    """Fail startup if the schema has not been initialised.

    The serving process deliberately does **not** run DDL. A gateway that creates
    its own tables on boot will happily create them in the wrong database after a
    typo in `PRISM_DATABASE_URL`, then serve traffic against an empty tenant table
    and reject every request with a 401 that looks like an auth bug. Schema
    creation is an explicit operator step (`scripts/init_db.py`); startup only
    verifies it happened.
    """
    try:
        async with db.session() as session:
            count = (await session.execute(text("SELECT count(*) FROM tenants"))).scalar_one()
    except (ProgrammingError, DBAPIError) as exc:
        raise RuntimeError(
            "The 'tenants' table is missing or unreadable. Initialise the database "
            "first:\n    python scripts/init_db.py\n"
            f"(underlying error: {type(exc).__name__})"
        ) from exc
    if not count:
        raise RuntimeError(
            "The 'tenants' table is empty, so every request would be rejected with "
            "401. Seed it:\n    python scripts/init_db.py"
        )
    logger.info("schema ready, %d tenant(s) loaded", count)


def create_app(
    settings: Settings | None = None,
    *,
    config: GatewayConfig | None = None,
    database: Database | None = None,
    provider_client: ProviderClient | None = None,
    limiter: SlidingWindowLimiter | None = None,
    dispatcher: Dispatcher | None = None,
    embedder: Embedder | None = None,
    classifier: Classifier | None = None,
    verify_schema: bool = True,
) -> FastAPI:
    """Build the application.

    Every collaborator can be injected. Injection is what keeps the tests honest:
    they exercise the real app object and the real dependency graph, not a
    hand-rolled stand-in that could diverge from it.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or load_settings()

        resolved_config = config or load_gateway_config(
            resolved_settings.gateway_config_path, resolved_settings.pricing_path
        )
        resolved_db = database or Database(resolved_settings.database_url)
        resolved_providers = provider_client or HttpProviderClient(
            timeout_seconds=resolved_settings.upstream_timeout_seconds,
            connect_timeout_seconds=resolved_settings.upstream_connect_timeout_seconds,
        )
        # The limiter is process state, so it is created once here rather than per
        # request. Injectable so a test can supply a controllable clock — waiting
        # sixty seconds to observe a window roll over is not a test.
        resolved_limiter = limiter or SlidingWindowLimiter()
        # Built here rather than per request so the retry policy is read from the
        # config exactly once, and so a test can inject one whose backoff does not
        # actually sleep. See prism/dispatch.py.
        resolved_dispatcher = dispatcher or Dispatcher(resolved_config.retry)

        # The embedding model. `require_embedding_cache` runs *inside* this branch
        # rather than unconditionally at the top of the lifespan: a caller who
        # injected an embedder has no use for the artifact, and failing startup over
        # a directory nothing is going to read would be a lie about what is wrong.
        # When nothing is injected — every real deployment — the check still runs
        # before the model is touched, and fails naming PRISM_MODEL_CACHE.
        if embedder is None:
            resolved_embedder: Embedder = FastEmbedEmbedder(
                resolved_settings.require_embedding_cache()
            )
        else:
            resolved_embedder = embedder
        # Both of these are startup costs on purpose: the first inference builds the
        # ONNX arenas, and `prepare` embeds the exemplar set. Deferring either would
        # charge the first request several hundred milliseconds it did not cause, and
        # would do it again after every deploy.
        await resolved_embedder.warm()
        resolved_classifier = classifier or SemanticClassifier(resolved_embedder)
        if isinstance(resolved_classifier, SemanticClassifier):
            await resolved_classifier.prepare()

        app.state.settings = resolved_settings
        app.state.config = resolved_config
        app.state.db = resolved_db
        app.state.provider_client = resolved_providers
        app.state.limiter = resolved_limiter
        app.state.dispatcher = resolved_dispatcher
        app.state.embedder = resolved_embedder
        app.state.classifier = resolved_classifier

        if verify_schema:
            await _assert_schema_ready(resolved_db)

        logger.info(
            "prism %s ready: %d providers, %d aliases, classifier=%s",
            __version__,
            len(resolved_config.provider_names),
            len(resolved_config.alias_names),
            type(resolved_classifier).__name__,
        )
        try:
            yield
        finally:
            # Only dispose what we created. Disposing an injected engine or an
            # injected HTTP client would break a caller that reuses it across
            # several apps.
            if provider_client is None:
                await resolved_providers.aclose()
            if database is None:
                await resolved_db.dispose()

    app = FastAPI(
        title="Prism",
        version=__version__,
        description="A multi-tenant, OpenAI-compatible LLM gateway.",
        lifespan=lifespan,
    )

    # One place decides status codes and bodies; see prism/errors.py. These two
    # wrappers add the audit write and then delegate, so the error contract stays in
    # `prism/errors.py` and the logging decision stays here.
    async def handle_prism_error(request: Request, exc: PrismError) -> JSONResponse:
        """Render a PrismError, and log the rejection it represents.

        This is the only place a 401, 403, 429 or 402 can be logged from: three of
        those four are raised inside the route, but a 401 comes from a dependency
        and never reaches it. See the module docstring in `prism/audit.py`.
        """
        context = audit.current(request)
        if context is not None:
            await audit.record_rejection(
                request.app.state.db,
                context,
                status=audit.status_for(exc.error_type),
                http_status=exc.status_code,
            )
        return await prism_error_handler(request, exc)

    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Answer a malformed body with an OpenAI-shaped 400, and log it.

        FastAPI's default is a 422 with its own body shape. Both are wrong for a
        data plane advertised as OpenAI-compatible: a client written against OpenAI
        branches on 400 and parses `error.message`, and would treat this as an
        unrecognised failure.

        The reported detail is limited to the field location and pydantic's own
        message. `exc.errors()` also carries the offending *input*, which for this
        endpoint is the caller's prompt — echoing it into an error body would put
        tenant content somewhere it was never meant to go.
        """
        first = (exc.errors() or [{}])[0]
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = first.get("msg", "Request body is not valid.")
        error = InvalidRequestError(
            f"Invalid request: {location or 'body'}: {message}",
            param=location or None,
        )
        context = audit.current(request)
        if context is not None:
            await audit.record_rejection(
                request.app.state.db,
                context,
                status=RequestStatus.INVALID_REQUEST.value,
                http_status=error.status_code,
            )
        return await prism_error_handler(request, error)

    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Log the request as an internal error, then return the redacted 500."""
        context = audit.current(request)
        if context is not None:
            await audit.record_rejection(
                request.app.state.db,
                context,
                status=RequestStatus.INTERNAL_ERROR.value,
                http_status=500,
            )
        return await unhandled_error_handler(request, exc)

    app.add_exception_handler(PrismError, handle_prism_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        """Attach a request id to every request, and an audit context to tenant traffic.

        The id is generated here, not in the route, for two reasons: it is the
        primary key of the `request_log` row, and rejections that never reach a
        route still need one.

        It is generated rather than taken from an inbound `x-request-id`. Honouring
        a caller-supplied id looks like a courtesy — it would let a team correlate
        their trace with ours — but it hands a caller the choice of a primary key.
        Replaying the same id then either collides on insert, turning a served
        request into a 500, or lets one tenant's row be attributed to a value
        another tenant chose. The id is ours; correlation is not worth that.

        The audit context is created *before* any dependency runs, which is what
        makes a 401 loggable: `require_tenant` raises before the route exists.
        """
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        if request.url.path.startswith(DATA_PLANE_PREFIX):
            audit.begin(request, request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(admin.router)
    app.include_router(console.router)
    return app


app = create_app()
