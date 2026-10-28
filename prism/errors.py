"""The error contract.

`docs/API_CONTRACT.md:106-113` fixes a status code and an error `type` for each
failure mode. Encoding that table once, here, is what stops the mapping drifting
as handlers are added: no route decides its own status code.

The response body is OpenAI-shaped, because the data plane must stay
OpenAI-compatible (`docs/API_CONTRACT.md:5`) and clients written against OpenAI
parse `error.message` / `error.type`.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class PrismError(Exception):
    """Base for every client-visible error.

    Subclasses set `status_code` and `error_type`; nothing else may invent them.
    """

    status_code: int = 500
    error_type: str = "internal_error"
    #: A `WWW-Authenticate` value, set only by failures that a *browser* should be
    #: invited to retry with credentials. The JSON API deliberately leaves this None:
    #: answering a programmatic 401 with a challenge makes an XHR client pop a native
    #: password box it cannot fill, and makes `curl` retry instead of reporting.
    challenge: str | None = None

    def __init__(self, message: str, *, code: str | None = None, param: str | None = None):
        super().__init__(message)
        self.message = message
        # The worked example at `docs/API_CONTRACT.md:96-102` shows `code` equal to
        # `type`. Defaulting rather than hardcoding keeps that shape while leaving
        # room for a narrower code (e.g. which provider failed) where one helps.
        self.code = code if code is not None else self.error_type
        self.param = param

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "param": self.param,
            }
        }


class InvalidRequestError(PrismError):
    """The request body itself is unusable.

    400, not FastAPI's default 422. OpenAI answers a malformed body with 400 and
    clients written against it branch on that; a 422 sends them down an unexpected
    path for a case they already handle. The trade is that 400 conflates "wrong
    shape" with other client errors, which is why the body still carries a `param`
    naming the field at fault.
    """

    status_code = 400
    error_type = "invalid_request_error"


class AuthenticationError(PrismError):
    """Missing, malformed, or unknown virtual key."""

    status_code = 401
    error_type = "authentication_error"


class ConsoleAuthenticationError(AuthenticationError):
    """The same 401, but one a browser can act on.

    The ops console is the only HTML surface here, and the address bar cannot send
    `Authorization: Bearer`. Carrying a Basic challenge lets the browser prompt for
    the admin token, which keeps the secret out of the URL — a `?token=` parameter
    would end up in shell history, the referrer of any outbound link, and every
    access log between here and the operator.
    """

    challenge = 'Basic realm="Prism ops console", charset="UTF-8"'


class ModelNotAllowedError(PrismError):
    """The key is valid but the requested model is not on its allowlist."""

    status_code = 403
    error_type = "model_not_allowed"


class RateLimitExceededError(PrismError):
    """The key exceeded its requests-per-minute limit."""

    status_code = 429
    error_type = "rate_limit_exceeded"


class BudgetExceededError(PrismError):
    """The key has exhausted its budget.

    `docs/API_CONTRACT.md:110` permits 429 or 402. We use **402**, for a concrete
    reason rather than taste: `scripts/load_test.py:106` classifies every 429 as
    "rate limited". Returning 429 here would make a budget-exhausted key look
    like a rate-limiter result in the load-test report, corrupting the one number
    that test exists to measure. 402 also tells a client something actionable —
    retrying later cannot help, unlike a 429.
    """

    status_code = 402
    error_type = "budget_exceeded"


class NotFoundError(PrismError):
    """Something named in the request does not exist.

    A model or alias on the data plane; on the admin plane, a `key=` selector that
    matches no tenant. The narrower `code` says which — `model_not_found` versus
    `key_not_found` — so one status code can serve both without the body being vague.
    """

    status_code = 404
    error_type = "not_found_error"


class UpstreamError(PrismError):
    """Every provider in the resolved chain failed."""

    status_code = 502
    error_type = "upstream_error"


async def prism_error_handler(request: Request, exc: PrismError) -> JSONResponse:
    """Render a PrismError.

    `Retry-After` is set for 429 so a well-behaved client backs off instead of
    hammering; it is deliberately *not* set for 402, where waiting does not help.
    """
    headers: dict[str, str] = {}
    if exc.status_code == 429:
        headers["Retry-After"] = "1"
    if exc.challenge:
        headers["WWW-Authenticate"] = exc.challenge
    return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler.

    Deliberately does not echo `str(exc)`. An unexpected exception may carry a
    provider API key, a DSN, or a row of another tenant's data, and
    `docs/DATA_MODEL.md:44` requires gateway secrets never to reach a
    client-visible error. The detail belongs in the server log, keyed by
    request id.
    """
    return JSONResponse(
        status_code=500,
        content=PrismError("Internal server error.").body(),
    )
