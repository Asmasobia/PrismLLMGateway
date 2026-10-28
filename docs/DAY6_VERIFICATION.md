# Day 6 verification report

Everything `docs/EVALUATION_GUIDE.md:102-110` asks a verification report to include, measured
against a live stack on this machine, with the commands used and the raw output. Where a number
here disagrees with an earlier draft of the README, this document is the later measurement and the
correct one — two such corrections are called out explicitly in §5 and §6, because a report that
quietly revises itself is not evidence.

Companion documents:

- [../README.md](../README.md) — how to run it; **Known limitations** is the honest ledger
- [DESIGN_NOTES.md](DESIGN_NOTES.md) — the "as built" tables
- [DAY1_DESIGN_LOG.md](DAY1_DESIGN_LOG.md) … [DAY5_DESIGN_LOG.md](DAY5_DESIGN_LOG.md) — why each
  piece is the way it is
- [EVALUATION_GUIDE.md](EVALUATION_GUIDE.md) — provided; the checklist this report answers

---

## Contents

- [0. The stack under test](#0-the-stack-under-test)
- [1. Test suite](#1-test-suite)
- [2. Smoke test](#2-smoke-test)
- [3. Load test and accounting reconciliation](#3-load-test-and-accounting-reconciliation)
- [4. Routing eval](#4-routing-eval)
- [5. Semantic cache: threshold, embedding, and the sample pairs](#5-semantic-cache-threshold-embedding-and-the-sample-pairs)
- [6. Gateway added latency](#6-gateway-added-latency)
- [7. Provider failure drills](#7-provider-failure-drills)
- [8. The timeout drill, and the bug it found](#8-the-timeout-drill-and-the-bug-it-found)
- [9. The four additional reviewer checks](#9-the-four-additional-reviewer-checks)
- [10. Known limitations](#10-known-limitations)

---

## 0. The stack under test

```bash
docker compose up -d                                              # Postgres 16 on :5433
python scripts/init_db.py                                         # schema + 4 seeded tenants
python scripts/mock_provider.py --port 9001 --name alpha &
python scripts/mock_provider.py --port 9002 --name beta &
python -m uvicorn prism.main:app --host 127.0.0.1 --port 8080 &
```

Python 3.12.8, Postgres 16.4 on port 5433, two mock providers, one gateway process. The seeded
policy, read back from `GET /admin/keys` rather than from the seed file, because what is loaded is
what matters:

| Team | rpm | tpm | Monthly budget | Allowlist | Cache | Threshold |
|---|---|---|---|---|---|---|
| `search` | 60 | 100,000 | $50.00 | `fast` | on | 0.92 |
| `research` | 300 | 1,000,000 | $500.00 | `fast`, `smart`, `auto` | **off** | — |
| `free-tier` | 10 | 20,000 | $5.00 | `fast` | on | 0.85 |
| `budget-demo` | 60 | 20,000 | **$0.00001** | `fast` | **off** | — |

That table is worth reading before the latency section. Which tenants have caching enabled is not
the intuitive assignment — `research`, the most permissive key, has it **off**, and `free-tier` has
it **on** — and a measurement that assumes otherwise measures the wrong thing. It did, once; see
§6.

---

## 1. Test suite

```bash
python -m pytest
# 391 passed in 42.47s

python -m ruff check prism/ tests/
# All checks passed!
```

391 tests, no skips, no xfails. The suite needs Postgres on 5433 (the `postgres` marker) and the
vendored embedding model (the `model` marker); both were present, so nothing was deselected. The
project is checked with `ruff check` only and is deliberately not `ruff format` clean.

---

## 2. Smoke test

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8080 --key prism-sk-search-1a2b3c
```

```
[1] Non-streaming completion + header contract
  PASS  returns 200  (got 200)
  PASS  body has choices and usage
  PASS  header x-prism-provider present  (alpha/alpha-small)
  PASS  header x-prism-cache present  (miss)
  PASS  header x-prism-cost-usd present  (0.0000147000)
  PASS  first request is a cache miss  (miss)

[2] Streaming
  PASS  returns 200  (got 200)
  PASS  content-type is text/event-stream  (text/event-stream; charset=utf-8)
  PASS  received multiple SSE chunks  (24 data lines)
  PASS  stream ends with [DONE]

[3] Auth
  PASS  invalid key rejected with 401/403  (got 401)

[4] Unknown model
  PASS  unknown model rejected with 4xx  (got 404)
  PASS  error body explains the problem

[5] Semantic cache: exact repeat
  PASS  identical repeat is a cache hit  (hit)

[6] Semantic cache: paraphrases (each pair soft; at least one pair must hit)
  PASS  pair A paraphrase is a cache hit  (hit)
  WARN  pair B paraphrase is a cache hit  (miss)
  PASS  matching is semantic, not exact-only (at least one pair hit)

[7] Semantic cache: unrelated prompt
  PASS  unrelated prompt is a cache miss  (miss)

==================================================
  17 passed, 1 warnings, 0 failed
```

**17 passed, 1 warning, 0 failed.**

### The WARN, explained

Pair B's paraphrase does not hit at `search`'s threshold of 0.92. The script marks each pair soft
and requires *at least one* to hit, so this is within the contract — but "within the contract" is
not an explanation, and §5 gives the actual cosine similarities. The short version: pair B's two
prompts embed at **0.8693**, and the near-miss case that must *not* hit embeds at **0.8487**. They
are 0.0206 apart. Any threshold low enough to catch pair B is within two hundredths of also
serving a two-factor-authentication question out of a password-reset cache entry. The WARN is the
correct side of that trade, and lowering the threshold to turn it green would be tuning against the
sample file.

---

## 3. Load test and accounting reconciliation

Two bursts, at different concurrency, against keys with different rpm limits. Each was run after
waiting out the current rate-limit minute, so the accepted count is the whole minute's allowance
and not a fraction of it left over from probe traffic.

### Burst 1 — 40 requests at concurrency 20, against the rpm-10 key

```bash
python scripts/load_test.py --url http://127.0.0.1:8080 --key prism-sk-free-7g8h9i \
  --requests 40 --concurrency 20 --rpm-limit 10
```

```
Burst finished in 1.1s
  accepted (200):      10
  rate limited (429):  30
  latency avg/p95 ms:  976 / 1125

Client-side accounting totals - compare with your usage API for this key:
  accepted requests:   10
  prompt tokens:       120
  completion tokens:   267
  sum of cost headers: 0.000178 USD (10 of 10 had a numeric header)

Over-admission check OK: 10 accepted <= limit 10.
```

**Exactly 10 of 10.** Not 9, not 11 — the limiter admits its full allowance and not one more, which
is the property a limiter that merely looked correct would miss in one direction or the other.

### Burst 2 — 100 requests at concurrency 30, against the rpm-60 key

```bash
python scripts/load_test.py --url http://127.0.0.1:8080 --key prism-sk-search-1a2b3c \
  --requests 100 --concurrency 30 --rpm-limit 60
```

```
Burst finished in 3.1s
  accepted (200):      60
  rate limited (429):  40
  latency avg/p95 ms:  1297 / 2282

Client-side accounting totals - compare with your usage API for this key:
  accepted requests:   60
  prompt tokens:       720
  completion tokens:   1587
  sum of cost headers: 0.001060 USD (60 of 60 had a numeric header)

Over-admission check OK: 60 accepted <= limit 60.
```

**Exactly 60 of 60**, at concurrency 30 — half again the concurrency of the first burst, against a
limit six times larger. The over-admission this test hunts for is an in-process race; `rpm-60 at
concurrency 30` is the configuration most likely to expose one, because thirty threads contend for
a counter with room to spare.

### Reconciliation against the usage API

`GET /admin/usage?key=search` and `GET /admin/keys?key=search` were snapshotted immediately before
and after burst 2, so what follows is a delta rather than a lifetime total that happens to look
plausible.

| Field | Before | After | Delta | Client-side figure | Agrees |
|---|---|---|---|---|---|
| `requests` | 279 | 379 | **100** | 100 sent | ✅ |
| `served` | 174 | 234 | **60** | 60 accepted | ✅ |
| `rejected` | 103 | 143 | **40** | 40 rate limited | ✅ |
| `failed` | 2 | 2 | **0** | 0 | ✅ |
| `prompt_tokens` | 1,237 | 1,957 | **720** | 720 | ✅ |
| `completion_tokens` | 3,305 | 4,892 | **1,587** | 1,587 | ✅ |
| `cost_usd` | 0.00213575 | 0.00319595 | **0.00106020** | 0.001060 | ✅ |

Every rejected request is logged, which is why `requests` moves by the full 100 and not by 60 — a
meter that only counted successes could not answer "was this client throttled or is it not
calling".

And the two independent paths to this month's spend agree:

| Field | Before | After |
|---|---|---|
| `spent_usd` (the atomic counter on `tenant`) | 0.00213575 | 0.00319595 |
| `logged_cost_usd` (the sum over `request_log`) | 0.00213575 | 0.00319595 |
| `reconciles` | `true` | `true` |

These are computed by different queries over different tables. `spent_usd` is incremented by the
atomic `UPDATE ... SET spent = spent + ? RETURNING spent` that admission checks against;
`logged_cost_usd` is a `SUM` over the audit rows. They agree to the tenth decimal place for all
four keys, which is the check that would have caught a lost update under concurrency — and is why
this project requires Postgres rather than SQLite (`tests/test_schema.py` proves the atomicity
claim directly, and shows the read-then-write version losing updates deterministically).

---

## 4. Routing eval

Graded offline — no gateway, no database, no network.

```bash
python scripts/routing_eval.py                      # semantic
python scripts/routing_eval.py --classifier length  # the baseline it has to beat
```

### Method

`method knn-exemplars  n=52 k=5 T=0.05  model=BAAI/bge-small-en-v1.5`

The `auto` alias classifies difficulty by k-nearest-neighbour against **52 hand-written exemplar
prompts**, each labelled simple or complex and tagged with a category (`proof`, `definition`,
`design-tradeoffs`, `payload-lookup`, …). A prompt is embedded once, cosine-compared to all 52, and
the `k=5` nearest vote with weight; a score above `0.5 + T` routes `smart`, below `0.5 - T` routes
`fast`. Before any of that, long prompts are reduced to their **ask**: quoted spans and pasted
payload are dropped, and only sentences carrying a question or instruction cue are kept. That step
is visible in the per-case output as `ask=6/65w dropped=payload`.

No code under `prism/` reads `expected_tier`. The answer key reaches the scorer and never the
classifier.

### Result: 19/20 = 95.0%, against 8/20 = 40.0% for the length baseline

| Case | Expected | Actual | | Score | Nearest three exemplars | Ask reduction |
|---|---|---|---|---|---|---|
| `route_001` | fast | fast | ✅ | 0.00 | translation, payload-lookup, payload-lookup | 6/6w |
| `route_002` | fast | fast | ✅ | 0.07 | unit-conversion, fact-lookup, arithmetic-check | 6/6w |
| `route_003` | fast | fast | ✅ | 0.00 | short-writing, copy-edit, short-writing | 12/12w |
| `route_004` | fast | fast | ✅ | 0.09 | definition, incident-diagnosis, short-writing | 7/7w |
| `route_005` | fast | fast | ✅ | 0.00 | translation, copy-edit, translation | 5/5w |
| `route_006` | fast | fast | ✅ | 0.10 | extraction, extraction, payload-lookup | 6/65w `dropped=payload` |
| `route_007` | fast | fast | ✅ | 0.47 | incident-diagnosis, definition, short-writing | 9/53w `dropped=payload` |
| `route_008` | fast | fast | ✅ | 0.00 | short-writing, extraction, translation | 18/18w |
| `route_009` | fast | **smart** | ❌ | 0.66 | **proof, proof, arithmetic-check** | 5/5w |
| `route_010` | fast | fast | ✅ | 0.00 | short-writing, copy-edit, copy-edit | 33/39w `dropped=payload` |
| `route_011` | smart | smart | ✅ | 0.94 | proof, proof, proof | 9/9w |
| `route_012` | smart | smart | ✅ | 0.86 | estimation, capacity-derivation, translation | 11/11w |
| `route_013` | smart | smart | ✅ | 1.00 | causal-mechanism, causal-mechanism, capacity-derivation | 16/16w |
| `route_014` | smart | smart | ✅ | 1.00 | design-tradeoffs, design-tradeoffs, plan-rollback | 19/19w |
| `route_015` | smart | smart | ✅ | 1.00 | design-tradeoffs, compare-recommend, design-tradeoffs | 20/20w |
| `route_016` | smart | smart | ✅ | 0.78 | algorithm-explain, snippet, semantics-reasoning | 23/23w |
| `route_017` | smart | smart | ✅ | 1.00 | semantics-reasoning, proof, design-tradeoffs | 23/23w |
| `route_018` | smart | smart | ✅ | 1.00 | plan-rollback, plan-rollback, design-tradeoffs | 22/22w |
| `route_019` | smart | smart | ✅ | 0.91 | incident-diagnosis, causal-mechanism, definition | 27/27w |
| `route_020` | smart | smart | ✅ | 0.98 | proof, proof, proof | 14/14w |

### The one miss, explained

**`route_009` — "Is 91 divisible by 7?" — expected `fast`, routed `smart`.** Score 0.66, and its
three nearest neighbours are `proof`, `proof`, `arithmetic-check`. The cause is that number-theory
*wording* is shared between checking one instance and proving a general claim: "is 91 divisible by
7" and "prove that every prime greater than 3 is congruent to ±1 mod 6" are lexically and
semantically close, and an embedding has no arithmetic to tell them apart.

It is left unfixed deliberately. A targeted exemplar would fix this case and nothing else, and
`docs/DESIGN_NOTES.md` forbids tuning against `data/routing_eval.jsonl` because it is the answer
key. The direction of the error is also the safe one: the request overspends on a capable model
rather than under-answering a hard question on a cheap one.

### The baseline

`--classifier length` scores **8/20 = 40.0%**, missing `route_006, route_007, route_010, route_011,
route_012, route_013, route_014, route_015, route_016, route_017, route_018, route_020` — twelve
cases, in both directions. The pack's fast cases include long prompts that are long because of
*pasted payload*, and its smart cases include short prompts that are short because a hard question
can be asked briefly ("trap: short but requires a proof", as the eval file's own note says).

`docs/EVALUATION_GUIDE.md:82` predicts ~60% for a length heuristic; this build's threshold of 24
words measures 40%. The threshold was chosen by reading prompts, not by fitting the eval. Reported
as measured — raising it to make the comparison look fairer would have meant fitting the answer key
in the other direction.

---

## 5. Semantic cache: threshold, embedding, and the sample pairs

### Embedding choice

**`BAAI/bge-small-en-v1.5`**, quantized ONNX, run locally through `fastembed`, pinned to revision
`52398278842ec682c6f32300af41344b1c0b0bb2`. 384 dimensions, L2-normalised so cosine similarity is a
dot product, 65 MB on disk, no network at any point: `HF_HUB_OFFLINE=1` is set in the embedder's constructor
rather than left to the environment, so a missing model is a startup error and never a silent
download. The same embedder serves the cache and the router, so a request that is both classified
and cached pays for one model, not two.

Chosen over a larger model because the decision this embedding supports is a threshold comparison,
not a ranking: 384 dimensions separate "same question, different words" from "different question,
similar words" well enough (the numbers below show the margin), and the cost is on the critical
path of every cached request.

### Thresholds

Per tenant, from the seed file: **`search` 0.92**, **`free-tier` 0.85**. `research` and
`budget-demo` have caching disabled. A per-tenant threshold rather than one global number is what
lets a search product run tight and a free tier run loose, and it is why `/admin/cache/stats`
reports `similarity_threshold` per key and `null` on the total — averaging thresholds would invent
a value no lookup ever used.

### Observed behaviour on the sample prompts

The prompts are `data/sample_requests.json`'s `req_cache_a1`/`a2`/`a3` and `req_cache_b1`/`b2`.
Cosine similarity computed with the same embedder the cache uses:

| Pair | Similarity | vs 0.92 | Observed on `search` | Correct |
|---|---|---|---|---|
| A1 ↔ A2 (paraphrase: "reset my password" / "steps to reset my password") | **0.9825** | above | **hit** | ✅ |
| A1 ↔ A3 (near-miss: password reset vs **two-factor** reset) | **0.8487** | below | **miss** | ✅ |
| B1 ↔ B2 (paraphrase: "refund policy for annual plans" / "bought an annual plan, can I get my money back") | **0.8693** | below | **miss** | ⚠️ the WARN |
| A1 ↔ B1 (unrelated) | **0.4283** | far below | **miss** | ✅ |

Live behaviour, in order, on the `search` key:

```
A1 first ask                   200 cache=miss      31 ms
A2 paraphrase of A1            200 cache=hit       47 ms
A3 near-miss, other intent     200 cache=miss      31 ms
B1 first ask                   200 cache=miss      47 ms
B2 paraphrase of B1            200 cache=miss      62 ms
```

**`req_cache_a3` is the case worth dwelling on.** It is a *different question in almost identical
words* — resetting two-factor authentication is not resetting a password, and serving the cached
password-reset answer would be a wrong answer, not a stale one. It embeds at 0.8487 and correctly
misses.

Now put the four numbers in order:

```
0.4283          0.8487        0.8693              0.9825
unrelated       A3 near-miss  B2 paraphrase       A2 paraphrase
                       └── 0.0206 apart ──┘
```

The near-miss that must not hit and the paraphrase that should are **0.0206 apart**, and the
near-miss is the lower of the two. There is a window — a threshold in (0.8487, 0.8693] — that gets
both right, and `free-tier`'s 0.85 sits inside it. But it is a two-hundredths-wide window located
by looking at the answers, which is exactly the tuning `docs/DESIGN_NOTES.md` forbids; 0.92 was
chosen for `search` as the value that keeps a comfortable margin above the near-miss, and the cost
of that choice is one missed paraphrase. Stated as a trade rather than hidden as a WARN.

### Cache statistics, lifetime, across all the traffic in this report

```
overall: lookups=604 hits=91 misses=513 hit_rate=0.1507 entries=228 tokens_saved=2462 cost_saved=$0.001227
  budget-demo  enabled=False  lookups=1    hits=0    rate=0.000  entries=0
  free-tier    enabled=True   lookups=41   hits=8    rate=0.195  entries=33
  research     enabled=False  lookups=268  hits=0    rate=0.000  entries=0
  search       enabled=True   lookups=294  hits=83   rate=0.282  entries=195
```

The headline 15.1% understates the cache, and the reason is visible in the breakdown: `research`'s
268 lookups sit in the denominator with caching disabled, so they can never be hits. The
cache-enabled keys run 28.2% and 19.5%. This is a known limitation rather than a bug — the
requests *do* reach the cache, and the reasoning for leaving the denominator alone is in the
README's **Known limitations**. The two halves of the response also have different time bases on
purpose: hits and misses are windowed, `entries` and the savings are lifetime, and the response
labels them rather than blending them into one number that means neither.

---

## 6. Gateway added latency

### How it was measured

Client-side wall clock, from a single-threaded Python script, 40 samples per row, each with a
**unique prompt** so nothing is an accidental cache hit. The comparison is a round trip straight to
the mock provider against the same round trip through the gateway; the difference is everything
Prism does — auth, allowlist, rate limit, budget admission, routing, the HTTP hop, the cache, and
the log write.

Two measurement corrections, both of which changed the answer:

1. **Medians came back quantized to multiples of 16 ms.** That is the Windows scheduler tick
   (~15.6 ms) showing through asyncio's sleep granularity, not a property of the gateway. The means
   below are over 40 samples, which averages the tick out; the medians are shown beside them so the
   quantization is visible rather than hidden.
2. **An earlier pass used `free-tier` as the "cache off" key.** `free-tier` has caching **on**
   (§0), so that row measured the cache twice and reported the cache as costing ~8 ms — a figure an
   earlier draft of the README repeated. The rows below use `research` for cache-off and `search`
   for cache-on. The real cost is an order of magnitude larger.

### Results

| | mean | median |
|---|---|---|
| upstream alone, straight to the mock | **11.3 ms** | 15.0 ms |
| gateway, cache **off** (`research`), `alias=fast` | **30.5 ms** | 31.0 ms |
| gateway, cache **off** (`research`), `alias=auto` | **46.5 ms** | 47.0 ms |
| gateway, cache **on** (`search`), `alias=fast`, all misses | **102.3 ms** | 94.0 ms |
| an exact cache **hit** (`search`) | **22.1 ms** | 31.0 ms |

Derived:

| | |
|---|---|
| **added by the gateway, no cache work** | **19.1 ms** |
| of which kNN difficulty classification (`auto` − `fast`) | **16.0 ms** |
| cost of a cache lookup + write on a **miss** | **71.9 ms** |
| an exact **hit** versus a miss on the same key | **80.3 ms faster** |

### Reading these numbers honestly

**The gateway's own overhead is ~19 ms.** That covers auth, the allowlist check, the rate limiter,
atomic budget admission, alias resolution, the extra HTTP hop, and writing a `request_log` row.

**Difficulty classification costs ~16 ms end to end — but that is an upper bound, not the
compute.** Day 4 measured `embed_one` directly, in-process and warm, at a **median of 3.8 ms**
(range 3.5–5.3 ms), and the kNN vote itself is 52 dot products against vectors already in memory.
The ~12 ms gap between 3.8 and 16 is not embedding time: embedding runs in a thread (ONNX holds the
GIL), so this delta crosses an `asyncio.to_thread` hop, and any measurement that crosses a thread
boundary on this box is quantized by the ~15.6 ms scheduler tick. Both numbers are honest about
different things — 3.8 ms is what the model costs, ~16 ms is what a client can observe — and the
end-to-end figure is the one that belongs in a latency budget. Either way it is why `auto` is opt-in
per tenant and why classification runs *after* every rejection check: a refused request pays none of
it.

**A cache miss is the expensive path, not the cheap one — ~72 ms.** That is counter-intuitive
enough to be worth breaking down, so the miss cost was measured against two tenants with different
numbers of stored entries:

| Tenant | Entries | Mean cost of a miss | Over cache-off (30.5 ms) |
|---|---|---|---|
| `free-tier` | 33 | 79.9 ms | +49.4 ms |
| `search` | 276 | 94.8 ms | +64.3 ms |

243 additional entries added ~15 ms, so the **per-tenant similarity scan costs roughly 0.06 ms per
entry** and the remaining **~49 ms is fixed**. That fixed part is *not* mostly the embedding: at
3.8 ms of actual model time it is a small share of 49 ms. It is the round trip to fetch the tenant's
stored vectors, the thread hop to embed, and the `INSERT` of the new entry with its 384 floats — a
second database write on the request's critical path, which a served request without caching does
not pay.

The scan is linear because `prism/cache.py` does similarity in Python over the tenant's rows rather
than in the database. Day 5's design log recorded, as an open item, that "the point at which a Python
scan stops being affordable has not been measured here"; this measures it.

**But the growth does not continue, and that changes what the limitation is.**
`prism/cache.py:87` caps a lookup at `MAX_CANDIDATES = 500`, taking the newest 500 entries by
`created_at`. Both tenants measured above are under that cap — 33 and 276 entries — so the linear
regime is what the slope describes. Past 500, scan cost **plateaus at roughly 30 ms** (500 × 0.06 ms)
and stops growing, because the `ORDER BY created_at DESC LIMIT 500` is index-assisted and does not
care how many rows sit behind it.

So the honest statement is not "this gets slow without bound". It is that the cap converts a latency
problem into a **correctness** one: a tenant with 5,000 cached entries scans 10% of them, and a valid
paraphrase match older than the newest 500 is silently missed while still occupying storage. A
`pgvector` index is the fix for both halves at once — it removes the need for a cap, so the entry
stops being missed *and* the scan stops being linear. Tuning `MAX_CANDIDATES` upward only trades the
correctness problem back for the latency one.

The slope itself is two points on one machine, so treat ~0.06 ms/entry as an order of magnitude
rather than a coefficient. What it establishes firmly is the shape, and that the 500 cap is doing
real work rather than being a precaution.

**A cache hit is fast in absolute terms and unimpressive against this upstream.** 22.1 ms end to
end, 80 ms faster than a miss on the same key — but only ~11 ms faster than the mock provider
answers unaided, because an exact-repeat hit short-circuits on the SHA-256 key before any embedding
is computed, and there is simply not much left to beat. **Against a real LLM the arithmetic
inverts**: the saved call is 1–3 seconds, so a hit is a 50–100× improvement and even a 72 ms miss
penalty is noise. The mock is the wrong yardstick for the cache's value and the right one for the
gateway's overhead, and this report uses it for the latter only.

### The `localhost` finding

Worth repeating from the README because it will bite anyone running this on Windows: pointing a
provider `base_url` at `localhost` rather than `127.0.0.1` cost **~265 ms per upstream call**.
`localhost` resolves dual-stack; the mock closes the connection after each response, so the name is
re-resolved every time; and a *pooled* `httpx` client pays it too (127.0.0.1: 0.0 ms median,
localhost: 265.5 ms). Total added latency measured 305 ms before the change and ~31 ms after, with
**no change to any file under `prism/`**. `gateway_config.json` now uses `127.0.0.1` and says why
in a `_comment`.

---

## 7. Provider failure drills

Driven through the mock providers' live failure injection (`POST /admin/config` on :9001).

| Drill | Injected | Observed |
|---|---|---|
| Provider down | alpha `mode: down` | beta served, `x-prism-provider: beta/beta-small`, `x-prism-fallback: true` |
| Recovery | alpha restored | traffic returned to the primary unaided on the next request, 313 ms via alpha — no manual intervention, and no lingering preference for the fallback |
| Slow but under the timeout | alpha `latency_ms: 3000` | served in 3094 ms via alpha, `fallback=false`, p95 3117 ms — under the configured 5000 ms degradation threshold, so a slow provider is *reported* slow without being abandoned |
| Intermittent errors | alpha `fail_rate: 0.3`, 8 requests | **8/8 succeeded**, one via fallback — the retry budget absorbed the rest |
| Past the timeout | alpha `latency_ms: 35000` | see §8 |

### An honest note on the health snapshot

During the drills, `GET /admin/providers/health` reported alpha with `requests=0 errors=0` while
alpha was the provider that was down, and beta carrying `retries=3 fallbacks=1`. That reads like a
bug and is not one. It is structural under-attribution, documented at `prism/usage.py:540-546`
before these drills ran: a request that failed over leaves **one** log row, and that row names the
provider that *succeeded*. So a dead provider's failures are attributed to its replacement, and the
signal for an outage is **`fallbacks` rising while `errors` stays flat** — which is exactly what the
snapshot showed. The drills confirmed the docstring rather than contradicting it.

Health after the timeout drills, over a one-hour window:

```
provider health, 1h window, enforced=false
  alpha  healthy   req=297  err=0   rate=0.000  fb=0  ret=0  avg=272    p95=1321
  beta   degraded  req=3    err=1   rate=0.333  fb=3  ret=3  avg=40177  p95=57098
```

Beta is `degraded` because the only traffic it ever served was the timeout drills — three requests,
each waiting out a 30-second stall on alpha first, one of which failed when both providers were
stalled. `enforced: false` is the most important field in that response: nothing routes on the
verdict, so a provider reported `degraded` is still tried in its configured order. Reporting a
verdict while implying it changed behaviour would be the dishonest version of the endpoint.

---

## 8. The timeout drill, and the bug it found

`docs/EVALUATION_GUIDE.md:91` asks for one drill the earlier passes did not cover:

> Make `alpha` slow (`latency_ms: 3000` or more) → your timeout fires; the client gets a timely
> response (fallback or clean error), never a hang.

3000 ms is well under the 30-second read timeout, so the earlier drill never fired a timeout at
all — it measured a slow success. Injecting `latency_ms: 35000`, past the timeout, found a real
defect.

### Before: 90.5 seconds

```
alpha at 35000 ms - PAST the timeout, so it must fire and beta must serve
  PASS  the client still got an answer            (200 in 90546 ms)
  PASS  served by the fallback, not the primary   (beta/beta-small)
  PASS  x-prism-fallback is true                  (true)
  FAIL  and it did NOT wait out the slow provider (90546 ms)
```

The log row said `retries=3`. Three consecutive 30-second timeouts against alpha before the chain
moved to beta — `max_attempts: 3` × a 30-second read timeout. Every individual behaviour was
correct: the timeout fired, failover happened, the header was honest, the answer arrived. The
*aggregate* was a 90-second wait, which is the hang the guide says must not happen.

### The cause

`prism/providers/http.py` classified every `httpx.TimeoutException` as `retry_same=True`, with the
reasoning that "a timeout says nothing about whether the request was valid, only that this attempt
did not finish". That is true and it is the wrong question. What matters is what the failed attempt
**cost**: a read timeout has by definition just spent the entire read budget, so retrying it
against the same endpoint spends the whole budget again, and failover does not begin until
`max_attempts` of them have elapsed.

### The fix

`_timeout_retry_same` in `prism/providers/http.py`. A **read** timeout (and a pool timeout, since
this client's pool timeout *is* the read budget) is no longer retried against the same target. A
**connect** timeout still is, because it is different in kind: it costs `connect_timeout_seconds`,
not the read budget, and an incomplete handshake is the transient blip backoff exists for. `try_next`
stays `True` in every case — the request still fails over, and the caller's wait is now bounded by
the **depth of the chain** instead of depth × attempts.

The rule went in the classifier rather than in `prism/dispatch.py`, because the dispatcher by design
never reasons about *why* an attempt failed; it acts on two booleans set where the failure was seen.
Putting a timeout special case in the loop would have created a second copy of the policy.

### After: 30.0 seconds

```
Read timeout is 30 s. Stalling alpha at 35000 ms, past it.

Non-streaming:
  PASS  the client got an answer                        (200 in 30.0 s)
  PASS  served by the fallback                          (beta/beta-small fallback=true)
  PASS  exactly ONE extra upstream call, not three      (retries=1)
  PASS  the wait is one timeout, not max_attempts of them  (30.0 s, against 90 s before the fix)

Streaming (the same path through open_stream):
  PASS  the stream opened                               (200, first byte at 30.0 s)
  PASS  over the fallback                               (beta/beta-small fallback=true)
  PASS  one extra upstream call                         (retries=1)
  PASS  no stacked timeouts                             (30.4 s total)

Both providers stalled - the chain must give up cleanly, not hang:
  PASS  a clean 502, not a hang and not a 500           (502)
  PASS  bounded by chain depth (2 timeouts), not depth x attempts (6)
        (60.1 s, against a worst case of 180 s before the fix)
  PASS  logged as upstream_error
  PASS  one retry total across both providers           (retries=1)

Restoring both providers.
  PASS  traffic returns to the primary                  (alpha/alpha-small in 47 ms)
```

| Scenario | Before | After |
|---|---|---|
| One provider stalled past the timeout, non-streaming | 90.5 s | **30.0 s** |
| One provider stalled past the timeout, streaming | — | **30.4 s**, first byte at 30.0 s |
| Both providers stalled | 180 s worst case | **60.1 s**, clean 502 |

The streaming half mattered as much as the non-streaming half and had the same bug: `open_stream`
had its own copy of the classification. It matters *more*, if anything — a streaming client watches
a blank screen for the whole wait, so three stacked timeouts are three times as visible.

Covered by four new tests in `tests/test_providers.py`, which drive real `httpx` timeouts through a
`MockTransport` and assert the flags on the raised `ProviderCallFailed` rather than inspecting the
source. Suite: 391 passed.

### What is still not fixed

There is **no total deadline for a request**. The bound is now chain depth × the read timeout, which
is 60 s for this two-provider chain but would be 150 s for a five-provider one, and no configuration
value caps it. The clean answer is a per-request budget that shrinks as attempts consume it and is
passed down as each call's timeout; it changes the signature of every provider call and is not built.
It is in the README's **Known limitations** with that reasoning, alongside the related gap that
there is no circuit breaker, so a sustained outage pays this cost on every single request.

---

## 9. The four additional reviewer checks

`docs/EVALUATION_GUIDE.md:97-100`. All four run live against the stack in §0.

### Cache isolation — the same prompt under two keys must not share an entry

```
  PASS  first ask on search is a miss                (miss)
  PASS  identical repeat on search hits              (hit)
  PASS  SAME prompt on free-tier does NOT hit        (miss - a hit here would be a cross-tenant leak)
```

The prompt is byte-identical across the three calls, and a fresh UUID was appended so no earlier
traffic could have primed it. `search` misses, then hits itself; `free-tier` sends the *same* string
and misses. The cache key is scoped by `tenant_id`, so two tenants cannot reach each other's
entries even on an exact SHA-256 match — which is the strongest form of this check, because an exact
match is the case a tenant-blind implementation would leak on first.

### Streaming honesty — tokens must arrive progressively

```
  PASS  content-type is text/event-stream            (text/event-stream; charset=utf-8)
  PASS  multiple data lines                          (24 lines)
  PASS  the stream was NOT buffered and delivered at once
        (first at 16 ms, last at 453 ms, spread 437 ms, median gap 16 ms)
```

24 SSE `data:` lines spread over **437 ms**, with a median inter-chunk gap of 16 ms — matching the
mock's documented ~20 ms per token, within the scheduler tick. A buffered "stream" would show all 24
lines arriving in one burst with a spread near zero, so the spread is the assertion that matters
rather than the line count.

### Budget edge — one request through, then a clean logged rejection

```
    spent before: 0.0 of budget 1e-05
    request 1 -> 200, request 2 -> 402
  PASS  first request is served                      (200)
  PASS  second is 402 budget_exceeded, not 429       (402)
  PASS  error type is budget_exceeded                (budget_exceeded)
  PASS  no Retry-After, because waiting cannot help
  PASS  the rejection itself is logged                (rejected_budget)
```

The *second* request is the rejected one, and that is correct rather than off by one: admission
checks spend **already recorded**, so the first request is admitted against a zero balance and is
what exhausts the $0.00001 budget. Three details beyond the status code:

- **402, not 429.** A budget is not a rate limit. `docs/API_CONTRACT.md` separates them and so does
  the error type, because the client action differs — wait, versus raise the budget.
- **No `Retry-After`.** A rate limit gets one because waiting fixes it; a monthly budget does not,
  because waiting until next month is not what the header means. Emitting one would be advice that
  does not work.
- **The rejection is logged**, with `status: rejected_budget` and the same `x-request-id` the client
  received. A refused request that leaves no trace is the one an operator cannot explain.

### Secret hygiene — no provider key in any client-visible surface

Every admin response, both error bodies, the ops console HTML, and `/readyz` were fetched and
searched:

```
  PASS  absent: mock-key            (provider API keys)
  PASS  absent: prism-sk-search-1a2b3c
  PASS  absent: prism-sk-free-7g8h9i
  PASS  absent: prism-sk-budget-demo-0j1k2l
  PASS  absent: <the admin token>
  PASS  absent: 9001                (provider ports)
  PASS  absent: 9002
  PASS  absent: 127.0.0.1           (provider base URLs)
```

Surfaces covered: `/admin/usage`, `/admin/logs?limit=100`, `/admin/cache/stats`,
`/admin/providers/health`, `/admin/keys`, `/console`, `/readyz`, a 404 unknown-model error body, and
a 401 bad-key error body. Nothing carries a provider credential, a virtual key, a base URL or a
port. Provider *names* do appear — via `x-prism-provider` and in the admin plane — and that is
deliberate: they are already visible to any caller through the response header, so listing them
behind the admin token discloses nothing new. `docs/DATA_MODEL.md:44` is about credentials.

This is also pinned by tests rather than only checked by hand: `tests/test_console.py` asserts the
console page carries no key material and no prompt text, and the provider tests assert that a
transport failure's message never quotes the credential that was sent.

---

## 10. Known limitations

The full ledger is the README's **[Known limitations](../README.md#known-limitations)** section,
maintained continuously as work proceeded rather than assembled at the end. The ones most relevant
to this report:

- **Rate-limit state is in memory, so a restart forgives outstanding usage**, and a second process
  would double every tenant's effective limit. `docs/DATA_MODEL.md:118` permits this explicitly;
  what it requires is that the counter be race-safe *within* the process, which §3's two bursts
  demonstrate and `tests/test_ratelimit.py` pins under thread contention. Redis is the
  multi-process answer and is not built.
- **No total request deadline** — §8. Bounded by chain depth × the read timeout.
- **No circuit breaker.** `degradation` thresholds are parsed, reported with a verdict, and not
  enforced (`enforced: false`). A sustained outage pays the full failover cost on every request.
- **The similarity scan is linear in the tenant's cached entries**, measured in §6 at ~0.06 ms per
  entry, and capped at the newest 500. The cap bounds the latency at ~30 ms and converts the problem
  into a correctness one: on a busy tenant a valid older match is silently missed. `pgvector` fixes
  both halves; raising the cap only trades one for the other.
- **The headline cache hit rate has cache-disabled tenants in its denominator** — §5. The requests
  do reach the cache, and the alternatives break a stated invariant, so it is documented rather
  than silently adjusted.
- **The router's exemplar set is 52 prompts written by hand**, so 95% is "beats length by a wide
  margin on the only held-out set available", not "95% accurate in general".
- **`route_009` routes to the expensive tier and shouldn't** — §4. Left unfixed because the fix
  would be fitting the answer key.
- **The length baseline scores 40%, not the ~60% the guide predicts** — §4. Reported as measured.
