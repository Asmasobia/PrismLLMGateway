"""Process configuration, read from the environment.

Split deliberately from `prism.config`: this module holds *deployment* settings
(where the database is, where the model cache is), while `prism.config` holds the
*gateway* configuration (providers, aliases, prices) loaded from JSON files.
They change for different reasons and at different times.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-derived settings. Missing required values fail at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Every field below is read from an explicit PRISM_* alias rather than
        # from the field name, so renaming a field never silently changes the
        # environment contract documented in .env.example.
        populate_by_name=True,
    )

    database_url: str = Field(alias="PRISM_DATABASE_URL")
    admin_token: str = Field(alias="PRISM_ADMIN_TOKEN")

    # Named `embedding_cache_dir`, not `model_cache`: pydantic reserves the
    # `model_` prefix for its own attributes and warns on collisions.
    embedding_cache_dir: Path = Field(alias="PRISM_MODEL_CACHE")

    gateway_config_path: Path = Field(
        default=Path("gateway_config.json"), alias="PRISM_GATEWAY_CONFIG"
    )
    pricing_path: Path = Field(
        default=Path("data/model_pricing.json"), alias="PRISM_PRICING_FILE"
    )
    seed_path: Path = Field(default=Path("data/seed_keys.json"), alias="PRISM_SEED_FILE")

    host: str = Field(default="0.0.0.0", alias="PRISM_HOST")
    port: int = Field(default=8080, alias="PRISM_PORT")

    # Upstream timeouts. Two numbers, not one, because connecting and generating
    # fail for different reasons and on different timescales — see the module
    # docstring in prism/providers/http.py. Defaults are deliberately loose enough
    # for a real provider; the failover demo lowers them.
    upstream_timeout_seconds: float = Field(
        default=30.0, gt=0, alias="PRISM_UPSTREAM_TIMEOUT_SECONDS"
    )
    upstream_connect_timeout_seconds: float = Field(
        default=5.0, gt=0, alias="PRISM_UPSTREAM_CONNECT_TIMEOUT_SECONDS"
    )

    # Cache entry lifetime. Deployment-wide rather than per tenant, because the
    # per-tenant knob `data/seed_keys.json` actually gives is the similarity
    # threshold, and inventing a second per-tenant field would mean editing provided
    # data. `None` — the default — means entries never expire, which is what makes a
    # paraphrase demo reproducible: an entry written during setup is still there
    # when the camera is on. A deployment that cares about staleness sets a number.
    cache_ttl_seconds: int | None = Field(default=None, gt=0, alias="PRISM_CACHE_TTL_SECONDS")

    @field_validator("database_url")
    @classmethod
    def _must_be_async_postgres(cls, v: str) -> str:
        """Catch the most common misconfiguration before it becomes a runtime error.

        A synchronous driver URL (`postgresql://`) appears to work right up until
        the first query, then fails inside the event loop with an error that does
        not name the real cause.
        """
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "PRISM_DATABASE_URL must use the asyncpg driver, i.e. start with "
                f"'postgresql+asyncpg://' (got {v.split('://')[0]!r}://...). "
                "Postgres is required rather than SQLite; see CLAUDE.md."
            )
        return v

    def require_embedding_cache(self) -> Path:
        """Fail fast if the vendored embedding model is absent.

        The model is never fetched on the request path (docs/DESIGN_NOTES.md), so
        a missing cache must be a startup error naming the variable to set, not a
        surprise on the first cache lookup.
        """
        path = self.embedding_cache_dir
        if not path.is_dir():
            raise RuntimeError(
                f"PRISM_MODEL_CACHE points at {path}, which is not a directory. "
                "The embedding model is a vendored artifact and is never downloaded "
                "at request time. See the README section 'The embedding model is not "
                "installed by pip'."
            )
        return path


def load_settings() -> Settings:
    """Read settings from the environment. Kept as a function, not a module-level
    singleton, so tests can build a Settings object without touching the process
    environment."""
    return Settings()  # type: ignore[call-arg]  # values come from env/.env
