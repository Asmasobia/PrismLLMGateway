# Day 4 design log — smart routing

What was built, in what order, why, and what each decision cost. Written alongside the code rather
than reconstructed afterwards, so the reasoning is the real reasoning.

Companion documents:

- [DAY1_DESIGN_LOG.md](DAY1_DESIGN_LOG.md) — the spine: config, errors, schema, the concurrency proof
- [DAY2_DESIGN_LOG.md](DAY2_DESIGN_LOG.md) — the data plane, and the length baseline this slice replaces
- [DAY3_DESIGN_LOG.md](DAY3_DESIGN_LOG.md) — resilience and streaming
- [DESIGN_NOTES.md](DESIGN_NOTES.md) — measured numbers (cosines, thresholds, the router's scores)
- [../README.md](../README.md) — how to run it; **Known limitations** is the honest ledger

---

## Contents

- [1. Scope: what Day 4 is and is not](#1-scope-what-day-4-is-and-is-not)
- [2. File map](#2-file-map)
- [3. Build order and why that order](#3-build-order-and-why-that-order)
- [4. Design decisions](#4-design-decisions)
- [5. The three proofs](#5-the-three-proofs)
- [6. Verification evidence](#6-verification-evidence)
- [7. Two earlier decisions reversed](#7-two-earlier-decisions-reversed)
- [8. Anticipated review questions](#8-anticipated-review-questions)
- [9. Known weaknesses, stated plainly](#9-known-weaknesses-stated-plainly)

---

## 1. Scope: what Day 4 is and is not

**Is:** replacing the length heuristic behind the `auto` alias with a semantic classifier, and
building the one command that grades it.

Concretely: an embedding-space kNN vote over 52 self-authored labelled exemplars; an *ask extraction*
front end that reduces a long prompt to the part that actually asks something; a `Classifier`
Protocol so the router's method is injectable and the length heuristic survives as a measurable
baseline; `scripts/routing_eval.py`, which scores a dataset in one command with no gateway, no
database and no network; and the test lane that can say something about quality rather than plumbing.

**Is not:**

- **Not the semantic cache.** This slice shares the embedding layer with it and warms the model at
  startup for it, but `prism/api/chat.py` still hard-codes `context.cache = "miss"`.
- **Not an LLM-based classifier.** Section 4 argues why, and the argument is about latency and cost
  on the request path, not about accuracy.
- **Not a trained model.** No fitting, no weights, no `scikit-learn`. The "training set" is 52
  prompts I wrote, and the only numbers chosen are `k` and a softmax temperature.
- **Not per-tenant routing policy.** Every tenant that asks for `auto` gets the same classifier.
- **Not a routing cache.** Identical prompts are embedded twice if they arrive twice.

The single most important constraint on this slice is a rule from
[DESIGN_NOTES.md](DESIGN_NOTES.md): **never tune against `data/routing_eval.jsonl`.** It is the
answer key. Everything about how this slice was *built* — the separate dev set, the order operations
were done in, what the tests are allowed to assert — follows from taking that seriously. A router
that scored 100% by being iterated against the eval would be worth nothing and would be
indistinguishable, from the outside, from one that had earned it.

---

## 2. File map

New:

| File | Lines | Owns |
|---|---|---|
| `prism/router_exemplars.py` | 311 | The 52 labelled exemplars, their shape tags, and the rules they were authored under |
| `tests/data/router_dev_set.jsonl` | 20 | The **only** dataset the router may be tuned against |
| `scripts/routing_eval.py` | 191 | One command, one number. Reads the answer key; nothing under `prism/` does |
| `scripts/fetch_model.py` | 220 | Fetch or SHA256-verify the vendored artifact. Verification runs offline and passes |
| `tests/test_router_semantic.py` | 467 | 31 tests: ask extraction, the vote, the exemplar set, and 4 quality tests |

Modified:

| File | Lines | What changed |
|---|---|---|
| `prism/routing.py` | 206 → 563 | Ask extraction, the `Classifier` Protocol, `SemanticClassifier`, `_label_for_score`; `resolve` became `async` |
| `prism/embeddings.py` | → 262 | `warm()` on the Protocol; `LazyEmbedding` replaced by `MemoEmbedder` |
| `tests/test_embeddings.py` | → 198 | Four memo tests replaced the lazy-holder tests; the session-scoped `real_embedder` fixture and the three `model`-marked tests behind it |
| `prism/deps.py` | 83 → 107 | `EmbedderDep` and `ClassifierDep`, and the per-request memo they share |
| `prism/main.py` | 255 → 280 | The embedder and classifier are constructed, warmed and prepared in the lifespan; both are injectable |
| `prism/api/chat.py` | 377 → 382 | One `await`, one dependency, and a comment about where in the order it sits |
| `tests/conftest.py` | 310 → 359 | The probe app pins the length baseline; the session-scoped `real_embedder` fixture |
| `tests/test_routing.py` | → 144 | Ten call sites became `await`; the semantic material moved out to its own module |
| `tests/test_dispatch.py` | 337 → 342 | Its `chain` fixture became `async` |
| `pyproject.toml` | → 67 | The `model` marker; an explicit ruff rule set; the provided scripts excluded from lint |
| `requirements.txt` | → 36 | `ruff` pinned — it had been configured for two slices without ever being installed |

`prism/db/models.py`, `prism/audit.py`, `prism/dispatch.py` and the provider layer are untouched by
the routing work. `request_log.route_reason` was sized and shipped on Day 2 for exactly this, so the
richer reason string needed no schema change.

---

## 3. Build order and why that order

```
embeddings (MemoEmbedder + warm) → exemplars → routing: extract_ask
      → routing: SemanticClassifier → routing: resolve goes async
      → deps + main (wiring) → api/chat (one line)
      → dev set → iterate → sweep → score the held-out eval ONCE
```

Four properties of that order were deliberate:

**The exemplars were written before any dataset was read.** Not before the *eval* — before the dev
set too. Authoring the exemplars first means they encode a theory of what makes a prompt hard
(section 4), rather than a memory of which cases were failing. Once you have seen failures, you
cannot un-see them, and every exemplar you add afterwards is partly a patch.

**The dev set was written after the exemplars and before the first measurement.** Twenty cases, ten
per tier, including eight traps of my own construction. Its purpose is to be the thing I am allowed
to overfit, so that the held-out score means something. Two exemplar gaps it caught are in section 6.

**`resolve` went async before the wiring, not after.** Making a signature async is a change that
propagates: ten call sites in `tests/test_routing.py` and a fixture in `tests/test_dispatch.py`.
Doing it as its own step, with the suite green before and after, kept that mechanical breakage
separate from the semantic work. Interleaved, an `AttributeError: 'coroutine' object has no
attribute 'chain'` looks like a routing bug.

**The held-out eval was scored last, exactly once, after the hyperparameter sweep was already
closed.** Not "mostly once". The sweep (section 6) ran on the dev set, k=5/T=0.05 was already the
choice and already on a plateau, so nothing changed as a result of it — and then the eval was run.
Any other order and the reported 95% would be a number about the eval rather than about the router.

---

## 4. Design decisions

### 4.1 kNN over authored exemplars, not an LLM call

The obvious way to classify difficulty is to ask a model. It is also the wrong way here, for a
reason that has nothing to do with accuracy: **the classifier sits in front of every `auto` request,
so its latency and cost are added to every `auto` request.** A gateway whose job is to route cheap
prompts to a cheap model, by first paying for an LLM call, has spent the saving before it makes it.
The classifier must be cheaper than the difference between the tiers it is choosing between, and an
upstream call is not.

The three real candidates:

| Method | Cost per request | Why not chosen |
|---|---|---|
| Keyword / regex rules | ~0 | Brittle in exactly the interesting cases: "prove", "explain" and "why" appear in trivial prompts, and the hardest prompts often contain no cue word at all |
| Trained classifier (logistic regression on embeddings, say) | one embedding | Needs labelled training data. I have 52 hand-written examples; fitting 384 weights to 52 points is a way to memorise them |
| **kNN over exemplars** | **one embedding** | Chosen |

kNN's real advantages here are operational rather than statistical. Adding a case is appending one
line — no retraining step, no artifact to version, no risk that a retrain silently moves cases that
used to work. And **it can explain itself**: the neighbours that voted are named prompts with shape
tags, so `route_reason` can say `near=proof,proof,arithmetic-check`, which is an explanation an
operator can act on. A logistic regression's explanation is 384 coefficients.

It also reuses the embedding model the cache already requires. No second artifact, no second
warm-up, no second thing that can be missing at startup.

### 4.2 Exemplars carry a difficulty float, not a tier name

```python
Exemplar("What does a database index do?", 0.0, "definition")
Exemplar("Prove that no largest prime exists.", 1.0, "proof")
```

The obvious encoding is the tier: `"fast"` / `"smart"`. It is wrong because the tier vocabulary
belongs to the *config*, not to the classifier. `data/gateway_config.sample.json` declares
`route_by_difficulty: {simple: ..., complex: ...}`, and a different deployment is free to declare
three tiers with different names. Exemplars labelled `"fast"` would have hard-coded this
repository's own sample config into the classification logic.

So exemplars carry difficulty on [0, 1] and `_label_for_score` maps the weighted score onto whatever
ordered labels the config declares, by slicing the unit interval into as many bands as there are
labels. Two labels split at 0.5; three split into thirds. It also returns the distance to the
nearest boundary as `margin`, which is what separates "obviously hard" from "0.51, could have gone
either way" in the reason string.

The exemplars themselves are only ever 0.0 or 1.0, and a test enforces that. A hand-written 0.6
would be a number nobody could defend under questioning; the intermediate values are produced by the
weighted vote, which is the one place they can be derived rather than asserted.

### 4.3 Ask extraction: length lies in both directions

Two failure shapes matter, and they pull in opposite directions:

- **Short but hard.** "Prove that the square root of two is irrational." Nine words, needs the
  expensive model. A length threshold sends it to the cheap one — a *quality* failure, the kind a
  user notices.
- **Long but trivial.** Forty numbers pasted in, then "which is the largest?". Fifty-four words,
  needs nothing. A length threshold sends it to the expensive one — a *cost* failure, the kind that
  shows up on an invoice.

Embeddings alone do not fix the second shape, because an embedding of a prompt that is 90% payload
is mostly an embedding of the payload. So long prompts are reduced to their **ask** before
classification:

1. Drop quoted spans of 8+ words (backticks, triple quotes, straight and curly quotes).
2. Split into sentences, and keep only those carrying a question mark, an ask cue, or an
   interrogative opening.
3. If nothing survives, keep the whole prompt and tag it `no-cue`.
4. Clip to 60 words, keeping head *and* tail.

Three details that are each a decision:

**The ask cue must appear at a clause head** — start of a sentence, or after a comma, colon, `and`,
`then`, `also`, `please` or `so`. Without that, "the report will explain the outage" matches on
"explain" and a sentence of pure context is promoted to the ask. With it, "Given the config above,
work out what that does to tail latency" is recognised even though the instruction does not begin
its sentence.

**Quote detection uses lookarounds so an apostrophe cannot open a quotation.** `'[^']+'` matches
from the apostrophe in `last week's` to the one in `before's`, deleting the middle of the prompt and,
on a bad day, the question at the end of it. There is a test named for exactly that.

**The clip keeps head and tail, not the first 60 words.** Instructions cluster at both ends —
"Explain the following: …" and "… so which one should we pick?". Keeping only the head throws away
the question in the second shape.

**Extraction is skipped entirely below 30 words**, because on a short prompt every step is a chance
to delete the ask and there is no payload to gain by removing it.

### 4.4 The same front end on both sides of every comparison

The exemplars are passed through `extract_ask` at `prepare()` time, not just incoming prompts.

Four exemplars deliberately carry pasted payload, because "long but trivial" is a shape the set has
to represent. If the query is reduced to its ask and the exemplar is not, then every comparison is
between an extracted ask and an unextracted wall of text — two different kinds of object, and the
cosine between them is dominated by that difference rather than by difficulty. Extracting both sides
costs nothing (it happens once, at startup) and makes the comparison well-posed.

This is the same principle as normalising vectors before comparing them, applied one level up.

### 4.5 `route_reason` explains without quoting

`route_reason` is written to `request_log`, so anything in it is retained. A reason that quoted the
prompt would be a second copy of the data the gateway is otherwise careful with, in the field an
operator is most likely to paste into a ticket. So it carries facts *about* the classification:

```
knn=exemplars(k=5,n=52) score=0.17 margin=0.33 top=0.67
near=payload-lookup,snippet,payload-lookup ask=5/54w dropped=payload
```

Five words of the 54 decided the tier; the three nearest exemplars are all cheap shapes; the top
cosine was 0.67. That is enough to defend or dispute the decision without seeing the prompt. The
shape tags exist for this: they are the vocabulary the reason speaks in, which is why every exemplar
must have one and a test enforces it.

`ask=5/54w dropped=payload` in particular answers the question a surprising tier always raises —
"what did it actually look at?"

### 4.6 A `Classifier` Protocol, and why `bind()` exists

```python
class Classifier(Protocol):
    async def classify(self, prompt: str, labels: list[str]) -> tuple[str, str]: ...
    def bind(self, embedder: Embedder) -> Classifier: ...
```

Two implementations: `LengthClassifier` (the Day 2 heuristic, kept) and `SemanticClassifier`. The
Protocol buys three things — the baseline stays *runnable* so `scripts/routing_eval.py` can measure
what the semantic router is worth; the general test suite pins the length classifier so an exemplar
edit cannot break the metering tests; and `create_app` takes both an embedder and a classifier, so
tests build an app with no ONNX in it.

`bind()` solves a scoping mismatch. The classifier is **app-scoped**: it embeds 52 exemplars once, at
startup. The memo over the embedder is **request-scoped**: within one request the router and the
cache should share embeddings, but across requests nothing should accumulate. Threading a
per-request embedder through `resolve` as a parameter would have put a caching concern into the
signature of every routing function. Instead `bind()` returns a lightweight twin that shares the
prepared exemplar vectors and swaps in the per-request embedder. FastAPI's per-request dependency
caching is what makes `EmbedderDep` resolve to one memo per request.

### 4.7 Why the softmax weighting, and why k=5

Cosines in this space sit in a narrow band — roughly 0.55–0.95 even for unrelated prompts — so an
unweighted majority among k neighbours throws away most of the signal, and raw-cosine weighting
barely separates the best neighbour from the fifth. Weights are `exp((sim - best) / T)`: a softmax,
shifted so the largest exponent is zero, which is mathematically identical and cannot overflow at
small T.

T = 0.05 makes the vote strongly favour the nearest neighbours while letting a close second and third
matter. k = 5 was chosen from the dev-set sweep in section 6, where it is both optimal *and* on a
plateau — the same score at three of four temperatures. A hyperparameter that is only optimal at one
setting is a hyperparameter that was fitted.

### 4.8 The `model` test lane

A third of this slice's value is unprovable without the real model, and the real model is 65 MB of
ONNX that takes seconds to load. So quality tests are marked `model` and skip when
`PRISM_MODEL_CACHE` is absent, exactly as the Postgres tests skip.

Two things make this honest rather than a way to hide failures. The quality tests score the **dev
set** only — a floor of 0.9 and "beats the length baseline" — because a floor on the held-out eval
would turn every future exemplar edit into an exercise in fitting the answer key. And the fixture
reads `PRISM_MODEL_CACHE` from the environment *and then from `.env`*, because the first version read
only the environment and skipped the entire lane on a machine where the model was present. That is
the worst possible failure mode for a skip-based lane: a green run that proved nothing.

The gateway itself does the opposite and refuses to start. A test that cannot run is an inconvenience;
a served request that silently lost semantic routing is a different kind of problem.

---

## 5. The three proofs

**1. The exemplars are authored, not copied.**
`test_no_exemplar_is_lifted_from_the_held_out_eval` reads the eval's *prompts* — never its
`expected_tier` — and asserts no exemplar reproduces one. Verbatim overlap would make the reported
held-out accuracy meaningless, and it is precisely the sort of thing that creeps in during a late
edit when you are looking for one more example of a shape.

**2. Topics appear at both ends of the difficulty scale.**
Embeddings are dominated by subject matter, so a topic present only among the hard exemplars teaches
the router that the *topic* is hard. Two tests enforce it: a lexical one over surface forms, and a
`model`-marked one that checks each topic's easy/hard pair is closer to each other than an unrelated
pair. The second exists because the first can only check spelling — the container topic is written
"docker run" on the easy side and "containers" on the hard side, and that pair scores 0.5817 against
0.5624 for a pair that *does* share a word. Requiring a shared token would have been asserting a
lexical proxy for a property that lives in the embedding space.

**3. The exemplars are embedded once, and binding does not re-embed them.**
`test_the_exemplars_are_embedded_once_no_matter_how_many_requests` counts calls into a fake embedder:
52 at `prepare()`, then exactly one more per request. `test_binding_shares_the_prepared_vectors`
asserts the per-request embedder sees only the incoming prompt. Together they pin the claim the whole
design rests on — that semantic routing costs *one* embedding per request, not 53.

---

## 6. Verification evidence

### The suite

**261 passed**, no failures and no skips on this machine (Postgres up, model present). Thirty-one of
those are the new `tests/test_router_semantic.py`, and `tests/test_embeddings.py` now holds 19.
Seven tests across the two files are `model`-marked, and all seven ran.

```
python -m pytest                                   # 261 passed
python -m pytest -m "not postgres"                 # no database needed
python -m pytest -m model                          # 7 passed: the quality lane
python -m ruff check prism tests scripts           # All checks passed!
python scripts/fetch_model.py                      # all five files match the pinned revision
```

### The hyperparameter sweep — dev set only

Accuracy on `tests/data/router_dev_set.jsonl` (20 cases):

|  | T=0.02 | T=0.05 | T=0.1 | T=0.25 |
|---|---|---|---|---|
| k=1 | 90% | 90% | 90% | 90% |
| k=3 | 90% | 90% | 90% | 90% |
| **k=5** | 90% | **95%** | **95%** | **95%** |
| k=7 | 90% | 95% | 90% | 90% |
| k=9 | 90% | 95% | 90% | 90% |

k=5, T=0.05 was already the shipped setting before this table existed, and the table changed nothing.
Two things are worth reading off it: the chosen point sits on a **plateau** rather than a spike, and
k=1 (nearest neighbour, no weighting at all) already gets 90% — the vote is worth one case out of
twenty, not the difference between working and not.

### The held-out score

`python scripts/routing_eval.py`, run once against `data/routing_eval.jsonl`:

| Classifier | Correct | Accuracy |
|---|---|---|
| Semantic (kNN, k=5, T=0.05) | 19/20 | **95%** |
| Length baseline (24 words) | 8/20 | 40% |

Guide bar: 80% (`docs/EVALUATION_GUIDE.md:83`). **All eight trap cases pass** —
`route_011/012/013/019/020` short-but-hard and `route_006/007/010` long-but-trivial — which is the
result that matters, because those eight are what length cannot do by construction.

Two numbers here are reported rather than fixed:

- **The single miss is `route_009`, "Is 91 divisible by 7?"**, routed `smart`. Neighbours: `proof`,
  `proof`, `arithmetic-check`. Number-theory vocabulary is shared between checking one instance and
  proving a general claim, and nine words is not enough for the embedding to separate them. Adding a
  "check this divisibility" exemplar would fix it and would be fitting the answer key. The error
  direction is the safe one: it overspends rather than under-answers.
- **The length baseline scores 40%, not the ~60% `docs/EVALUATION_GUIDE.md:82` predicts.** The
  threshold is 24 words, chosen on Day 2 by reading prompts, and most of this eval's hard cases are
  short. Tuning the baseline up would have meant fitting the answer key to make my own comparison
  look fairer.

### What the dev set caught before the eval was ever run

Two exemplar-set gaps, each fixed by one exemplar carrying a comment naming the rule it applies:

| Dev case | Symptom | Cause | Fix |
|---|---|---|---|
| `dev_002` "What is a database index?" | routed `smart` | The index topic appeared only among the hard exemplars, so the *topic* read as hard | Added `"What does a database index do?"` at 0.0 |
| `dev_012` Fermi estimation | routed `fast` | "How many X in a Y" is also the grammar of unit conversion, which is an easy shape | Added a coffee-consumption estimation exemplar at 1.0 |

The first of these is what rule 1 — topics on both sides — is *for*, and it was found by the dev set
rather than by inspection. That is the argument for having a dev set at all.

---

## 7. Two earlier decisions reversed

**`LazyEmbedding` → `MemoEmbedder`.** The Day 3 embedding layer had a `LazyEmbedding`: a holder for
one text that embedded on first access, so a request that never needed a vector never paid for one.
That was right when the cache was the only consumer. This slice added a second consumer that embeds
a *different* string — the router embeds the extracted ask, the cache keys on the full prompt — and
a single-text holder cannot represent two. `MemoEmbedder` memoises by text instead: one call per
distinct string, zero calls when nothing needs embedding, and it satisfies `Embedder` so it can be
dropped anywhere the real one goes. It also exposes `embedded`, which is how the tests count calls.

The lazy design was not wrong; it was right for one consumer and became wrong at two.

**`resolve` became `async`.** It was synchronous by choice: resolution was dictionary lookups, and an
`async def` that never awaits is noise. Semantic classification has to await an embedding, and the
alternative — a second, async resolution path for `auto` — is precisely the mistake
`test_a_router_inherits_the_full_fallback_chain` exists to prevent. A router that returned only its
tier's primary would work in every demo and lose failover silently. One path for all three kinds of
request is worth an `await` on the two that do not need one, and `tests/test_dispatch.py`'s `chain`
fixture carries a comment saying so.

A third, smaller reversal: `[tool.ruff]` had been in `pyproject.toml` for two slices with `ruff`
never installed or pinned, so its declared 92-column limit had never been enforced and 51 lines
exceeded it. The limit is now 100 — where this codebase actually sits — the rule set is named
explicitly rather than inherited from whichever ruff is installed, the four provided scripts are
excluded because they are not mine to edit, and `ruff` is pinned in `requirements.txt`. Reflowing 51
lines across 20 files to defend the aspirational 92 would have been a whitespace diff over nearly
every file, submitted alongside the slice it would have made unreviewable.

---

## 8. Anticipated review questions

**"95% on twenty cases. Isn't that one lucky case away from 90%?"**
Yes, and the confidence interval on 20 samples is wide — roughly 75–99% at 95% confidence. The
number I would defend is not "95%" but "beats the length baseline by 11 cases out of 20, and gets all
eight of the constructed traps right". The trap results are the ones with a mechanism behind them:
they pass because payload is stripped before classification and because short-hard shapes are
represented in the exemplar set, not because of how the sample fell.

**"You wrote the exemplars and you wrote the dev set. Isn't that circular?"**
For the dev set, yes, deliberately — that is what it is for, and it is why the eval is scored
separately and once. The eval was written by someone else, and 19/20 on it is the claim. The
structural defences against circularity are in section 5: no exemplar reproduces an eval prompt, and
nothing under `prism/` can read `expected_tier`.

**"What stops someone tuning against the eval next week?"**
Nothing technical, and that is honest. What exists is: the rule stated in three places
(`DESIGN_NOTES.md`, the `routing_eval.py` docstring, the `test_router_semantic.py` docstring), the
absence of any test that asserts on the eval — so a "make the suite green" instinct cannot drag
anyone toward it — and the overlap test. A pre-commit hook that failed on edits to
`router_exemplars.py` in the same commit as a routing_eval run would be a real defence and is not
built.

**"One embedding per `auto` request. What does that cost?"**
Measured on this machine, once the model is warm: **median 3.8 ms** for a short prompt, range
3.5–5.3 ms over 20 runs, in a thread. Set against the difference between the `fast` and `smart`
tiers, and against a provider call measured in hundreds of milliseconds, it is not close — that is
the whole economic argument for the slice. What it does cost is *scalability shape*: ONNX holds the
GIL, so throughput is bounded
by the default thread-pool executor, and that bound now sits on the routing path and not just the
cache path. Measuring it under concurrency is Day 5's load-test work, and it is in Known limitations
until then.

**"Why is the classifier app-scoped but the embedder per-request?"**
Because they have different lifetimes for different reasons. The 52 exemplar vectors are immutable
and expensive, so they are computed once. The memo is a per-request deduplication between the router
and the cache, and a process-lifetime memo would be an unbounded cache of every prompt the gateway
had ever seen — a memory leak with a privacy dimension. `bind()` is what lets one object have both.

**"What happens if the exemplar set is empty or the model fails to load?"**
An empty exemplar set degrades to the length baseline and says `no exemplars` in the reason — a
configuration mistake is not a reason to refuse a request. A missing model is the opposite: the
gateway refuses to start, naming `PRISM_MODEL_CACHE`. The asymmetry is deliberate. A misconfigured
exemplar set still routes every request somewhere sensible; a missing model means every `auto`
request would silently fall back, and silently serving a degraded product is worse than not starting.

**"Why does `route_reason` not include the prompt? It would be more useful."**
It would, and it would also mean every routed request writes a second copy of its prompt into
`request_log`, which is the table an operator queries casually and exports. The shape tags plus
`ask=5/54w` were designed to answer the same questions — what did it look at, what did it look
like — without that. If a specific prompt needs investigating, the request id is in the row.

**"Ask extraction throws away part of the prompt. What if it throws away the wrong part?"**
Then a prompt is classified on the wrong basis, and the reason string says what survived, so it is
diagnosable rather than mysterious. The failure is bounded in one direction on purpose: when nothing
recognisable as an ask survives, the *whole* prompt is classified and tagged `no-cue`, because
dropping a real instruction is the expensive failure and classifying some payload is the cheap one.
The known hole — payload with no sentence boundaries at all — is in section 9 and has a test.

---

## 9. Known weaknesses, stated plainly

- **Routing quality is bounded by 52 prompts I wrote.** The eval is 20 cases from the same provided
  pack the exemplars were reasoned about. A genuinely different traffic mix could do materially worse
  and nothing in this build would detect it. The honest claim is "beats length by a wide margin on
  the only held-out set available", not "95% accurate in general". A production answer collects real
  routed prompts, has them labelled, and retrains; none of that exists here.
- **No confidence-based abstention.** `margin` is computed and reported and then not used. A
  principled gateway would route a low-margin prompt to the more expensive tier — cheap insurance
  against exactly the ambiguous cases — and the threshold for that would be a config value. Not
  built, because choosing the threshold needs a distribution of margins from real traffic, and
  inventing one from 20 cases would be the same mistake as fitting the eval.
- **Ask extraction cannot split payload with no sentence boundaries.** A space-joined token run with
  a question appended is one "sentence" containing the ask, so it is clipped rather than dropped.
  It degrades rather than breaks — the clip keeps head and tail and the question is at the tail — and
  real pasted payload is newline-separated. Pinned by
  `test_payload_with_no_sentence_boundaries_is_clipped_rather_than_dropped` so it is a known decision
  rather than a surprise.
- **`route_009` is a known wrong answer, left in.** See section 6.
- **English only.** The cue lists, the interrogative openings and the exemplars are all English, and
  the embedding model is `bge-small-en`. A non-English prompt gets no ask extraction worth the name
  and is classified against exemplars in another language.
- **Every `auto` request pays an embedding, with no memo across requests.** Two identical prompts a
  second apart are embedded twice. A small LRU keyed on the extracted ask would remove most of that
  for repetitive traffic and is not built; the semantic cache slice will make the same question
  worth revisiting for the full prompt.
- **No per-tenant routing policy.** Every tenant that asks for `auto` gets the same classifier, the
  same exemplars and the same k. `--classifier length` exists only in `scripts/routing_eval.py`,
  so a tenant who would rather have predictable cheap routing than accurate routing cannot ask for it.
- **The throughput bound moved onto the routing path.** ONNX holds the GIL and every embed runs in
  `asyncio.to_thread`, so concurrent `auto` requests contend for the default thread-pool executor.
  Unmeasured under load until Day 5.
- **`scripts/fetch_model.py --fetch` has never been run.** The verify half is exercised and passes,
  including its failure paths; the network transfer is not.
