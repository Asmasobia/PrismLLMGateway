"""Config loading, validation, pricing, and credential hygiene. No server needed."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from prism.config import (
    ConfigError,
    DegradationPolicy,
    GatewayConfig,
    ModelAlias,
    ModelPrice,
    Provider,
    RetryPolicy,
    load_gateway_config,
    load_prices,
)
from prism.errors import NotFoundError

ROOT = Path(__file__).resolve().parents[1]


def test_sample_config_loads(config: GatewayConfig) -> None:
    assert set(config.provider_names) == {"alpha", "beta"}
    assert set(config.alias_names) == {"fast", "smart", "auto"}
    assert len(config.priced_models) == 4
    assert config.retry.max_attempts == 3


def test_provider_repr_never_leaks_the_api_key() -> None:
    """docs/DATA_MODEL.md:44 — provider keys must not reach logs.

    The default dataclass repr would print the key, and every logging call that
    formats an object goes through repr. This is the guard for that.
    """
    provider = Provider(name="alpha", base_url="http://x/v1", api_key="super-secret-key")
    assert "super-secret-key" not in repr(provider)
    assert "super-secret-key" not in str(provider)
    assert "***" in repr(provider)


def test_prices_are_exact_decimals() -> None:
    prices = load_prices(ROOT / "data" / "model_pricing.json")
    # Decimal("0.15"), not Decimal(0.15). The latter would be
    # 0.1499999999999999944488848768742172978818416595458984375.
    assert prices["alpha-small"].input_per_1m == Decimal("0.15")
    assert prices["alpha-large"].output_per_1m == Decimal("15.00")


def test_cost_is_order_independent() -> None:
    """The property that motivates Decimal over float for money.

    Summing the same set of costs in two different orders must give the same total,
    because the load test's reconciliation compares a client-side total against a
    server-side one and the request completion order is not deterministic.
    """
    price = ModelPrice("alpha-small", Decimal("0.15"), Decimal("0.60"))
    costs = [price.cost(p, c) for p, c in [(5, 17), (7, 3), (11, 29), (2, 101)]]
    assert sum(costs) == sum(reversed(costs))
    # And the value itself is the exact decimal, not a float artefact.
    assert price.cost(1_000_000, 1_000_000) == Decimal("0.75")


def test_zero_tokens_cost_nothing() -> None:
    price = ModelPrice("alpha-small", Decimal("0.15"), Decimal("0.60"))
    assert price.cost(0, 0) == Decimal("0")


def test_provider_for_model_resolves_by_convention(config: GatewayConfig) -> None:
    assert config.provider_for_model("alpha-small").name == "alpha"
    assert config.provider_for_model("beta-large").name == "beta"
    with pytest.raises(NotFoundError):
        config.provider_for_model("gamma-small")


def test_alias_chain_is_primary_then_fallbacks(config: GatewayConfig) -> None:
    fast = config.alias("fast")
    assert fast is not None
    assert fast.chain == ("alpha-small", "beta-small")
    assert not fast.is_router


def test_auto_is_a_router(config: GatewayConfig) -> None:
    auto = config.alias("auto")
    assert auto is not None
    assert auto.is_router
    assert auto.chain == ()
    assert auto.route_by_difficulty == {"simple": "fast", "complex": "smart"}


def test_is_known_target_covers_aliases_and_concrete_models(config: GatewayConfig) -> None:
    assert config.is_known_target("fast")
    assert config.is_known_target("alpha-small")
    assert not config.is_known_target("no-such-model-xyz")


def test_retry_backoff_grows_geometrically() -> None:
    retry = RetryPolicy(max_attempts=3, initial_backoff_ms=200, backoff_multiplier=2)
    assert [retry.backoff_ms(i) for i in range(3)] == [200, 400, 800]


# -- validation: each of these would otherwise surface as a request-time 500 ----


def _config(**aliases: ModelAlias) -> GatewayConfig:
    return GatewayConfig(
        providers={"alpha": Provider("alpha", "http://x/v1", "k")},
        aliases=aliases,
        prices={"alpha-small": ModelPrice("alpha-small", Decimal("1"), Decimal("1"))},
        retry=RetryPolicy(),
        degradation=DegradationPolicy(),
    )


def test_alias_with_neither_primary_nor_router_is_rejected() -> None:
    with pytest.raises(ConfigError, match="neither a primary model"):
        _config(broken=ModelAlias(name="broken"))


def test_alias_that_is_both_chain_and_router_is_rejected() -> None:
    with pytest.raises(ConfigError, match="either a router or a chain"):
        _config(
            broken=ModelAlias(
                name="broken", primary="alpha-small", route_by_difficulty={"simple": "fast"}
            )
        )


def test_unpriced_model_in_a_chain_is_rejected() -> None:
    with pytest.raises(ConfigError, match="no entry in the price table"):
        _config(fast=ModelAlias(name="fast", primary="alpha-huge"))


def test_router_pointing_at_a_router_is_rejected() -> None:
    """This is the infinite-resolution-loop guard.

    `auto -> auto` would recurse forever in the resolver, and the resolver is the
    one code path shared by every request.
    """
    with pytest.raises(ConfigError, match="itself a router"):
        _config(
            fast=ModelAlias(name="fast", primary="alpha-small"),
            auto=ModelAlias(name="auto", route_by_difficulty={"simple": "auto2"}),
            auto2=ModelAlias(name="auto2", route_by_difficulty={"simple": "fast"}),
        )


def test_router_pointing_at_an_unknown_alias_is_rejected() -> None:
    with pytest.raises(ConfigError, match="not a configured alias"):
        _config(auto=ModelAlias(name="auto", route_by_difficulty={"simple": "nope"}))


def test_missing_config_file_names_the_fix(tmp_path: Path) -> None:
    # Escaped: the dots are literal, and an unescaped pattern would also match a
    # message naming some *other* file whose name happened to differ only there.
    with pytest.raises(ConfigError, match=re.escape("gateway_config.sample.json")):
        load_gateway_config(tmp_path / "absent.json", ROOT / "data" / "model_pricing.json")


def test_duplicate_provider_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {"name": "alpha", "base_url": "http://a/v1", "api_key": "k"},
                    {"name": "alpha", "base_url": "http://b/v1", "api_key": "k"},
                ],
                "model_aliases": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Duplicate provider"):
        load_gateway_config(path, ROOT / "data" / "model_pricing.json")


def test_comment_keys_are_stripped(config: GatewayConfig) -> None:
    """The provided JSON documents itself with `_comment`; none of it is config."""
    assert "_comment" not in config.alias_names
    assert "_comment" not in config.priced_models
