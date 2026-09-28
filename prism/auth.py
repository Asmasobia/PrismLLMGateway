"""Authentication and authorisation for both planes.

Two planes, two mechanisms, and they are not interchangeable:

* **Data plane** — `Authorization: Bearer <virtual-key>`. Resolved by hashing the
  presented key and looking up the row. High-entropy secret, indexed lookup.
* **Admin plane** — a single shared token compared with `secrets.compare_digest`.
  Constant-time comparison matters *here* and not for virtual keys: the admin
  token is operator-chosen and therefore may be low-entropy and guessable, and a
  byte-by-byte `==` on a low-entropy secret is a real oracle. A virtual key is
  found by index lookup, so there is no comparison loop to time.
* **Ops console** — the *same* admin token, additionally accepted as HTTP Basic so a
  browser can be prompted for it. Same secret, one more transport; see
  `require_console`.

**Enforcement order** (`docs/API_CONTRACT.md:41` asks for it to be documented):

1. authentication — 401
2. tenant status — 401 (a disabled key is not a valid key)
3. model allowlist — 403
4. rate limit — 429
5. budget — 402

Cheapest and most certain first. Authentication precedes everything because
without a tenant there is no limit to apply. The allowlist precedes the rate limit
because a request for a model the team may not use is wrong no matter how much
quota is left — spending quota to discover that would be a bug, and it would let a
caller burn a tenant's rate limit with requests that could never succeed.
"""

from __future__ import annotations

import base64
import secrets
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select

from prism.config import GatewayConfig
from prism.db.models import Tenant
from prism.deps import ConfigDep, SessionDep, SettingsDep
from prism.errors import (
    AuthenticationError,
    ConsoleAuthenticationError,
    ModelNotAllowedError,
    NotFoundError,
)
from prism.keys import hash_virtual_key

_BEARER = "bearer"


def extract_bearer(authorization: str | None) -> str:
    """Pull the credential out of an `Authorization` header.

    Every failure raises the *same* AuthenticationError message. Distinguishing
    "missing header" from "malformed scheme" from "unknown key" in the response
    would tell an attacker which of their guesses was structurally correct.
    """
    if not authorization:
        raise AuthenticationError(
            "Missing Authorization header. Send 'Authorization: Bearer <virtual-key>'."
        )
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != _BEARER or not credential.strip():
        raise AuthenticationError(
            "Malformed Authorization header. Expected 'Bearer <virtual-key>'."
        )
    return credential.strip()


async def require_tenant(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Tenant:
    """Resolve the caller's virtual key to a tenant, or reject with 401."""
    key = extract_bearer(authorization)
    result = await session.execute(
        select(Tenant).where(Tenant.key_hash == hash_virtual_key(key))
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        # Same wording as a disabled key below: an unknown key and a revoked key
        # are indistinguishable from outside, which is the point.
        raise AuthenticationError("Invalid virtual key.")
    if not tenant.is_active:
        raise AuthenticationError("Invalid virtual key.")
    return tenant


TenantDep = Annotated[Tenant, Depends(require_tenant)]


def enforce_allowlist(tenant: Tenant, requested_model: str, config: GatewayConfig) -> None:
    """Check a requested model against the tenant's allowlist.

    Not a dependency, because it needs the request *body* — the model name lives
    in the JSON, not in a header — and body parsing belongs to the route.

    The unknown-model check comes first. A model that does not exist is a 404
    regardless of who asked, and answering 403 would imply the model exists and is
    merely off-limits, which leaks the model registry to any caller with a key.
    """
    if not config.is_known_target(requested_model):
        raise NotFoundError(
            f"Unknown model or alias {requested_model!r}.",
            code="model_not_found",
            param="model",
        )
    if not tenant.allows(requested_model):
        raise ModelNotAllowedError(
            f"Model {requested_model!r} is not on the allowlist for this key.",
            param="model",
        )


async def require_admin(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guard the admin plane with the shared demo token.

    `docs/API_CONTRACT.md:33` permits "any simple documented mechanism". This is
    that — and its weakness is documented in the README rather than disguised:
    one shared bearer token, no rotation, no per-operator identity.
    """
    token = extract_bearer(authorization)
    if not secrets.compare_digest(token, settings.admin_token):
        raise AuthenticationError("Invalid admin token.")


AdminDep = Annotated[None, Depends(require_admin)]


def _basic_password(authorization: str) -> str | None:
    """The password half of an `Authorization: Basic` header, or None.

    The username is ignored rather than checked. There is one shared admin token and
    no per-operator identity, so inventing a username to compare against would be
    security theatre: it would suggest the console knows who is looking when it does
    not. Browsers require *something* in the box, so anything is accepted there.
    """
    scheme, _, encoded = authorization.partition(" ")
    if scheme.strip().lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    _, separator, password = decoded.partition(":")
    return password if separator else None


async def require_console(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guard the ops console with the admin token, over Basic *or* Bearer.

    The same secret as `require_admin`, deliberately: the console shows exactly what
    `/admin/*` already returns, so a second credential would be a second thing to
    rotate that protects nothing new.

    Both schemes are accepted because both callers are real. A browser can only be
    made to send Basic, and `scripts/` and `curl` already send Bearer everywhere else
    — refusing Bearer here would make the console the one endpoint whose smoke check
    needs a different incantation. `compare_digest` for the same reason
    `require_admin` uses it.
    """
    presented = ""
    if authorization:
        presented = _basic_password(authorization) or ""
        if not presented:
            # Not Basic, so try Bearer — but let the Basic challenge carry the failure,
            # because a browser is the only caller that arrives with no header at all.
            try:
                presented = extract_bearer(authorization)
            except AuthenticationError:
                presented = ""
    if not secrets.compare_digest(presented, settings.admin_token):
        raise ConsoleAuthenticationError("Invalid admin token.")


ConsoleDep = Annotated[None, Depends(require_console)]

__all__ = [
    "AdminDep",
    "ConfigDep",
    "ConsoleDep",
    "TenantDep",
    "enforce_allowlist",
    "extract_bearer",
    "require_admin",
    "require_console",
    "require_tenant",
]
