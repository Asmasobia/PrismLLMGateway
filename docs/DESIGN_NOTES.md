# Design notes

Measured facts and the decisions that follow from them. This file is the source of truth for the
numbers in the verification report — do not re-derive them ad hoc, and do not change one without
re-measuring and updating the row here.

Provenance of the measurements: taken 2026-09-13 against the vendored model described below.

**All six fixture cosines below were independently re-measured offline on 2026-09-13**
with `HF_HUB_OFFLINE=1`, reading prompts directly from `data/sample_requests.jsonl`. Every value
reproduced to four decimal places (max delta 0.0000), with `dim=384` and L2 norm `1.0000`. The
quantized ONNX model is therefore deterministic across processes here, which is what makes the
margin analysis below trustworthy.

---

## Embedding model — a vendored artifact

The gateway embeds text with **`BAAI/bge-small-en-v1.5`**, quantized ONNX, served through
`fastembed`. fastembed resolves that name to the `qdrant/bge-small-en-v1.5-onnx-q` repository.

| Property | Value |
|---|---|
| Cache location | `%LOCALAPPDATA%\prism-models` (machine-specific) |
| Size on disk | 65 MB |
| Pinned revision | `52398278842ec682c6f32300af41344b1c0b0bb2` |
| Embedding dimension | 384 |
| Vectors | L2-normalised (norm 1.0000), so cosine similarity is a dot product |

**The model is never fetched at request time.** It is treated as a vendored artifact: present on
disk before the gateway starts, or the gateway fails fast at startup. This is deliberate, not a
workaround — a gateway that reaches out to a model host on the request path has an unadvertised
dependency and an unbounded tail latency.

Load it as:

```python
TextEmbedding(
    model_name="BAAI/bge-small-en-v1.5",
    cache_dir=os.environ["PRISM_MODEL_CACHE"],
)
```

- `PRISM_MODEL_CACHE` lives in `.env`, which is gitignored because the path is machine-specific.
  `.env.example` carries a placeholder only.
- Verify with `HF_HUB_OFFLINE=1` set. Expected: loads in under a second, `dim=384`, L2 norm
  `1.0000`.
- **Warm the model once at startup.** ONNX inference is CPU-bound and holds the GIL, so every
  embed call must run inside `asyncio.to_thread` — otherwise it blocks the event loop and the
  token-by-token streaming pacing collapses under any concurrency.
- An `Embedder` **Protocol** sits in front of it, with a `FakeEmbedder` returning deterministic
  hash vectors. Unit tests and CI therefore need neither the model nor a network.
- `scripts/fetch_model.py` exists for reviewers: pinned revision plus a SHA256 check.

**Never** add `verify=False` or `HF_HUB_DISABLE_SSL_VERIFICATION` anywhere in this repository. If
the model is absent, the correct outcome is a clear startup error naming `PRISM_MODEL_CACHE`.

---

## Semantic cache — threshold and guards

Similarity threshold is **per tenant**, from `data/seed_keys.json`. The `search` key ships `0.92`
and `free-tier` ships `0.85`.

### Measured fixture cosines

Prompts are from `data/sample_requests.jsonl`.

| Pair | Cosine | Required behaviour |
|---|---|---|
| `req_cache_a1` / `req_cache_a2` | **0.9825** | must **hit** |
| `req_cache_b1` / `req_cache_b2` | **0.8693** | should hit |
| `req_cache_a1` / `req_cache_a3` | **0.8487** | must **miss** (2FA near-miss) |
| `req_cache_a2` / `req_cache_a3` | **0.8462** | must **miss** |
| `req_cache_a1` / `req_no_cache` | **0.4847** | must miss |
| `req_cache_b1` / `req_no_cache` | **0.6090** | must miss |

### Why 0.92 is the right threshold to demo on

A threshold satisfying *every* fixture must sit above `0.8487` (so `a3` misses) and at or below
`0.8693` (so pair B hits). That window is `(0.8487, 0.8693]` — about two hundredths wide.

The `free-tier` key's `0.85` falls inside that window, so it appears to satisfy all six fixtures.
**It is not a safe place to stand:** its margin over the must-miss `a1/a3` pair is `0.85 − 0.8487
= 0.0013`. That is well inside the range where quantized-ONNX numerical noise or a tokenizer
change could flip a required miss into a cross-topic false hit — the exact failure
`docs/EVALUATION_GUIDE.md` treats as a correctness failure.

At the `search` key's `0.92`:

- pair A hits (0.9825, margin 0.06)
- `a3` misses with a margin of 0.07
- pair B (0.8693) does **not** hit, and is a documented WARN

`scripts/smoke_test.py` scores each paraphrase pair as WARN individually but fails hard if
*neither* pair hits. Pair A hits with room to spare, so the hard check passes. Trading pair B down
to a WARN in exchange for a 0.07 miss margin is the right trade, and it is the reason the cache
demo runs on `prism-sk-search-1a2b3c` rather than the free-tier key.

Secondary reason for the same choice: the free-tier key is limited to 10 rpm and `smoke_test.py`
fires roughly nine requests on the key under test. One retry would produce a spurious 429.

### Two guards, applied before the threshold

Cosine similarity alone is not sufficient, and this is load-bearing rather than defensive:

| Guard | Rule | Evidence it is needed |
|---|---|---|
| **Literal** | numbers, IDs, currency amounts, dates and quoted strings must match exactly | `"100 USD"` vs `"200 USD"` measures **0.9056** — above 0.92 is within reach, and the answers differ completely |
| **Polarity** | both prompts must have the same negation profile | a negation pair measures **0.9292**, which clears 0.92 outright |

Both guards only ever *reject* a candidate; neither can admit one. So they cannot cause a false
hit, and lowering the threshold does not weaken them.

---

## Lexical matching cannot substitute for embeddings

Measured, so it does not have to be re-argued:

| Method | MUST-HIT pair | MUST-MISS pair | Verdict |
|---|---|---|---|
| TF-IDF, word | 0.1986 | 0.4749 | **ordering inverted** |
| TF-IDF, char 3–5 grams | 0.2168 | 0.5006 | **ordering inverted** |

In both cases the pair that must miss scores *higher* than the pair that must hit, so **no
threshold exists** that satisfies the fixtures. Bag-of-words is listed among the pack's acceptable
simplifications, but it cannot produce the seeded behaviour. The embedding model is not a
nice-to-have.

---

## Difficulty router

**Method: kNN over 30–50 self-authored labelled exemplars**, in the same embedding space as the
cache.

Two rules that are about honesty, not accuracy:

- **Never read `expected_tier` in routing logic.** `docs/DATA_MODEL.md:11` says the labels are not
  routing inputs.
- **Never tune against `data/routing_eval.jsonl`.** It is the answer key. `docs/EVALUATION_GUIDE.md:82`
  notes a length-only heuristic scores ~60% by design; the commonly-found 75% length threshold is
  only reachable by fitting to the eval, which is measuring yourself against your own answers.

To keep that second rule operational rather than aspirational: exemplars are authored to cover the
*shapes* of the hard cases — short-but-hard and long-but-trivial — reasoned about from first
principles, not copied from the eval. Iterate against a small self-authored dev set, and score
`routing_eval.jsonl` as a held-out set. `docs/EVALUATION_GUIDE.md:83` sets the bar: "a documented
method reaching 80%+ is a solid baseline." Eight of the twenty cases are deliberate traps
(`route_011/012/013/019/020` short-but-hard, `route_006/007/010` long-but-trivial), so clearing 80%
means getting most traps right — length alone cannot do it.

---

## Caveats to carry into Known limitations

- `scripts/fetch_model.py` is written for reviewers but **has never been exercised end to end**:
  the model was vendored out of band, so it ships unverified against a live host.
- Cache pair B (`req_cache_b1`/`b2`) is a permanent WARN at the demoed threshold of 0.92, by the
  deliberate trade documented above.
- Embedding is CPU-bound and single-process; throughput under concurrency is bounded by
  `asyncio.to_thread`'s default executor.
