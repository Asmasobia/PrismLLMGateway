"""The gateway configuration: providers, model aliases, prices, retry policy.

Loaded from JSON at startup, validated once, then immutable. Two consequences of
that choice worth naming:

* **Validated at startup, not per request.** A malformed alias chain is a
  deployment error. Discovering it on the first request that happens to use that
  alias turns a config typo into a production 500.
* **Immutable after load.** The resolution path is read-only, so no lock is
  needed around it and no request can observe a half-updated chain.

Provider credentials live here, and `docs/DATA_MODEL.md:44` requires them never
to reach a response, a log, or a client-visible error. `Provider.__repr__` is
overridden to redact the key, because the default dataclass repr would print it
the first time anything logged a provider object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from prism.errors import NotFoundError


class ConfigError(Exception):
    """Raised at startup for an invalid configuration. Never client-visible."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str

    def __repr__(self) -> str:  # pragma: no cover - defensive, exercised by test
        # Never let a credential reach a log line or a traceback by accident.
        return f"Provider(name={self.name!r}, base_url={self.base_url!r}, api_key='***')"


@dataclass(frozen=True)
class ModelAlias:
    """Either a concrete chain (`primary` + ordered `fallbacks`) or a router.

    `auto` is a router: it maps a difficulty label to *another alias*, which is
    then resolved by the same code path. Keeping one resolution function rather
    than a separate "router path" is what makes `auto` inherit retries and
    failover for free instead of re-implementing them.
    """

    name: str
    primary: str | None = None
    fallbacks: tuple[str, ...] = ()
    route_by_difficulty: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_router(self) -> bool:
        return bool(self.route_by_difficulty)

    @property
    def chain(self) -> tuple[str, ...]:
        """Primary first, then fallbacks, in order. Empty for a router."""
        if self.primary is None:
            return ()
        return (self.primary, *self.fallbacks)


@dataclass(frozen=True)
class ModelPrice:
    """Prices in USD per 1M tokens.

    `Decimal`, not `float`. `docs/EVALUATION_GUIDE.md` requires the load test's
    reported spend to reconcile with the usage API, and binary floats do not sum
    associatively — 1000 additions of 1.4e-5 lands in a different place depending
    on the order the requests finished. Decimal makes the sum order-independent.
    """

    model: str
    input_per_1m: Decimal
    output_per_1m: Decimal

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            self.input_per_1m * Decimal(prompt_tokens) / million
            + self.output_per_1m * Decimal(completion_tokens) / million
        )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_ms: int = 200
    backoff_multiplier: float = 2.0

    def backoff_ms(self, attempt: int) -> float:
        """Delay before retry `attempt` (0-based: attempt 0 needs no delay)."""
        return self.initial_backoff_ms * (self.backoff_multiplier**attempt)


@dataclass(frozen=True)
class DegradationPolicy:
    error_rate_threshold: float = 0.5
    window_seconds: int = 60
    p95_latency_ms: int = 5000


@dataclass(frozen=True)
class ResolvedTarget:
    """One concrete (provider, model) attempt in a resolved chain."""

    provider: Provider
    model: str

    @property
    def label(self) -> str:
        """The `x-prism-provider` header value, e.g. `alpha/alpha-small`."""
        return f"{self.provider.name}/{self.model}"


class GatewayConfig:
    """Immutable, validated view of the gateway's configuration."""

    def __init__(
        self,
        providers: Mapping[str, Provider],
        aliases: Mapping[str, ModelAlias],
        prices: Mapping[str, ModelPrice],
        retry: RetryPolicy,
        degradation: DegradationPolicy,
    ) -> None:
        self._providers = dict(providers)
        self._aliases = dict(aliases)
        self._prices = dict(prices)
        self.retry = retry
        self.degradation = degradation
        self._validate()

    # -- introspection ----------------------------------------------------

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    @property
    def alias_names(self) -> tuple[str, ...]:
        return tuple(self._aliases)

    @property
    def priced_models(self) -> tuple[str, ...]:
        return tuple(self._prices)

    def provider(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError:
            raise NotFoundError(f"Unknown provider {name!r}.") from None

    def alias(self, name: str) -> ModelAlias | None:
        return self._aliases.get(name)

    def price(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            # A model with no price cannot be billed, and silently billing $0
            # would corrupt the accounting the evaluation reconciles.
            raise NotFoundError(f"No price on file for model {model!r}.") from None

    def is_known_target(self, name: str) -> bool:
        """True if `name` is either an alias or a priced concrete model."""
        return name in self._aliases or name in self._prices

    # -- provider ownership ------------------------------------------------

    def provider_for_model(self, model: str) -> Provider:
        """Map a concrete model name to the provider that serves it.

        The provided config states providers and models but never says which
        provider owns which model; the pack encodes it in the naming convention
        `{provider}-{size}`, and `scripts/validate_pack.py:64` relies on exactly
        that. So Prism relies on it too — but in **one** function, not scattered
        through the routing code, and never by naming a provider in source. That
        satisfies the problem statement's ban on hardcoded provider names while
        still matching the data we were given: to switch to an explicit
        `models: [...]` field per provider, only this function changes.
        """
        prefix = model.rsplit("-", 1)[0]
        provider = self._providers.get(prefix)
        if provider is None:
            raise NotFoundError(
                f"Model {model!r} does not belong to any registered provider."
            )
        return provider

    # -- validation --------------------------------------------------------

    def _validate(self) -> None:
        """Reject a configuration that would fail later, at request time.

        Mirrors the invariants `scripts/validate_pack.py` checks on the provided
        data, applied to whatever config this deployment actually loaded.
        """
        if not self._providers:
            raise ConfigError("No providers configured.")
        if not self._prices:
            raise ConfigError("No model prices configured.")

        for alias in self._aliases.values():
            if alias.is_router and alias.primary is not None:
                raise ConfigError(
                    f"Alias {alias.name!r} sets both route_by_difficulty and primary; "
                    "an alias is either a router or a chain, not both."
                )
            if not alias.is_router and alias.primary is None:
                raise ConfigError(
                    f"Alias {alias.name!r} has neither a primary model nor "
                    "route_by_difficulty."
                )
            # Every concrete model in a chain must be priced and owned.
            for model in alias.chain:
                if model not in self._prices:
                    raise ConfigError(
                        f"Alias {alias.name!r} references model {model!r}, which has "
                        "no entry in the price table."
                    )
                self.provider_for_model(model)  # raises if unowned
            # A router's targets must be chain aliases: pointing a tier at
            # another router is how you get an infinite resolution loop.
            for difficulty, target in alias.route_by_difficulty.items():
                target_alias = self._aliases.get(target)
                if target_alias is None:
                    raise ConfigError(
                        f"Alias {alias.name!r} routes difficulty {difficulty!r} to "
                        f"{target!r}, which is not a configured alias."
                    )
                if target_alias.is_router:
                    raise ConfigError(
                        f"Alias {alias.name!r} routes difficulty {difficulty!r} to "
                        f"{target!r}, which is itself a router. Router targets must "
                        "be concrete alias chains."
                    )


def strip_comments(obj: Any) -> Any:
    """Drop the `_comment` keys the provided JSON uses for documentation."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if k != "_comment"}
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def load_prices(path: Path) -> dict[str, ModelPrice]:
    """Load the price table.

    Prices are read with `Decimal(str(value))`, not `Decimal(value)`. Passing a
    float to Decimal preserves the float's binary error (`Decimal(0.15)` is
    0.1499999999999999944488848768742172978818416595458984375); going via `str`
    gives the decimal literal the file actually contains.
    """
    raw = strip_comments(json.loads(path.read_text(encoding="utf-8")))
    prices: dict[str, ModelPrice] = {}
    for model, entry in raw.items():
        try:
            prices[model] = ModelPrice(
                model=model,
                input_per_1m=Decimal(str(entry["input_per_1m"])),
                output_per_1m=Decimal(str(entry["output_per_1m"])),
            )
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"Malformed price entry for {model!r}: {exc}") from exc
    return prices


def load_gateway_config(config_path: Path, pricing_path: Path) -> GatewayConfig:
    """Load and validate the gateway configuration from disk."""
    if not config_path.is_file():
        raise ConfigError(
            f"Gateway config not found at {config_path}. Copy the provided sample: "
            f"cp data/gateway_config.sample.json {config_path}"
        )
    if not pricing_path.is_file():
        raise ConfigError(f"Price table not found at {pricing_path}.")

    raw = strip_comments(json.loads(config_path.read_text(encoding="utf-8")))

    providers: dict[str, Provider] = {}
    for entry in raw.get("providers", []):
        try:
            provider = Provider(
                name=entry["name"], base_url=entry["base_url"], api_key=entry["api_key"]
            )
        except KeyError as exc:
            raise ConfigError(f"Provider entry missing field {exc}.") from exc
        if provider.name in providers:
            raise ConfigError(f"Duplicate provider {provider.name!r}.")
        providers[provider.name] = provider

    aliases: dict[str, ModelAlias] = {}
    for name, entry in raw.get("model_aliases", {}).items():
        aliases[name] = ModelAlias(
            name=name,
            primary=entry.get("primary"),
            fallbacks=tuple(entry.get("fallbacks", ())),
            route_by_difficulty=dict(entry.get("route_by_difficulty", {})),
        )

    retry_raw = raw.get("retry", {})
    retry = RetryPolicy(
        max_attempts=int(retry_raw.get("max_attempts", 3)),
        initial_backoff_ms=int(retry_raw.get("initial_backoff_ms", 200)),
        backoff_multiplier=float(retry_raw.get("backoff_multiplier", 2)),
    )

    deg_raw = raw.get("degradation", {})
    degradation = DegradationPolicy(
        error_rate_threshold=float(deg_raw.get("error_rate_threshold", 0.5)),
        window_seconds=int(deg_raw.get("window_seconds", 60)),
        p95_latency_ms=int(deg_raw.get("p95_latency_ms", 5000)),
    )

    return GatewayConfig(
        providers=providers,
        aliases=aliases,
        prices=load_prices(pricing_path),
        retry=retry,
        degradation=degradation,
    )
