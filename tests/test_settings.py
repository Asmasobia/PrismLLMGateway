"""Startup validation. These are the errors an operator sees before anything runs."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from prism.settings import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://prism:prism@localhost:5433/prism_test",
        "admin_token": "t",
        "embedding_cache_dir": Path("."),
    }
    base.update(overrides)
    # `_env_file=None` keeps this machine's real .env out of the test. Otherwise a
    # local override would make the default-value assertions below pass or fail
    # depending on whose checkout they run in.
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_a_synchronous_driver_url_is_rejected() -> None:
    """`postgresql://` works until the first query, then fails inside the event loop
    with an error that does not name the real cause. Catch it at startup."""
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url="postgresql://prism:prism@localhost:5433/prism")


def test_sqlite_is_rejected() -> None:
    """CLAUDE.md's storage decision, enforced rather than merely documented."""
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url="sqlite+aiosqlite:///prism.db")


def test_an_asyncpg_url_is_accepted() -> None:
    assert _settings().database_url.startswith("postgresql+asyncpg://")


def test_a_missing_model_cache_fails_with_an_actionable_message(tmp_path: Path) -> None:
    settings = _settings(embedding_cache_dir=tmp_path / "not-there")
    with pytest.raises(RuntimeError, match="PRISM_MODEL_CACHE"):
        settings.require_embedding_cache()


def test_a_present_model_cache_is_returned(tmp_path: Path) -> None:
    assert _settings(embedding_cache_dir=tmp_path).require_embedding_cache() == tmp_path


def test_defaults_match_the_documented_environment() -> None:
    """.env.example is the contract; these defaults must not drift from it."""
    settings = _settings()
    assert settings.port == 8080
    assert settings.gateway_config_path == Path("gateway_config.json")
    assert settings.pricing_path == Path("data/model_pricing.json")
    assert settings.seed_path == Path("data/seed_keys.json")
