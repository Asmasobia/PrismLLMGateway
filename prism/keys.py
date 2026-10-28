"""Virtual-key handling.

Isolated in its own module so there is exactly one place that decides how a key
becomes a database lookup. If the hash ever changes, it changes here and in the
seeder, and nowhere else.
"""

from __future__ import annotations

import hashlib

#: How much of the key is kept in cleartext for display. `prism-sk-search-1a2b3c`
#: keeps `prism-sk-search`: enough to identify the tenant in an admin view,
#: nowhere near enough to reconstruct the secret.
_PREFIX_SEGMENTS = 3


def hash_virtual_key(key: str) -> str:
    """SHA-256 hex digest of a virtual key.

    Unsalted, deliberately. A per-row salt would make lookup impossible — we get
    a key and need the row, so the digest has to be deterministic across rows.
    That is safe here only because the input is high-entropy and machine-generated:
    there is no rainbow table for a 128-bit random string. The same reasoning is
    why this is SHA-256 and not bcrypt (see `Tenant`).
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """A non-secret label derived from the key, for logs and admin views."""
    parts = key.split("-")
    if len(parts) <= _PREFIX_SEGMENTS:
        # Short or unconventional key: show only the first segment rather than
        # risk echoing most of the secret.
        return parts[0][:16]
    return "-".join(parts[:_PREFIX_SEGMENTS])
