"""The difficulty classifier: ask extraction, the kNN vote, and the exemplar set.

Three lanes, and the split is the point:

* **String work** — `extract_ask` and the score-to-label mapping. No embedder.
* **Plumbing** — the semantic classifier driven by `FakeEmbedder`: that it returns a
  label from the config's vocabulary, that it embeds the exemplars once rather than
  per request, that its reason names its method and contains no prompt text. A
  lexical stand-in cannot say anything about routing *quality*, so these tests do not
  pretend to.
* **Quality** — marked `model`, and scored against `tests/data/router_dev_set.jsonl`
  only. `data/routing_eval.jsonl` is deliberately **not** asserted on here: it is
  scored once by `scripts/routing_eval.py` as a held-out set, and a test with a floor
  on it would turn every future exemplar edit into an exercise in fitting the answer
  key (`docs/DESIGN_NOTES.md`).
"""

from __future__ import annotations

import json

import pytest

from prism.config import GatewayConfig
from prism.embeddings import FakeEmbedder, cosine
from prism.router_exemplars import EXEMPLARS, shapes
from prism.routing import (
    MAX_ASK_WORDS,
    LengthClassifier,
    SemanticClassifier,
    _label_for_score,
    extract_ask,
    resolve,
)
from tests.conftest import ROOT

DEV_SET = ROOT / "tests" / "data" / "router_dev_set.jsonl"
HELD_OUT = ROOT / "data" / "routing_eval.jsonl"


def user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


# --------------------------------------------------------------------------
# Ask extraction. No embedder involved: this is string work.
# --------------------------------------------------------------------------


def test_a_short_prompt_is_classified_whole() -> None:
    """Nothing to strip, and stripping risks deleting the ask itself."""
    ask = extract_ask("Prove that the square root of two is irrational.")
    assert ask.text == "Prove that the square root of two is irrational."
    assert ask.dropped == ()
    assert ask.words == ask.full_words


def test_a_pasted_payload_is_dropped_and_the_question_survives() -> None:
    """The long-but-trivial trap, reduced to the thing that decides the tier."""
    prompt = (
        "Here are the response times we recorded, in milliseconds: "
        + ", ".join(str(120 + i) for i in range(40))
        + ". Which value is the largest?"
    )
    ask = extract_ask(prompt)
    assert ask.text == "Which value is the largest?"
    assert "payload" in ask.dropped
    assert ask.words < ask.full_words


def test_a_long_quoted_block_is_dropped_and_the_instruction_survives() -> None:
    prompt = (
        "Rewrite the paragraph below so that it fits in one sentence: "
        "'We are postponing the release because the migration rehearsal found two "
        "problems that we would rather fix before customers see them, and the team "
        "would like another day to test the fix properly.'"
    )
    ask = extract_ask(prompt)
    assert "quote" in ask.dropped
    assert ask.text.startswith("Rewrite the paragraph below")
    assert "postponing" not in ask.text


def test_an_apostrophe_does_not_open_a_quotation() -> None:
    """A possessive must not swallow everything up to the next apostrophe.

    Without the lookarounds in `_QUOTED` this deletes the middle of the prompt, and
    on a bad day the question at the end of it.
    """
    prompt = (
        "Here are last week's totals and the week before's totals for every region "
        "we operate in, covering thirty separate lines of figures that nobody needs "
        "to read in full for this particular question. Which region grew fastest?"
    )
    ask = extract_ask(prompt)
    assert "quote" not in ask.dropped
    assert ask.text == "Which region grew fastest?"


def test_an_instruction_after_a_comma_is_recognised() -> None:
    """In "Given X, work out Y" the ask does not start its sentence."""
    prompt = (
        "Our configuration retries every status code three times with a 200 ms "
        "backoff that doubles, and two providers sit behind one alias. "
        "Given a provider failing at half of all requests, work out what that does "
        "to tail latency."
    )
    assert "work out what that does" in extract_ask(prompt).text


def test_a_prompt_with_no_recognisable_ask_falls_back_to_the_whole_thing() -> None:
    """Dropping a real ask is the expensive mistake; classifying payload is the cheap one."""
    prompt = " ".join(f"line{i} value{i}" for i in range(40))
    ask = extract_ask(prompt)
    assert "no-cue" in ask.dropped
    assert ask.text.startswith("line0")


def test_payload_with_no_sentence_boundaries_is_clipped_rather_than_dropped() -> None:
    """A known limitation, pinned so it is a decision and not a surprise.

    `_SENTENCE` splits on terminal punctuation and newlines. Payload carrying neither
    — a space-joined run of tokens with the question tacked on the end — is one
    "sentence" that contains the ask, so it is kept whole and only the clip saves it.
    The clip keeps head *and* tail, so the question at the end still reaches the
    classifier; that is why this degrades rather than breaks. Real pasted payload
    (logs, tables, stack traces) is newline-separated and splits correctly — see
    `test_a_pasted_payload_is_dropped_and_the_question_survives`.
    """
    prompt = "Here is the log: " + " ".join(f"line{i}" for i in range(60)) + " How many lines?"
    ask = extract_ask(prompt)
    assert ask.dropped == ("clipped",)
    assert "payload" not in ask.dropped
    # Degraded, not broken: the ask still survives at the tail.
    assert ask.text.endswith("How many lines?")


def test_a_very_long_ask_is_clipped_from_the_middle() -> None:
    """Head and tail survive, because that is where instructions live."""
    prompt = "Explain the following. " + " ".join(f"w{i}" for i in range(300)) + " Why?"
    ask = extract_ask(prompt)
    assert "clipped" in ask.dropped
    assert ask.words <= MAX_ASK_WORDS + 1  # the ellipsis counts as a word
    assert ask.text.startswith("Explain the following.")
    assert ask.text.endswith("Why?")


# --------------------------------------------------------------------------
# Score to label. The only place that knows how many tiers a deployment has.
# --------------------------------------------------------------------------


def test_two_labels_split_at_the_midpoint() -> None:
    labels = ["simple", "complex"]
    assert _label_for_score(0.49, labels)[0] == "simple"
    assert _label_for_score(0.51, labels)[0] == "complex"


def test_three_labels_split_into_thirds() -> None:
    """A deployment with its own vocabulary is mapped, not rejected."""
    labels = ["simple", "medium", "complex"]
    assert _label_for_score(0.1, labels)[0] == "simple"
    assert _label_for_score(0.5, labels)[0] == "medium"
    assert _label_for_score(0.9, labels)[0] == "complex"


def test_the_extremes_of_the_score_range_stay_in_range() -> None:
    labels = ["simple", "complex"]
    assert _label_for_score(0.0, labels)[0] == "simple"
    assert _label_for_score(1.0, labels)[0] == "complex"


def test_the_margin_measures_distance_to_the_decision_boundary() -> None:
    """What separates "obviously hard" from "0.51, could have gone either way"."""
    labels = ["simple", "complex"]
    assert _label_for_score(0.5, labels)[1] == pytest.approx(0.0)
    assert _label_for_score(1.0, labels)[1] == pytest.approx(0.5)


# --------------------------------------------------------------------------
# The semantic classifier: plumbing, with a lexical stand-in.
# --------------------------------------------------------------------------


async def test_the_semantic_classifier_returns_a_label_from_the_vocabulary() -> None:
    label, _ = await SemanticClassifier(FakeEmbedder()).classify(
        "what is a queue?", ["simple", "complex"]
    )
    assert label in {"simple", "complex"}


async def test_the_exemplars_are_embedded_once_no_matter_how_many_requests() -> None:
    """Fifty-odd embeddings at startup, not fifty-odd per request."""
    embedder = FakeEmbedder()
    classifier = SemanticClassifier(embedder)
    await classifier.prepare()
    assert len(embedder.calls) == len(EXEMPLARS)

    await classifier.classify("a prompt", ["simple", "complex"])
    await classifier.classify("another prompt", ["simple", "complex"])
    # Two more calls, one per prompt — not two more exemplar sets.
    assert len(embedder.calls) == len(EXEMPLARS) + 2


async def test_preparing_twice_does_not_embed_twice() -> None:
    embedder = FakeEmbedder()
    classifier = SemanticClassifier(embedder)
    await classifier.prepare()
    await classifier.prepare()
    assert len(embedder.calls) == len(EXEMPLARS)


async def test_binding_shares_the_prepared_vectors() -> None:
    """The per-request memo must not re-embed the exemplar set."""
    classifier = SemanticClassifier(FakeEmbedder())
    await classifier.prepare()

    per_request = FakeEmbedder()
    await classifier.bind(per_request).classify("what is a queue?", ["simple", "complex"])

    assert per_request.calls == ["what is a queue?"]


async def test_a_bound_length_classifier_is_itself() -> None:
    baseline = LengthClassifier()
    assert baseline.bind(FakeEmbedder()) is baseline


async def test_the_reason_names_the_method_and_carries_no_prompt_text() -> None:
    """`route_reason` is written to `request_log`.

    An audit trail that quotes prompts is a second copy of the data the gateway
    promised not to keep, and it is the field an operator pastes into a ticket.
    """
    prompt = "Explain how zephyrquux tokens are reconciled at month end."
    _, reason = await SemanticClassifier(FakeEmbedder()).classify(
        prompt, ["simple", "complex"]
    )
    assert "knn=exemplars" in reason
    assert "zephyrquux" not in reason


async def test_the_reason_records_how_much_of_the_prompt_was_classified() -> None:
    """Ask extraction has to be visible, or a surprising tier cannot be explained."""
    prompt = (
        "Here is the log:\n"
        + "\n".join(f"2026-09-14T10:0{i % 10}:00Z worker ready line{i}" for i in range(30))
        + "\nHow many lines are there?"
    )
    _, reason = await SemanticClassifier(FakeEmbedder()).classify(
        prompt, ["simple", "complex"]
    )
    assert "dropped=payload" in reason
    assert f"/{len(prompt.split())}w" in reason


async def test_an_empty_exemplar_set_degrades_to_the_baseline() -> None:
    """A configuration mistake is not a reason to refuse the request."""
    classifier = SemanticClassifier(FakeEmbedder(), ())
    label, reason = await classifier.classify("word " * 100, ["simple", "complex"])
    assert label == "complex"
    assert "no exemplars" in reason


async def test_the_router_reports_the_semantic_method_in_the_route_reason(
    config: GatewayConfig,
) -> None:
    """End to end through `resolve`, so the wiring is proved rather than assumed."""
    route = await resolve(
        config, "auto", user("what is a queue?"), SemanticClassifier(FakeEmbedder())
    )
    assert route.tier in {"fast", "smart"}
    assert "knn=exemplars" in route.reason


# --------------------------------------------------------------------------
# The exemplar set itself. Structure, not quality — no model needed.
# --------------------------------------------------------------------------


def test_every_exemplar_is_labelled_at_an_end_of_the_scale() -> None:
    """Difficulty is a float so the *config* can have any number of tiers.

    The exemplars themselves stay at 0.0 and 1.0: a hand-written 0.6 would be a
    number nobody could defend, and the weighted vote is what produces the
    intermediate values.
    """
    assert {exemplar.difficulty for exemplar in EXEMPLARS} == {0.0, 1.0}


def test_the_two_difficulty_classes_are_balanced() -> None:
    """An imbalanced set biases every kNN vote toward the larger class."""
    easy = sum(1 for exemplar in EXEMPLARS if exemplar.difficulty == 0.0)
    assert abs(easy - (len(EXEMPLARS) - easy)) <= 2


def test_no_exemplar_repeats_another() -> None:
    assert len({exemplar.text for exemplar in EXEMPLARS}) == len(EXEMPLARS)


def test_every_exemplar_carries_a_shape_tag() -> None:
    """The tags are what `route_reason` says instead of quoting the prompt."""
    assert all(exemplar.shape for exemplar in EXEMPLARS)
    assert sum(shapes().values()) == len(EXEMPLARS)


#: Topics that must appear at both ends of the scale, each as the set of surface
#: forms that count. A topic is a *subject*, not a token: the container topic is
#: written "docker run" on the easy side and "containers" on the hard side, and
#: requiring one shared word would be asserting a lexical proxy for a property that
#: lives in the embedding space. Measured, so this is not an appeal to intuition:
#: that cross-difficulty pair scores 0.5817, against 0.5624 for the postgres pair
#: and 0.6402 for the kafka pair — both of which *do* share a token — and 0.2795
#: for an unrelated pair. See `test_paired_topics_are_close_in_embedding_space`,
#: which enforces the rule where it actually applies.
TOPIC_SURFACE_FORMS = (
    ("postgres", ("postgres",)),
    ("containers", ("docker", "container")),
    ("kafka", ("kafka",)),
    ("python", ("python",)),
    ("indexes", ("index",)),
    ("primes", ("prime",)),
)


def test_topics_appear_on_both_sides_of_the_scale() -> None:
    """Rule 1 of the exemplar set, enforced rather than merely documented.

    Embeddings are dominated by subject matter, so a topic appearing only among the
    hard exemplars teaches the router that the *topic* is hard — exactly the bug the
    dev set caught with "what is a database index?".
    """
    easy = " ".join(e.text.lower() for e in EXEMPLARS if e.difficulty == 0.0)
    hard = " ".join(e.text.lower() for e in EXEMPLARS if e.difficulty == 1.0)
    for topic, forms in TOPIC_SURFACE_FORMS:
        assert any(f in easy for f in forms), f"{topic} is missing from the easy exemplars"
        assert any(f in hard for f in forms), f"{topic} is missing from the hard exemplars"


def test_no_exemplar_is_lifted_from_the_held_out_eval() -> None:
    """The exemplars are authored, not copied. This is the check that keeps it true.

    Reads the eval's *prompts* — never `expected_tier` — and asserts that no exemplar
    reproduces one. A verbatim overlap would make the reported held-out accuracy
    meaningless, and it is the kind of thing that creeps in during a late edit.
    """
    prompts = {
        json.loads(line)["prompt"].strip().lower()
        for line in HELD_OUT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    for exemplar in EXEMPLARS:
        assert exemplar.text.strip().lower() not in prompts


# --------------------------------------------------------------------------
# Quality. Needs the vendored model; scored against my own dev set only.
# --------------------------------------------------------------------------


def _dev_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in DEV_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.model
async def test_the_semantic_router_beats_the_length_baseline_on_the_dev_set(
    config: GatewayConfig, real_embedder
) -> None:
    """The claim the slice exists to make, on the set it is allowed to be tuned on.

    A floor rather than an exact score: an exemplar edit that improves the held-out
    number should not also have to edit a constant here, but a regression of more
    than one case should fail the suite.
    """
    semantic = SemanticClassifier(real_embedder)
    await semantic.prepare()
    cases = _dev_cases()

    scores = {}
    for name, classifier in (("semantic", semantic), ("length", LengthClassifier())):
        correct = 0
        for case in cases:
            route = await resolve(config, "auto", user(case["prompt"]), classifier)
            correct += route.tier == case["expected_tier"]
        scores[name] = correct / len(cases)

    assert scores["semantic"] >= 0.9, scores
    assert scores["semantic"] > scores["length"], scores


@pytest.mark.model
async def test_the_two_trap_shapes_route_correctly(
    config: GatewayConfig, real_embedder
) -> None:
    """The two ways length lies, as one test.

    Both prompts are authored here rather than lifted from either dataset, so the
    test still means something if the dev set changes.
    """
    semantic = SemanticClassifier(real_embedder)
    await semantic.prepare()

    short_but_hard = "Prove that a graph with every vertex of even degree has an Euler circuit."
    long_but_trivial = (
        "Below is the guest list for the retirement party: "
        + ", ".join(f"{name} plus one" for name in ("Ana", "Bo", "Cy", "Di", "Eve", "Fay"))
        + ", and then Gus plus one, Hal plus one, Ivy plus one, Jo plus one, Kai plus "
        "one, Lou plus one, Max plus one and Nan plus one. "
        "How many names are on the list?"
    )

    hard = await resolve(config, "auto", user(short_but_hard), semantic)
    easy = await resolve(config, "auto", user(long_but_trivial), semantic)

    assert hard.tier == "smart", hard.reason
    assert easy.tier == "fast", easy.reason


@pytest.mark.model
async def test_paired_topics_are_close_in_embedding_space(real_embedder) -> None:
    """Rule 1 again, this time where it actually applies.

    `test_topics_appear_on_both_sides_of_the_scale` can only check spelling. What the
    rule needs is that the easy and hard exemplar for a topic sit near each other, so
    the topic itself carries no difficulty signal — and that holds whether or not the
    two share a word. Asserted as a band against an unrelated pair rather than as a
    fixed constant: the useful claim is "clearly the same subject", not "0.58".
    """
    unrelated = cosine(
        await real_embedder.embed_one("What is the capital of Peru?"),
        await real_embedder.embed_one("Prove that the square root of two is irrational."),
    )

    for topic, forms in TOPIC_SURFACE_FORMS:
        easy = next(
            e for e in EXEMPLARS if e.difficulty == 0.0 and any(f in e.text.lower() for f in forms)
        )
        hard = next(
            e for e in EXEMPLARS if e.difficulty == 1.0 and any(f in e.text.lower() for f in forms)
        )
        similarity = cosine(
            await real_embedder.embed_one(easy.text),
            await real_embedder.embed_one(hard.text),
        )
        assert similarity > unrelated + 0.2, f"{topic}: {similarity:.4f} vs {unrelated:.4f}"


@pytest.mark.model
async def test_the_nearest_shapes_are_ones_a_human_would_pick(real_embedder) -> None:
    """Sanity on the explanation, not just the verdict.

    `route_reason` names the shapes that voted, and an operator will read them as an
    explanation. If a rollback plan's nearest neighbours were `translation` the tier
    might still be right, but the reason would be noise.
    """
    classifier = SemanticClassifier(real_embedder)
    await classifier.prepare()
    _, reason = await classifier.classify(
        "Draw up a plan to move our billing tables to a new cluster without downtime, "
        "and say how you would undo each step.",
        ["simple", "complex"],
    )
    assert "plan-rollback" in reason
