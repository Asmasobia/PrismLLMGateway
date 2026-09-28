"""The embedding layer: similarity arithmetic, the fake, the memo, and the artifact.

Split into two lanes on purpose. Everything here runs in milliseconds with no
artifact and no network except the handful of tests marked `model`, which load the
vendored ONNX model and are the only ones that can say anything about *quality*.
The unmarked tests prove arithmetic and plumbing, which is all a lexical stand-in
can prove — see the `FakeEmbedder` docstring.
"""

from __future__ import annotations

import math

import pytest

from prism.db.models import EMBEDDING_DIM as SCHEMA_DIM
from prism.embeddings import (
    EMBEDDING_DIM,
    Embedder,
    FakeEmbedder,
    MemoEmbedder,
    cosine,
)


def test_the_schema_and_the_model_agree_on_the_dimension() -> None:
    """Two constants, one truth.

    `prism/db/models.py` sizes the stored vector and `prism/embeddings.py` produces
    it. If they drift, every row written after the drift is silently uncomparable
    with every row written before it, and nothing fails at write time because the
    column is a Postgres array with no declared length.
    """
    assert EMBEDDING_DIM == SCHEMA_DIM == 384


def test_cosine_of_a_vector_with_itself_is_one() -> None:
    assert cosine([0.3, 0.4, 0.5], [0.3, 0.4, 0.5]) == pytest.approx(1.0)


def test_cosine_ignores_magnitude() -> None:
    """The property that makes a bare dot product dangerous.

    A dot product over un-normalised vectors grows with length, so a long prompt
    would score as *more similar* to everything — false cache hits, in the direction
    of serving one caller another caller's answer.
    """
    assert cosine([1.0, 0.0], [7.0, 0.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_with_a_zero_vector_is_zero_not_an_exception() -> None:
    """An empty prompt is a request, not a crash."""
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_rejects_a_dimension_mismatch() -> None:
    """Comparing a 384-vector with a 768-vector is a bug, not a low score."""
    with pytest.raises(ValueError):
        cosine([1.0, 0.0], [1.0, 0.0, 0.0])


async def test_the_fake_embedder_satisfies_the_protocol() -> None:
    assert isinstance(FakeEmbedder(), Embedder)


async def test_the_fake_embedder_returns_unit_vectors_of_the_right_width() -> None:
    vector = await FakeEmbedder().embed_one("a handful of ordinary words")
    assert len(vector) == EMBEDDING_DIM
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0)


async def test_the_fake_embedder_is_stable_across_instances() -> None:
    """Two instances must agree, or nothing built on it is reproducible."""
    first = await FakeEmbedder().embed_one("stable across instances")
    second = await FakeEmbedder().embed_one("stable across instances")
    assert first == second


def test_the_fake_embedder_bucket_is_not_process_salted() -> None:
    """The reason it hashes with sha1 rather than `hash()`.

    `hash()` is salted per process, so a vector stored before a restart would not
    match the same text embedded after one: every stored embedding in the database
    would quietly become garbage, and the symptom would be a cache that stops
    hitting for no visible reason. This asserts a fixed bucket, which only holds for
    a stable hash.
    """
    assert FakeEmbedder()._bucket("prism") == 110


async def test_the_fake_embedder_scores_a_reused_wording_above_an_unrelated_one() -> None:
    """Enough signal to test thresholds with, and no more than that."""
    embedder = FakeEmbedder()
    base = await embedder.embed_one("how do I restart the ingest worker")
    close = await embedder.embed_one("how do I restart the worker")
    far = await embedder.embed_one("translate this menu into Greek")
    assert cosine(base, close) > cosine(base, far)


async def test_an_empty_prompt_embeds_without_dividing_by_zero() -> None:
    embedder = FakeEmbedder()
    blank = await embedder.embed_one("")
    punctuation = await embedder.embed_one("!!! ...")
    assert cosine(blank, punctuation) == pytest.approx(1.0)
    assert cosine(blank, await embedder.embed_one("real words here")) == pytest.approx(0.0)


async def test_the_memo_embeds_each_distinct_text_once() -> None:
    inner = FakeEmbedder()
    memo = MemoEmbedder(inner)

    first = await memo.embed_one("what is a queue?")
    again = await memo.embed_one("what is a queue?")

    assert first == again
    assert memo.embedded == ["what is a queue?"]
    # The important half: the underlying model was called once, not twice.
    assert inner.calls == ["what is a queue?"]


async def test_the_memo_costs_nothing_when_nobody_embeds() -> None:
    """A `fast` request with caching off must not touch the model at all."""
    inner = FakeEmbedder()
    MemoEmbedder(inner)
    assert inner.calls == []


async def test_the_memo_deduplicates_within_a_batch() -> None:
    inner = FakeEmbedder()
    memo = MemoEmbedder(inner)

    vectors = await memo.embed(["a", "b", "a"])

    assert len(vectors) == 3
    assert vectors[0] == vectors[2]
    assert inner.calls == ["a", "b"]


async def test_the_memo_reports_the_underlying_dimension() -> None:
    assert MemoEmbedder(FakeEmbedder()).dimension == EMBEDDING_DIM


# --------------------------------------------------------------------------
# The vendored artifact. Marked `model`: needs PRISM_MODEL_CACHE populated.
# --------------------------------------------------------------------------

@pytest.mark.model
async def test_the_real_model_loads_offline_and_returns_normalised_vectors(
    real_embedder,
) -> None:
    """Three claims in `docs/DESIGN_NOTES.md`, checked rather than asserted in prose.

    That the model loads from the local cache with no network (the fixture sets
    `HF_HUB_OFFLINE`, and a fetch attempt would fail rather than succeed quietly),
    that its width is the 384 the schema was sized for, and that its output is
    L2-normalised — which is what makes the measured cosine thresholds comparable
    across runs.
    """
    vector = await real_embedder.embed_one("does this load without a network?")
    assert len(vector) == EMBEDDING_DIM
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0, abs=1e-3)


@pytest.mark.model
async def test_the_real_model_ranks_a_paraphrase_above_an_unrelated_prompt(
    real_embedder,
) -> None:
    """The one thing the fake cannot do, stated as a test rather than a claim.

    `docs/DESIGN_NOTES.md` records that a lexical matcher ranks the must-miss pair
    *above* the must-hit pair, so this ordering is the reason the artifact is worth
    shipping at all.
    """
    paraphrase_a = await real_embedder.embed_one("How do I reset my password?")
    paraphrase_b = await real_embedder.embed_one("What's the process for changing my password?")
    unrelated = await real_embedder.embed_one("What is the capital of Peru?")

    assert cosine(paraphrase_a, paraphrase_b) > cosine(paraphrase_a, unrelated)
    assert cosine(paraphrase_a, paraphrase_b) > 0.85


@pytest.mark.model
async def test_batching_and_single_embedding_agree(real_embedder) -> None:
    """Batched and one-at-a-time must produce the same vectors.

    They go through different code paths in `fastembed`, and a difference here would
    mean the exemplar set (embedded as a batch at startup) lived in a subtly
    different space from the prompts it is compared against.
    """
    texts = ["first prompt", "second prompt"]
    batched = await real_embedder.embed(texts)
    singly = [await real_embedder.embed_one(text) for text in texts]
    for left, right in zip(batched, singly, strict=True):
        assert cosine(left, right) == pytest.approx(1.0, abs=1e-6)
