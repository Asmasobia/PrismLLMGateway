"""Text → vector, behind a Protocol, with the model treated as a vendored artifact.

Two consumers share this module and that is deliberate: the difficulty router
(`prism/routing.py`) and the semantic cache both work in the **same embedding
space**. One model, one dimension, one set of measured cosines in
`docs/DESIGN_NOTES.md` — two embedding spaces would mean two sets of thresholds to
justify and two artifacts to ship.

**The model is never fetched on the request path.** It is present in
`PRISM_MODEL_CACHE` before the process starts, or startup fails naming the variable
to set. A gateway that reaches out to a model host mid-request has an unadvertised
dependency and an unbounded tail latency, and it fails in the environment where it
is least expected to. `HF_HUB_OFFLINE` is set here rather than only in `.env`, so
the invariant holds even when someone runs the app with a hand-rolled environment.

**Every embed call runs in a worker thread.** ONNX inference is CPU-bound and holds
the GIL, so embedding on the event loop stalls every other request in the process —
including the SSE relay, whose whole point is that tokens arrive as they are
produced (`docs/IMPLEMENTATION_GUIDE.md:96`). `asyncio.to_thread` releases the loop
for the duration. The cost is that throughput is bounded by the default executor,
which is recorded in the README's Known limitations rather than hidden.

`FakeEmbedder` is a second implementation, not a mock, for the same reason
`FakeProviderClient` is: unit tests and the no-database lane must run with neither
the 65 MB artifact nor a network. It is lexical rather than semantic, so it can
prove *plumbing* (scoping, thresholds, hit accounting) and deliberately cannot
prove *quality* — the tests that grade quality are the ones that load the real
model, and they say so.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("prism.embeddings")

#: The vendored model. `fastembed` resolves this name to the quantized ONNX
#: repository `qdrant/bge-small-en-v1.5-onnx-q`; the pinned revision and SHA are in
#: `docs/DESIGN_NOTES.md`.
MODEL_NAME = "BAAI/bge-small-en-v1.5"

#: Vectors are 384-dimensional and L2-normalised, so cosine similarity is a plain
#: dot product. `prism/db/models.py:EMBEDDING_DIM` must agree; a test asserts it.
EMBEDDING_DIM = 384

_TOKEN = re.compile(r"[a-z0-9']+")


@runtime_checkable
class Embedder(Protocol):
    """What the router and the cache need from an embedding model.

    A Protocol rather than an ABC because the real implementation wraps a
    third-party object and the fake wraps nothing: neither is naturally a subclass
    of the other, and structural typing says exactly what is required without
    forcing an inheritance relationship that means nothing at runtime.
    """

    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch. Batching matters: the exemplar set is embedded in one call."""
        ...

    async def embed_one(self, text: str) -> list[float]:
        ...

    async def warm(self) -> None:
        """Pay first-inference costs at startup. A no-op is a valid implementation."""
        ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, not assuming normalised inputs.

    Both implementations here return unit vectors, so this could be a dot product —
    and it is not, because the day an embedder returns un-normalised vectors the
    failure mode of a bare dot product is *silently inflated similarity*, i.e. false
    cache hits across tenants' prompts. Dividing by the norms costs two square roots
    per comparison and removes that class of bug. Zero vectors return 0.0 rather
    than raising, because an empty prompt is a request, not a crash.
    """
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


class FastEmbedEmbedder:
    """The real embedder: quantized ONNX `bge-small-en-v1.5`, served offline."""

    dimension = EMBEDDING_DIM

    def __init__(self, cache_dir: Path | str, *, model_name: str = MODEL_NAME) -> None:
        # Enforced here, not just documented in `.env`: the vendored-artifact rule is
        # a property of this gateway, and an env file is not present in every way the
        # app can be started. Never accompanied by an SSL workaround — if the model
        # is absent the correct outcome is a startup error, not a download over a
        # weakened connection.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

        from fastembed import TextEmbedding  # imported late: 65 MB of ONNX runtime

        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
        self.model_name = model_name

    def _embed_sync(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(list(texts))]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts)

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]

    async def warm(self) -> None:
        """Run one inference at startup so the first real request does not pay for it.

        The first `embed` call is where ONNX allocates its arenas and the tokenizer
        is built — hundreds of milliseconds, charged to whichever tenant happened to
        arrive first, and charged again after every deploy. Doing it in the lifespan
        moves that cost to a moment when nobody is waiting.
        """
        vector = await self.embed_one("warm")
        logger.info(
            "embedding model ready: %s (dim=%d, norm=%.4f)",
            self.model_name,
            len(vector),
            math.sqrt(sum(x * x for x in vector)),
        )


class FakeEmbedder:
    """A deterministic, lexical stand-in. Same interface, no artifact, no network.

    Tokens are hashed into a fixed number of buckets and the resulting bag-of-words
    vector is L2-normalised, so:

    * identical text scores exactly 1.0,
    * a paraphrase that reuses words scores high,
    * unrelated text scores near 0.

    That is enough to test *plumbing* — per-tenant scoping, threshold comparisons,
    hit accounting, the exact-match fast path — deterministically and in
    milliseconds. It is **not** semantic: `docs/DESIGN_NOTES.md` measures TF-IDF
    ranking the must-miss fixture pair *above* the must-hit pair, so a lexical
    matcher cannot reproduce the graded behaviour. Tests that grade quality load the
    real model and are marked `model`; tests that grade wiring use this.
    """

    dimension = EMBEDDING_DIM

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension
        self.calls: list[str] = []

    def _bucket(self, token: str) -> int:
        # sha1 rather than `hash()`: `hash()` is salted per process, so vectors would
        # differ between runs and every stored embedding would be worthless after a
        # restart. Same reason `prism/providers/fake.py` uses crc32 for its replies.
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self.dimension

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _TOKEN.findall(text.lower()):
            vector[self._bucket(token)] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0.0:
            # An empty or punctuation-only prompt. A zero vector would make cosine
            # undefined, so one fixed bucket carries it: two empty prompts are then
            # identical to each other and dissimilar to everything else, which is
            # the behaviour a cache should have.
            vector[0] = 1.0
            return vector
        return [x / norm for x in vector]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [self._vector(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def warm(self) -> None:
        return None


class MemoEmbedder:
    """An embedder that remembers, scoped to one request.

    Two consumers embed during a single request and they do not embed the same
    string: the router classifies the *extracted ask*
    (`prism/routing.py:extract_ask`) and the cache keys on the full prompt. Usually
    those differ; for a short prompt they are character-for-character identical,
    because extraction leaves short prompts alone. So neither "embed once and share"
    nor "embed in both places" is right — the first is wrong, the second wastes a
    CPU-bound call on every short prompt through `auto`.

    Memoising by text gets both: one call per distinct string, zero calls when
    nothing needs embedding (a `fast` request with caching off), and no consumer has
    to know whether the other already ran. It satisfies `Embedder`, so it can be
    passed anywhere the real one can.

    Deliberately unbounded, because its lifetime is one request and a request has at
    most a handful of distinct texts. Not thread-safe: within a request the awaits
    are ordered.
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._memo: dict[str, list[float]] = {}
        #: Texts that actually reached the underlying embedder, in order. Tests
        #: assert on its length to prove a request embedded once, or not at all.
        self.embedded: list[str] = []

    @property
    def dimension(self) -> int:
        return self._embedder.dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        missing = [text for text in dict.fromkeys(texts) if text not in self._memo]
        if missing:
            for text, vector in zip(missing, await self._embedder.embed(missing), strict=True):
                self._memo[text] = vector
            self.embedded.extend(missing)
        return [self._memo[text] for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def warm(self) -> None:
        """Delegates, so this class satisfies `Embedder` whole. Nothing warms a memo."""
        await self._embedder.warm()


__all__ = [
    "EMBEDDING_DIM",
    "MODEL_NAME",
    "Embedder",
    "FakeEmbedder",
    "FastEmbedEmbedder",
    "MemoEmbedder",
    "cosine",
]
