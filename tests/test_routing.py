"""Resolution: aliases, chains, and the structure of the router.

The behaviour worth pinning is not "does `fast` resolve" — it is that the router
resolves *through the same path* as a plain alias, so `auto` cannot lose failover
while `fast` keeps it. `test_a_router_inherits_the_full_fallback_chain` is that test.

Structure only, and no embedder: everything here runs on the length baseline. The
classifiers, ask extraction and the exemplar set are in
`tests/test_router_semantic.py`, which is where the `model` lane lives.
"""

from __future__ import annotations

import json

import pytest

from prism.config import GatewayConfig, load_gateway_config, load_prices
from prism.errors import NotFoundError
from prism.routing import (
    LENGTH_THRESHOLD_WORDS,
    classify_difficulty,
    last_user_text,
    resolve,
)
from tests.conftest import PRICING, SAMPLE_CONFIG


def user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


async def test_a_concrete_model_resolves_to_a_chain_of_one(config: GatewayConfig) -> None:
    route = await resolve(config, "beta-large", user("hi"))
    assert [t.label for t in route.chain] == ["beta/beta-large"]
    # No decision was made, so there is nothing to record.
    assert route.reason is None
    assert route.tier is None


async def test_an_alias_resolves_to_primary_then_fallbacks_in_order(config: GatewayConfig) -> None:
    route = await resolve(config, "fast", user("hi"))
    assert [t.label for t in route.chain] == ["alpha/alpha-small", "beta/beta-small"]
    assert route.primary.label == "alpha/alpha-small"


async def test_a_router_inherits_the_full_fallback_chain(config: GatewayConfig) -> None:
    """The reason `auto` goes through `resolve` instead of having its own path.

    A router that only returned its tier's primary would work in every demo and lose
    failover silently — `auto` traffic would 502 where `fast` traffic would not.
    """
    short = await resolve(config, "auto", user("What is a queue?"))
    assert short.tier == "fast"
    fast = await resolve(config, "fast", [])
    assert [t.label for t in short.chain] == [t.label for t in fast.chain]


async def test_a_router_records_the_decision_and_the_reason(config: GatewayConfig) -> None:
    """`docs/DATA_MODEL.md:75` requires the chosen tier *and* why, for `auto` requests."""
    route = await resolve(config, "auto", user("word " * (LENGTH_THRESHOLD_WORDS + 5)))
    assert route.tier == "smart"
    assert [t.label for t in route.chain] == ["alpha/alpha-large", "beta/beta-large"]
    assert "difficulty=complex" in route.reason
    # The reason names the method, so nobody reads more insight into it than exists.
    assert "heuristic=length" in route.reason


def test_the_baseline_classifier_splits_on_length_only(config: GatewayConfig) -> None:
    labels = ["simple", "complex"]
    assert classify_difficulty("short question", labels)[0] == "simple"
    assert classify_difficulty("word " * 100, labels)[0] == "complex"


def test_unknown_difficulty_labels_still_route() -> None:
    """A deployment may use its own vocabulary; unknown labels sort last, not crash."""
    label, _ = classify_difficulty("word " * 100, ["banana", "apple"])
    assert label in {"apple", "banana"}


def test_classification_reads_the_last_user_turn_only() -> None:
    """A trivial follow-up in a long thread is trivial.

    Summing the whole conversation would classify "yes?" as hard purely because the
    thread is long, and would get *more* wrong the longer a chat ran.
    """
    messages = [
        {"role": "user", "content": "word " * 200},
        {"role": "assistant", "content": "a long answer " * 50},
        {"role": "user", "content": "thanks, and why?"},
    ]
    assert last_user_text(messages) == "thanks, and why?"
    assert classify_difficulty(last_user_text(messages), ["simple", "complex"])[0] == "simple"


def test_multimodal_content_yields_its_text_parts() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this image"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    ]
    assert last_user_text(messages) == "what is in this image"


def test_no_user_message_classifies_rather_than_crashing() -> None:
    assert last_user_text([{"role": "system", "content": "be terse"}]) == ""
    assert last_user_text(None) == ""


async def test_an_unknown_target_is_a_404(config: GatewayConfig) -> None:
    with pytest.raises(NotFoundError):
        await resolve(config, "no-such-model", user("hi"))


async def test_an_unpriced_model_cannot_reach_an_upstream(tmp_path) -> None:
    """A model with no price is unbillable, so it must not be servable.

    Serving it would put a $0 request into the accounting that the evaluation
    reconciles — the cheapest possible way to make every total wrong.
    """
    prices = load_prices(PRICING)
    config = GatewayConfig(
        providers={"alpha": load_gateway_config(SAMPLE_CONFIG, PRICING).provider("alpha")},
        aliases={},
        prices={k: v for k, v in prices.items() if k != "alpha-large"},
        retry=load_gateway_config(SAMPLE_CONFIG, PRICING).retry,
        degradation=load_gateway_config(SAMPLE_CONFIG, PRICING).degradation,
    )
    with pytest.raises(NotFoundError):
        await resolve(config, "alpha-large", user("hi"))


def test_a_router_target_that_is_not_an_alias_is_rejected_at_load_time(tmp_path) -> None:
    """Belt and braces: config validation catches it, so resolution never sees it."""
    raw = json.loads(SAMPLE_CONFIG.read_text(encoding="utf-8"))
    raw["model_aliases"]["auto"]["route_by_difficulty"]["simple"] = "nonexistent"
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(Exception, match="not a configured alias"):
        load_gateway_config(path, PRICING)
