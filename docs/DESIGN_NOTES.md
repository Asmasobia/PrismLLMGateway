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
| Embed latency, warm | short prompt: **median 3.8 ms**, range 3.5–5.3 ms over 20 single-text calls |

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
- `scripts/fetch_model.py` exists for reviewers: pinned revision plus a SHA256 check. Run bare it
  verifies the cache offline; `--fetch` downloads first. The digests were computed from the vendored
  copy, so verification is checking the bytes the numbers below were measured with.

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

### Two guards, applied after the threshold

Cosine similarity alone is not sufficient, and this is load-bearing rather than defensive:

| Guard | Rule | Measured evidence |
|---|---|---|
| **Literal** | numbers, IDs, currency amounts, month names and quoted strings must match as a *multiset* | the amount pair below: **0.9149** |
| **Polarity** | both prompts must carry the same negation profile | the negation pair below: **0.9620** |

Both pairs are named and reproducible, because a threshold argument built on unattributed numbers
cannot be re-checked after a model change:

| Pair | Prompts | Cosine |
|---|---|---|
| Negation | `Can I cancel my annual plan?` / `Can I **not** cancel my annual plan?` | **0.9620** |
| Amount | `What is the refund on a 100 USD charge?` / `…on a 200 USD charge?` | **0.9149** |

Read the second number honestly: **0.9149 does not clear the demoed 0.92**, so at the `search` key
the threshold would already have rejected that pair on its own. It *does* clear the `free-tier`
key's 0.85 comfortably, and that is the tenant where the literal guard is load-bearing — a
deployment that lowers its threshold to catch more paraphrases is exactly the one that starts
confusing a $100 refund with a $200 one. The negation pair, at 0.9620, clears **both** thresholds
and would be a false hit at any setting a paraphrase cache can usefully run at.

Both guards only ever *reject* a candidate; neither can admit one. So they cannot cause a false
hit, and lowering the threshold does not weaken them — which is the property that makes them worth
having at 0.92 even though only one of the two pairs needs them there.

The literal guard compares a **multiset**, not a set: `"transfer 100 to 200"` and
`"transfer 200 to 100"` have identical literal *sets* and different answers. Counting occurrences
costs nothing and closes that case.

### As built

| Property | Value |
|---|---|
| Lookup order | scope ─▶ exact hash ─▶ embed ─▶ threshold ─▶ literal guard ─▶ polarity guard |
| Exact path | SHA-256 of the normalised prompt, indexed — no embedding computed at all |
| Candidate window | newest **500** entries per `(tenant, scope)`, cosine computed in Python |
| Eviction | TTL only, purged opportunistically after a successful write. No sweeper, no size cap |
| TTL default | `PRISM_CACHE_TTL_SECONDS` unset ⇒ entries never expire |
| Write path | non-streaming responses only |

Four decisions in that table are worth their own line:

- **The tenant and the scope are part of the key, not a filter on it.** `docs/PRISM_PROBLEM_STATEMENT.md:66`
  forbids serving one team another team's answer, and a `WHERE tenant_id = ?` is a rule someone can
  forget to write. `uq_cache_exact` is `(tenant_id, cache_key, prompt_hash)`, so the isolation is in
  the shape of the key and a cross-tenant hit is not expressible.
- **The scope covers the tier, the conversation prefix and the shaping parameters.** A `fast` answer
  is not a valid `smart` answer; a reply that followed three turns of context is not a valid reply to
  the same question asked cold; and `temperature=0` and `temperature=1.4` are different requests. The
  prefix is matched **exactly** and only the final user turn is matched semantically, because a
  paraphrase of the last question is a paraphrase, while a paraphrase of the history is a different
  conversation.
- **The exact path runs before the embedding.** A repeated identical prompt — which is most of what a
  real cache sees — costs one indexed lookup and no ONNX inference. The semantic path only runs when
  the literal one misses.
- **A rejected candidate does not stop the scan.** A newer, nearer entry thrown out by the literal
  guard must not shadow an older valid one; the scan keeps going and the reason string records what
  was rejected and why (`semantic sim=0.9825 scanned=7 best=0.9825 threshold=0.9200 rejected=literal:1`).

**Cached answers can be streamed; streamed answers are not cached.** Replaying a stored completion
as SSE loses nothing — the chunks are generated from the real body, framed exactly like
`scripts/mock_provider.py:185` frames its own. Going the other way would mean assembling a
completion body out of deltas, and `system_fingerprint`, `logprobs` and any provider extension
simply are not in the delta stream, so a later non-streaming caller would receive the gateway's
reconstruction rather than a provider's response.

**The cache stores prompts and responses; the log deliberately does not.** `docs/DATA_MODEL.md:78`
asks for body storage to be a documented decision. It was declined for `request_log` and accepted
for `cache_entries`, because a cache that does not keep the response is not a cache. What limits the
exposure is that caching is opt-in per tenant (two of the four seeded keys have it off) and that TTL
is the only retention control — both are in the README's Known limitations.

### The smoke test still passes with a salted prefix

`scripts/smoke_test.py` prefixes each paraphrase prompt with a random 8-hex salt so a re-run does
not hit an entry the previous run wrote. Measured three times with the real model:

| Pair | Trial 1 | Trial 2 | Trial 3 | Behaviour at 0.92 |
|---|---|---|---|---|
| A (`req_cache_a1`/`a2`) | 0.9827 | 0.9857 | 0.9875 | **hits** |
| B (`req_cache_b1`/`b2`) | 0.8856 | 0.8874 | 0.9054 | WARN, as documented above |
| A1 / unrelated | 0.4862 | 0.4674 | 0.5091 | misses |

The salt is the *same* 8 hex characters for both members of a pair, so it contributes identical
literals to both and the literal guard does not reject the paraphrase. No smoke-test prompt trips
the volatility guard either.

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

**Method: kNN over 52 self-authored labelled exemplars**, in the same embedding space as the
cache. Built and measured; the numbers are at the end of this section.

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

### As built

| Property | Value |
|---|---|
| Exemplars | 52, across 23 shape tags, balanced 26/26 between the two difficulty classes |
| Difficulty labels | a float, 0.0 or 1.0 — never a tier name, so the *config* owns the vocabulary |
| Neighbours (k) | 5 |
| Softmax temperature | 0.05 |
| Ask extraction | applied above 30 words, clipped to 60 |

Three choices worth naming:

- **The exemplars carry a difficulty float, not a tier name.** A deployment with three tiers gets
  mapped by `_label_for_score`, which slices the weighted score across however many ordered labels
  the config declares. Exemplars labelled `"fast"`/`"smart"` would have hard-coded this repository's
  own sample config into the classifier.
- **Both sides of every comparison go through ask extraction.** The exemplars are extracted at
  `prepare()` time, not just the incoming prompt. Four exemplars carry pasted payload deliberately,
  and comparing an extracted ask against an unextracted wall of text compares two different things.
- **`route_reason` never contains prompt text.** It lands in `request_log`, so it carries method, k,
  n, score, margin, top cosine, the three nearest exemplars' *shape tags*, and how much of the
  prompt survived extraction — an explanation without a second copy of the prompt. A 54-word
  payload-plus-question prompt produces, verbatim:

  ```
  knn=exemplars(k=5,n=52) score=0.17 margin=0.33 top=0.67
  near=payload-lookup,snippet,payload-lookup ask=5/54w dropped=payload
  ```

  Five words of the 54 decided the tier, and an operator can see that without seeing the prompt.

### Measured

Scored once, as a held-out set, with `python scripts/routing_eval.py`:

| Classifier | `data/routing_eval.jsonl` (held out) | `tests/data/router_dev_set.jsonl` (tuned on) |
|---|---|---|
| Semantic (kNN) | **19/20 = 95%** | 19/20 = 95% |
| Length baseline | 8/20 = 40% | — |

Against the guide's bar of 80%, and all eight trap cases pass.

Two honest notes on those numbers:

- **The length baseline measured 40%, not the ~60% `docs/EVALUATION_GUIDE.md:82` predicts.** The
  threshold in this build is 24 words, chosen by looking at prompts rather than at the eval, and most
  of the eval's hard cases are short. A threshold fitted to the eval would score better and would
  mean nothing. The gap is worth stating rather than quietly matching the guide's figure.
- **The single miss is `route_009`, "Is 91 divisible by 7?"**, routed `smart`. Its neighbours are
  `proof`, `proof`, `arithmetic-check`: number-theory vocabulary is shared between "check this
  instance" and "prove this general claim", and the embedding cannot separate them from nine words.
  Left in rather than fixed with a targeted exemplar, which would be fitting the answer key. The
  error direction is the safe one — it over-routes to the expensive tier, so it costs money rather
  than answer quality.

Hyperparameters were swept on the dev set only. k=5 / T=0.05 was already optimal and sits on a
plateau, so nothing was changed after the sweep.

---

## Admin plane — as built

Five endpoints, all read-only, all behind one shared token
(`docs/API_CONTRACT.md:31` permits "any simple documented mechanism"):

| Endpoint | Answers |
|---|---|
| `GET /admin/usage?key=&from=&to=` | spend and token totals over a window, per key and in total |
| `GET /admin/logs?key=&limit=` | recent `request_log` entries, newest first |
| `GET /admin/cache/stats?key=&from=&to=` | hits, misses, hit rate, entries, tokens and dollars saved |
| `GET /admin/providers/health?window_seconds=` | per-provider error rate, latency and verdict |
| `GET /admin/keys?key=` | each key's policy and its standing this month |

`docs/API_CONTRACT.md:5` permits changing admin shapes if the changes are documented. Four are, and
each is a decision rather than drift:

- **`key=` is a selector, not a secret.** The contract's example echoes a whole virtual key, which
  implies putting a live credential in a query string — where it lands in the access log, in shell
  history, in the browser address bar and in the ops console's own URL, none of which can be revoked.
  So the parameter accepts a team name (recommended, and unique), a key prefix, or the full key
  resolved by hash for literal contract compatibility. Only the prefix is ever echoed back. A
  selector matching two tenants is a 400 naming the ambiguity, not a silently-chosen first row.
- **`key=` is optional.** Omitted, the top level totals every key and `by_key` breaks it down. That
  is the shape an ops console needs; returning a differently-shaped body depending on whether a
  filter was passed would force every consumer to branch.
- **`served` / `rejected` / `failed` partition `requests`.** `requests` counts every logged row
  including rejections, because `docs/DATA_MODEL.md:57` requires those rows to exist and an API that
  omitted them would disagree with the log it reads. The partition is what lets an operator see why a
  client's own count of successful calls is lower, instead of suspecting the meter. The three tuples
  are asserted exhaustive against `RequestStatus` by a test.
- **A log entry's `key` replaces the document's `virtual_key`.** The raw key is not stored at all, so
  there is nothing to render under that name; returning a prefix labelled `virtual_key` would be
  worse than renaming the field.

Three properties worth defending:

- **The token guard is declared on the router, not on each route.** A route added to
  `prism/api/admin.py` later is authenticated by construction. The per-route form is one forgotten
  parameter away from publishing every tenant's spend, and a parametrised test walks the whole
  surface to prove the guard was inherited.
- **Admin traffic writes no `request_log` row.** `prism/main.py:53` only opens an audit context for
  `/v1/`, so `requests` cannot measure how often someone refreshed the console.
- **Usage is a query, never a stored aggregate.** `docs/DATA_MODEL.md:14` calls the Usage Record "a
  query over `request_log`" and the reason is that a usage row written alongside every log row is a
  dual write, and dual writes drift. The one denormalised counter that does exist — `budget_periods`
  — is reported *next to* the log-derived total by `/admin/keys`, with a `reconciles` flag, so the
  claim that it is "a cache with a correctness proof" can be checked without writing SQL.

### Money is rendered two ways, deliberately

Aggregates are JSON **numbers**: they exist to be summed and charted, the contract's example shows a
number, and the totals are computed as exact `Decimal` in Postgres and converted once at the
boundary. Per-request costs in `/admin/logs` are **strings**, produced by the same `format_usd` that
wrote the `x-prism-cost-usd` header the client saw — so an operator chasing a reconciliation gap can
diff the two byte for byte. The scientific-notation argument in `prism/money.py` is about headers
read by shells and spreadsheets; a JSON number is read by a parser, for which `2e-05` is unambiguous.

### One reconciliation gap that is real, and is not a bug

`/admin/usage` reports what the **providers** were asked for. A cache hit is charged zero tokens and
zero dollars, so these totals reconcile against a provider invoice — but they are *lower* than a
client's own sum of the `usage` blocks it received, because a replayed cache hit carries the original
response's token counts in its body. The difference is exactly `tokens_saved` from
`/admin/cache/stats`. `scripts/load_test.py:123` prints the client-side figures for comparison, so
someone will hit this; it is a property of caching, not a discrepancy in the meter.

### Provider health is observed, not enforced

`gateway_config.json` ships a `degradation` block — `error_rate_threshold`, `window_seconds`,
`p95_latency_ms` — which was parsed and read by nothing. `/admin/providers/health` now reads it, and
answers the contract's "whether the gateway currently considers it healthy" with a verdict plus a
top-level **`enforced: false`**. There is still no circuit breaker: a provider reported `degraded` is
tried first anyway and still costs each request its retry budget before failover. Reporting a verdict
while implying it changed behaviour would be the dishonest version of this endpoint, so the flag is
part of the response and a test asserts it.

Two further honesty notes on that endpoint:

- **Errors are under-attributed, structurally.** A provider that failed and was successfully failed
  over from leaves a row naming the provider that *succeeded*, so its failure appears in `fallbacks`
  and `retries`, not in `errors`. Per-attempt accuracy needs a row per attempt. `fallbacks` rising
  while `errors` stays flat is the signal that something upstream in a chain is sick — and a test
  pins exactly that shape.
- **A provider with no traffic reports `unknown`, not `healthy`.** Silence is not health, and the
  provider nobody has called since the last restart is the one most likely to be broken. The provider
  list comes from the configuration rather than from observed traffic, so a completely dead provider
  still appears.

---

## End-to-end latency — as measured

Client-side wall clock, 40 samples per row, unique prompts so nothing is an accidental hit, against
two mock providers on loopback. Full method and the derivation in
[DAY6_VERIFICATION.md](DAY6_VERIFICATION.md#6-gateway-added-latency).

| Path | Mean | Notes |
|---|---|---|
| Straight to the mock provider | 11.3 ms | the baseline everything below is measured against |
| Gateway, cache off, `alias=fast` | 30.5 ms | **~19 ms added** by auth, allowlist, rate limit, budget, routing, the hop, the log write |
| Gateway, cache off, `alias=auto` | 46.5 ms | **+16 ms** for classification, of which 3.8 ms is the embedding and the rest is a thread hop and the ~15.6 ms scheduler tick |
| Gateway, cache on, all misses | 102.3 ms | **+49 to +64 ms**, depending on stored entries |
| An exact cache hit | 22.1 ms | short-circuits on the SHA-256 key before any embedding |

Cache-miss cost against cache size, which is the number Day 5 recorded as unmeasured:

| Tenant | Entries | Mean cost of a miss |
|---|---|---|
| `free-tier` | 33 | 79.9 ms |
| `search` | 276 | 94.8 ms |

**~0.06 ms per stored entry**, plus ~49 ms fixed (fetching the tenant's vectors, the thread hop, and
the `INSERT` of a new entry with its 384 floats). Two points on one machine, so treat the slope as an
order of magnitude. What it settles is the shape: linear, and visible at hundreds of entries.

Both tenants are under `MAX_CANDIDATES = 500`, so this is the linear regime. Past the cap the scan
**plateaus at ~30 ms** and the limitation stops being about latency: a valid match older than the
newest 500 entries is silently missed instead. That is the version of the problem `pgvector` has to
solve, and raising the cap only trades it back.

Two measurement traps worth recording, because both produced wrong numbers first:

- **Medians quantize to multiples of 16 ms** on this box — the Windows scheduler tick showing through
  asyncio's sleep granularity. Means over 40 samples average it out; a median of five samples does
  not.
- **`research` has caching off and `free-tier` has it on**, which is not the intuitive assignment. An
  earlier pass used `free-tier` as the cache-off control, measured the cache twice, and reported it
  as costing ~8 ms instead of ~49.

## Caveats to carry into Known limitations

- `scripts/fetch_model.py --fetch` **has never been run**: the model was vendored out of band, so
  the download half ships unverified against a live host. The verify half runs and passes.
- Cache pair B (`req_cache_b1`/`b2`) is a permanent WARN at the demoed threshold of 0.92, by the
  deliberate trade documented above.
- Embedding is CPU-bound and single-process; throughput under concurrency is bounded by
  `asyncio.to_thread`'s default executor. This now applies to `auto` routing as well as to the
  cache, so it is on the critical path of every routed request, not only of cache lookups.
- The router's single held-out miss (`route_009`) is left in deliberately, and the length baseline
  measures 40% rather than the guide's stated ~60% — both explained above.
- Ask extraction cannot separate payload from question when the payload has no sentence boundaries
  at all: a space-joined token run with a question appended is one "sentence" containing the ask, so
  it is clipped rather than dropped. It degrades rather than breaks, because the clip keeps head and
  tail and the question is at the tail. Pinned by a test.
- Streamed answers are never written to the cache, by the argument above. A tenant whose traffic is
  entirely streaming gets no cache entries at all.
- The cache has **no size cap and no background sweeper**. With `PRISM_CACHE_TTL_SECONDS` unset —
  the default, which is what makes the paraphrase demo reproducible — entries never expire and the
  table grows without bound.
- A lookup scans the newest **500** entries per `(tenant, scope)`, so a valid entry can fall out of
  the window on a busy tenant and be missed while still occupying storage. The point at which a
  per-tenant Python scan stops being affordable is the point at which this needs pgvector — now
  measured at **~0.06 ms per stored entry** (see the latency section above), so the 500-entry window
  caps the scan at roughly 30 ms and the cap is doing real work rather than being a precaution.
- The volatility guard is lexical: it refuses to *store* an answer whose prompt contains time words
  ("today", "right now", "latest"). A time-sensitive question phrased with no time words at all —
  "what is the price of X" — is cached like any other.
- `cache_entries` stores prompt text and response bodies, which `request_log` deliberately omits.
  Caching is opt-in per tenant and TTL is the only retention control.
- Provider health is **observed, not enforced**, and its error counts are under-attributed when a
  failover succeeds — both explained in the admin section above.
- `/admin/usage` totals are lower than a client's own sum of response `usage` blocks by exactly the
  tokens the cache replayed. Explained above; it is the correct behaviour for reconciling against a
  provider invoice.
- Key management is read-only. `GET /admin/keys` exists; `POST /admin/keys`, which
  `docs/PRISM_PROBLEM_STATEMENT.md:186` lists as good to have, does not.
- The admin plane has one shared token, no rotation and no per-operator identity, so its audit story
  is "someone with the token did this".
