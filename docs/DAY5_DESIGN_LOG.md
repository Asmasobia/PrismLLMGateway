# Day 5 design log — the semantic cache and the admin plane

What was built, in what order, why, and what each decision cost. Written alongside the code rather
than reconstructed afterwards, so the reasoning is the real reasoning.

Companion documents:

- [DAY1_DESIGN_LOG.md](DAY1_DESIGN_LOG.md) — the spine: config, errors, schema, the concurrency proof
- [DAY2_DESIGN_LOG.md](DAY2_DESIGN_LOG.md) — the data plane, the header contract, the accounting
- [DAY3_DESIGN_LOG.md](DAY3_DESIGN_LOG.md) — resilience and streaming
- [DAY4_DESIGN_LOG.md](DAY4_DESIGN_LOG.md) — smart routing, and the embedding layer this slice reuses
- [DESIGN_NOTES.md](DESIGN_NOTES.md) — the measured numbers, and the "as built" tables for both halves
- [../README.md](../README.md) — how to run it; **Known limitations** is the honest ledger

---

## Contents

- [1. Scope: what Day 5 is and is not](#1-scope-what-day-5-is-and-is-not)
- [2. File map](#2-file-map)
- [3. Build order and why that order](#3-build-order-and-why-that-order)
- [4. Design decisions](#4-design-decisions)
- [5. The five proofs](#5-the-five-proofs)
- [6. Verification evidence](#6-verification-evidence)
- [7. Earlier decisions revisited](#7-earlier-decisions-revisited)
- [8. Anticipated review questions](#8-anticipated-review-questions)
- [9. Known weaknesses, stated plainly](#9-known-weaknesses-stated-plainly)

---

## 1. Scope: what Day 5 is and is not

Two slices, in one day, because the second reads what the first writes.

**Slice 7 — the semantic cache.** A per-tenant response cache that answers paraphrases: an exact
hash path, then an embedding-space nearest-entry search above a per-tenant threshold, then two
rejection-only guards, then serving — non-streaming or replayed as SSE. Plus a write-path volatility
guard, TTL expiry, and hit accounting.

**Slice 8 — the usage and admin API.** Five read-only endpoints over `request_log`, `cache_entries`
and `budget_periods`: usage, recent logs, cache statistics, provider health, key policy.

**Is not:**

- **Not the ops console.** The queries were deliberately split into `prism/usage.py` so a
  server-rendered page can call them without going through HTTP, and that page is not built.
- **Not a schema change.** `prism/db/models.py` is untouched — still the 320 lines it has been since
  Day 2. `cache_entries` was designed on Day 1 for exactly this, including `hit_count`,
  `last_hit_at`, `expires_at` and `uq_cache_exact`; `request_log.cache` and
  `RequestStatus.CACHE_HIT` likewise. Four days of "the cache will need this" turned out to be
  right, and the one thing that would have made this slice a migration was already there.
- **Not a circuit breaker.** `/admin/providers/health` *reads* the `degradation` block and publishes
  a verdict. Nothing routes on it, and the response says `enforced: false` out loud.
- **Not key management.** The admin plane is read-only. There is no create, rotate, disable or
  budget-adjust endpoint; the seed file plus `scripts/init_db.py` is still the only way keys change.
- **Not a vector index.** Similarity is still a bounded per-tenant scan with cosine computed in
  Python. `docs/DATA_MODEL.md` allows it and Day 1 recorded the ANN index as deferred.
- **Not write-path caching for streams.** Cached answers can be streamed; streamed answers are never
  stored (section 4.13).

The constraint that shaped the whole cache half: **a false hit is the only failure in this gateway
that the caller cannot detect.** A rate limiter that over-admits shows up in a load test; a cache
that answers a slightly different question returns a plausible paragraph and nobody notices. Every
ordering decision in `prism/cache.py` follows from taking that seriously.

---

## 2. File map

New:

| File | Lines | Owns |
|---|---|---|
| `prism/cache.py` | 727 | Normalisation, the scope key, both guards, volatility, lookup/store/purge, SSE replay |
| `prism/usage.py` | 765 | Every admin query, as functions returning dataclasses — no HTTP, no rendering |
| `prism/api/admin.py` | 371 | Five read-only routes, one router-level auth guard, and the JSON shapes |
| `tests/test_cache.py` | 907 | 59 tests in three lanes: 32 pure, 25 against Postgres, 3 against the real model |
| `tests/test_admin.py` | 588 | 37 tests: 4 pure, 33 against Postgres and the full HTTP stack |

Modified:

| File | Lines | What changed |
|---|---|---|
| `prism/api/chat.py` | 382 → 589 | The lookup between routing and dispatch; `serve_cached`, `remember`, `replay`; the provider-header override |
| `prism/settings.py` | 98 → 106 | `PRISM_CACHE_TTL_SECONDS`, nullable, with the argument for defaulting to "never expire" |
| `prism/errors.py` | 137 → 143 | `NotFoundError` now also covers a `key=` selector matching no tenant. Docstring only |
| `prism/main.py` | 280 → 281 | `app.include_router(admin.router)` |
| `tests/test_chat.py` | 785 → 786 | One assertion: the Day 2 hard-coded `miss` is now a real one |
| `.env.example`, `README.md`, `docs/DESIGN_NOTES.md` | | The TTL variable, the five routes, the admin section, twelve new limitations |

**Unchanged, and that is the interesting column:** `prism/db/models.py`, `prism/deps.py`,
`prism/embeddings.py`, `prism/audit.py`, `prism/budget.py`, `prism/ratelimit.py`,
`prism/dispatch.py`, `prism/auth.py`, `tests/conftest.py`. The cache needed no new schema, no new
dependency wiring and no new fixture; the admin plane needed no new auth code. `require_admin` and
`tests/test_admin_auth.py` were written on **Day 1** and had no consumer for four days — this is the
slice that gave them one, and they needed no change to serve it.

---

## 3. Build order and why that order

```
cache: normalise/digest → split_conversation → scope
     → literals + literal_match → negated → is_volatile
     → CacheHit/Lookup → lookup → record_hit → store → purge_expired
     → served_target → replay_chunks
     → settings (TTL) → api/chat (serve_cached, remember, replay)

admin: usage: Window/resolve_window → find_tenant → usage_by_key/combine
     → recent_logs → cache_savings → provider_health → key_states
     → api/admin (five routes) → main (one line)
```

Five properties of that order were deliberate.

**The guards were written and measured before anything could store an entry.** `literals`,
`literal_match` and `negated` are ordinary text functions with no database and no model, so both
were measured against the real embedding model — 0.9620 for the negation pair, 0.9149 for the
amount pair — while the cache could not yet hold anything. Built the other way round, each guard
would have been a patch on an observed false hit, and a guard authored in response to one failure
generalises to that failure only. The [DESIGN_NOTES](DESIGN_NOTES.md) table exists because the
numbers came first.

**`lookup` before `store`.** The reader defines the key. A writer written first invents a key shape
that seems reasonable and the reader then has to match it, which is how a cache ends up with a
`cache_key` nobody can explain. Writing the read path first meant `scope()` was designed against the
question "what must be identical for this answer to still be correct?" rather than "what do I
happen to have in scope at insert time?"

**The pure text layer, then the storage layer, then HTTP — and `prism/cache.py` never learned about
HTTP.** It takes a `Tenant`, a session, a question and an embedder, and returns a `Lookup`. Nothing
in it imports FastAPI, which is why the whole first lane of `tests/test_cache.py` (32 tests) runs
with no database, no model and no server.

**`prism/usage.py` before `prism/api/admin.py`, as separate modules rather than one.** The ops
console is the next deliverable and it needs the same aggregates without a round trip through its
own HTTP API. Splitting after the fact means either duplicating queries or having the console call
itself over the network; splitting first cost nothing, because the functions were going to exist
either way. It also forced a useful discipline: `usage.py` returns dataclasses with computed
properties (`hit_rate`, `total_tokens`, `status`), so the *rules* live next to the data and
`admin.py` only renames fields and formats money.

**The admin plane after the cache, not before.** Two of its five endpoints are about the cache, and
one of its sharper design questions — what the denominator of a hit rate should be — is only
answerable once the cache is actually sitting behind the rate limiter and the budget (section 4.18).
Built first, `/admin/cache/stats` would have reported over an empty table and the denominator
question would have looked like a matter of taste.

---

## 4. Design decisions

### 4.1 The scope is part of the key, not a filter on it

`uq_cache_exact` is `(tenant_id, cache_key, prompt_hash)`, and every read filters on the first two.
`docs/PRISM_PROBLEM_STATEMENT.md:66` calls a cross-tenant hit a data leak, and the defence against
it is not a remembered `WHERE` clause but the shape of the key: there is no query in the module that
can reach another tenant's rows, because `tenant_id` is bound before `cache_key` is even computed.

The `cache_key` itself folds in three things — the served tier, the conversation prefix and the
sampling parameters. The full argument is in [DESIGN_NOTES](DESIGN_NOTES.md#as-built); the part
worth restating is that folding a parameter into the *namespace* is strictly better than refusing to
cache parameterised requests. A `temperature=1.4` request still gets a cache, just its own one.

One implementation detail with a reason: the bare tier is returned unchanged for the common
single-turn, no-parameters request (`"fast"`), and a 16-hex digest is only appended when there is
something more to pin down (`"fast#a1b2…"`). That keeps `cache_key` readable in psql and in the
console for the majority of rows, which matters because it is the column an operator uses to work
out why two requests did not share an answer.

### 4.2 The conversation prefix is matched exactly; only the final turn is matched semantically

The two obvious ways to cache a multi-turn conversation are both wrong. Keying on the last user
message alone leaks context — "and what about Postgres?" means different things after different
histories, and serving one for the other is a false hit with no lexical warning at all. Embedding
the whole transcript instead makes every long conversation similar to every other long conversation,
because accumulated history dominates the vector and the actual question becomes a rounding error.

So `split_conversation` cuts at the final user turn: everything before it goes into the scope digest
and must match byte for byte, and only that turn is compared semantically. A follow-up can hit, but
only inside an identical conversation so far — which is exactly the condition under which the
earlier answer is still the right one. `test_a_follow_up_does_not_hit_across_conversations` pins it.

### 4.3 Both guards reject only, and that is the property that makes them safe

Neither guard can promote a candidate that similarity did not already accept. Three things follow,
and they are the reason two hand-written guards are defensible in front of a reviewer at all:

- A guard bug can cost a cache hit. It cannot invent one.
- Lowering the threshold does not weaken them. A deployment that drops to 0.85 to catch more
  paraphrases is exactly the one where the literal guard starts earning its place.
- They can be extended later without re-arguing the safety case.

The ordering is threshold first, then guards, and that is deliberate too: the guards are Python text
work and the threshold rejects most candidates for free, so the expensive check runs on the few that
survive.

### 4.4 Volatility is a *write*-path guard, because the threshold cannot do this job

`data/sample_requests.jsonl` ships `req_no_cache` — "What is the current status of the payments
service?" — and `docs/PRISM_PROBLEM_STATEMENT.md:67` asks for a decision. The decision: serve it
normally, never store it.

The reason it has to be a write-path guard is worth being precise about, because it is the one place
where a threshold argument fails completely. The danger with a status question is not a near-miss
against a similar prompt. It is the **same question asked twice** — which the exact-hash path answers
instantly, with a stale status, at similarity 1.0. No threshold, however high, touches that case. The
only lever is refusing to write the entry.

The guard is lexical (time words and phrases), it logs when it declines so "why does this never
cache?" is answerable from the outside, and it is deliberately narrow: bare `"now"` is **not** a cue,
because "now I understand, how do I…" is an ordinary cacheable question and treating it as volatile
would silently disable caching for a whole style of phrasing.
`test_bare_now_is_not_treated_as_volatile` exists so that stays a decision rather than an accident.

### 4.5 The exact path exists to avoid the semantic path

An identical prompt is one indexed lookup: no embedding, no scan, no threshold, no guards. That is
the common case in real traffic (a retried request, a polling page) and it is also the cheapest thing
the gateway can do — cheaper than the routing decision that precedes it.

Skipping the guards there is safe *because* the text is identical: byte-identical text has identical
literals and identical polarity by construction. It is safe with respect to staleness only because
volatile prompts were never stored in the first place — 4.4 and 4.5 hold each other up, which is why
they were written in the same sitting.

Normalisation for the hash collapses whitespace and case, so a trailing newline or a capital letter
does not produce a second copy of the same answer. The *stored* `prompt_text` is the original,
because that is the prompt that was actually asked and it is what the literal guard reads.

### 4.6 SHA-256, not `hash()`

Two reasons, and the second is the one that would actually have bitten. A collision would serve one
answer under a different question, which is the failure mode this module exists to prevent. And
Python's `hash()` is salted per process, so every stored entry would become unreachable after a
restart — a cache that silently empties itself on deploy, with a 100% hit rate in every test that
never restarts. `prism/embeddings.py:FakeEmbedder` uses sha1 for its buckets for the same reason,
and `prism/providers/fake.py` uses crc32 for its replies.

### 4.7 The candidate window is 500, newest first

Similarity is computed in Python over a per-tenant scan, so the scan is the one part of a lookup
whose cost grows with a tenant's history. Five hundred 384-dimensional dot products is well under a
millisecond and keeps a lookup bounded no matter how long a tenant has been running.

Newest-first rather than oldest-first because a live workload asks about recent things, so those are
the entries worth keeping in range. The cost of that choice is real and is in Known limitations: on a
busy tenant a valid old entry can fall out of the window and be missed while still occupying
storage. That is the point at which this needs pgvector, and the honest statement is that the
threshold has not been measured on this machine.

### 4.8 A cache hit costs zero tokens and zero dollars

The tokens in the entry were purchased once, on the request that created it, and charged then.
Charging them again would make `/admin/usage` report more spend than any provider will ever invoice,
which destroys the reconciliation `docs/EVALUATION_GUIDE.md` performs and the one
`scripts/load_test.py` prints.

What the cache *saved* is not lost by this: it is `hit_count × tokens` per entry, which is exactly
what `/admin/cache/stats` reports. So the two numbers an operator wants — what we owe, and what we
avoided owing — come from two different places on purpose, and neither is inferred from the other.

The consequence is one reconciliation gap that is real and is not a bug: a client summing the `usage`
blocks it received gets *more* than `/admin/usage` reports, by exactly `tokens_saved`, because a
replayed body carries the original response's token counts. It is documented in three places — the
route docstring, [DESIGN_NOTES](DESIGN_NOTES.md#one-reconciliation-gap-that-is-real-and-is-not-a-bug)
and the README — because `scripts/load_test.py:123` prints the client-side figure and invites the
comparison.

### 4.9 The header names where the bytes came from; the row says no provider was called

The two provided documents pull in opposite directions here and both are right about their own
artifact. `docs/API_CONTRACT.md:73` wants `x-prism-provider` to name "the upstream provider/model
that served it", which for a replayed answer is the provider that produced it originally.
`docs/DATA_MODEL.md:60` wants the *row* to leave `resolved_provider` null on a cache hit.

Honouring both is not a contradiction, it is the distinction between provenance and activity: no
provider was called, so usage grouped by provider must not attribute this request to one, while a
client reading the header still learns whose answer it is holding. `prism_headers(context,
provider=…)` overrides the header and only the header; `context.resolved` is explicitly cleared
first, because `route.primary` is still sitting there from the resolution that preceded the lookup
and leaving it would put a provider in the audit trail that this request never touched.

`served_target` falls back to the resolved primary when the stored provider is no longer in the
config, which is a real situation rather than a hypothetical: an entry outlives a config change.

This does narrow a Day 2 claim, and section 7 says so rather than leaving the two documents to
disagree quietly.

### 4.10 The cache write happens after the commit, in its own transaction, and cannot fail the request

`remember()` runs after the charge and the log row are committed, opens its own transaction, and
swallows every exception with a logged traceback. Two separate judgements:

- **Separate transaction**, because a failed cache insert must not roll back a charge that really
  happened. Sharing the transaction would make the cache capable of corrupting the accounting, which
  inverts their importance.
- **Swallowed**, because by this point the caller's answer is already settled. An exception here
  cannot be reported to them; it can only turn a served request into a 500 for a reason they have no
  stake in. A missing cache entry costs one upstream call later; a 500 costs the answer.
  `prism/audit.py:record_rejection` makes the same call for the same reason.

`ON CONFLICT DO NOTHING` against `uq_cache_exact` handles two identical prompts arriving
concurrently — normal behaviour for a retrying client, and not a reason to fail anything. First
writer wins, second is a no-op, and `test_storing_the_same_prompt_twice_writes_one_row` pins it.

TTL purging is opportunistic: it runs after a successful write, on the write path, so it never adds
latency to a read. Expired rows are already unservable — every query filters them — so this reclaims
space rather than affecting correctness. There is no sweeper and no size cap, which is in Known
limitations.

### 4.11 `Lookup` hands back the embedding rather than relying on the memo

`store()` needs the question's vector and `lookup()` has just computed one. The per-request
`MemoEmbedder` from Day 4 would make a second `embed_one` call free, so passing the vector back
looks redundant.

It is not, and the distinction is worth stating because it is the kind of thing that rots: depending
on a memo for **speed** is fine, depending on it for **correctness** is a hidden coupling. If the
memo were ever scoped differently — per app, per tenant, removed — the version that re-embeds would
silently start paying 4 ms of ONNX on every cache write, and nothing would fail. `Lookup.embedding`
is `None` exactly when the semantic path never ran (caching off, or enabled with no threshold), and
`remember()` returns early on `None` rather than embedding a string for an entry that tenant will
never read.

### 4.12 The cache sits after the rate limit and the budget

A cache hit still consumes a request from the key's per-minute quota, and a tenant that has exhausted
its budget gets a 402 even for a question the cache could have answered for free.

Both are deliberate. The rate limit protects *this* gateway's capacity, which a cache hit still
occupies — a replay is cheap, not free, and it holds a connection and a database session. A budget is
a spend ceiling the tenant asked for; continuing to serve past it on the grounds that these
particular answers happen to be free would make the ceiling mean something different depending on
cache contents, which is not a ceiling. It also keeps the pipeline honest in the other direction: the
cheap rejections run first, so nothing embeds a prompt it was never going to answer.

`test_a_cache_hit_still_consumes_rate_limit_quota` pins the first half and
`test_a_rate_limited_request_is_not_counted_as_a_cache_miss` pins the reporting consequence.

### 4.13 Cached answers can be streamed; streamed answers are not cached

The asymmetry is not laziness. Replaying a stored completion as SSE loses nothing — the chunks are
generated from the real body and framed exactly like `scripts/mock_provider.py` frames its own.
Going the other way means assembling a completion body out of deltas, and that body would be the
gateway's reconstruction rather than a provider's response: `system_fingerprint`, `logprobs` and any
provider extension present on the non-streaming shape simply are not in the delta stream. A later
non-streaming caller would then receive a subtly poorer object than the provider would have sent, and
would have no way to tell. `prism/providers/base.py:UpstreamCompletion` makes exactly this argument
about never rebuilding a body.

The cost is stated in Known limitations: a tenant whose traffic is entirely streaming gets no cache
entries at all.

Two smaller decisions inside the replay path. The row is written **before the first byte**, unlike
the live streaming path which must defer until the cost is known — here the body, its usage and its
cost are all already known, so deferring would only add a way to lose the row if the client
disconnects. And `x-prism-cost-usd` is omitted on a cached stream even though the cost is known and
is zero, because the contract omits it on streaming responses; emitting it on cached streams *only*
would turn the header's presence into a side channel for whether the cache answered, which
`x-prism-cache` already states outright.

### 4.14 `hit_count` is incremented in SQL

`hit_count = hit_count + 1` in the statement rather than read-modify-write in Python, for the same
reason `prism/budget.py` increments spend that way: two concurrent hits on the same entry would
otherwise both read the same value and one increment would vanish. The consequence here is only a
wrong number in `/admin/cache/stats` rather than lost money — but a hit rate nobody can trust is not
worth reporting, and the correct form costs nothing.

### 4.15 Usage is a query, never a stored aggregate

`docs/DATA_MODEL.md:14` calls the Usage Record "a query over `request_log`", and the reason is that a
usage row written alongside every log row is a dual write, and dual writes drift — usually silently,
usually in the direction that flatters the gateway.

So `prism/usage.py` contains no writes at all. The one denormalised counter that does exist,
`budget_periods.spent_usd`, is not treated as the source of truth: `/admin/keys` reports it *next to*
the total summed from `request_log` over the same month, with a `reconciles` boolean. Day 1's
docstring calls that counter "a cache with a correctness proof"; this is the slice where the proof
became something an operator can check without writing SQL.

Grouping is by the log row's **own** denormalised `team` and `key_prefix`, not by a join to
`tenants`. That is what makes a deleted tenant's traffic still appear in a historical window —
`tenant_id` cascades to null, the strings do not — and it is why the anonymous 401 bucket has a place
to live at all.

### 4.16 `key=` is a selector, not a secret

`docs/API_CONTRACT.md:119`'s example echoes a whole virtual key, which implies putting a live
credential in a query string. A query string lands in the access log, in shell history, in the
browser address bar and in the console's own URL, and none of those can be revoked.

So `find_tenant` accepts three forms in one `or_`: the team name (recommended, and unique), the key
prefix, or the full key resolved by `hash_virtual_key` — that last one purely for literal contract
compatibility. Only the prefix is ever echoed back. Zero matches is a 404 with
`code: "key_not_found"` naming the parameter; **more than one** match is a 400 naming the ambiguous
teams, rather than a silently-chosen first row, because "whose spend am I looking at?" is not a
question an ops tool should answer by accident.

`test_the_key_selector_accepts_a_team_a_prefix_or_the_key` asserts all three forms return byte-
identical bodies, which is the only way the deviation is actually compatible rather than nominally
so.

### 4.17 `served` / `rejected` / `failed` partition `requests`, and a test keeps the partition exhaustive

`requests` counts every logged row, including rejections, because `docs/DATA_MODEL.md:57` requires
those rows to exist and an API that quietly omitted them would disagree with the log it reads. But a
single total conflates "we served 900 and refused 100" with "we served 1000", so the three status
tuples partition it: served (`ok`, `cache_hit`), rejected (the five client-side refusals), failed
(`upstream_error`, `internal_error`).

The value of the partition is diagnostic: it is what lets an operator see *why* a client's own count
of successful calls is lower than `requests`, instead of suspecting the meter.

`test_the_status_partition_is_exhaustive` asserts the three tuples, concatenated, equal
`RequestStatus` exactly and contain no duplicate — so a status added later cannot silently vanish
from the API, which is the failure mode this kind of hand-maintained grouping has.

### 4.18 The hit rate's denominator is lookups, not requests

The cache sits behind the rate limit and the budget, so a rejected request never reached it. If the
denominator were all logged requests, a tenant's reported hit rate would **fall when it got rate
limited** — the cache would look like it was getting worse at the exact moment it was doing nothing
at all, and an operator would chase the wrong thing.

So `hit_rate` divides by rows with `status IN (ok, cache_hit)`: the requests that actually performed
a lookup. `test_a_rate_limited_request_is_not_counted_as_a_cache_miss` proves the premise before the
property — 11 requests on the free key produce `[200] × 10 + [429]`, and then `lookups == 10` while
`/admin/usage` still reports `requests == 11, rejected == 1`. Without the first assertion the test
would pass trivially on the day the rate limiter stopped firing.

Two time bases are labelled explicitly in the response rather than blended: hits, misses and hit rate
are **windowed**; entries, `tokens_saved` and `cost_saved_usd` are **lifetime**, because `hit_count`
on an entry is a running total with no per-day breakdown. Presenting a windowed rate next to a
lifetime saving without saying so would be the sort of quiet mismatch that makes a dashboard lie.

### 4.19 The token guard is declared on the router

```python
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)
```

Not a per-route `AdminDep`. The per-route form is one forgotten parameter away from publishing every
tenant's spend, and the forgetting happens in three months when someone adds a sixth endpoint. This
way a route added later is authenticated by construction, and
`test_every_admin_route_needs_the_admin_token` walks all five paths to prove the guard was inherited
rather than assumed.

Two neighbouring properties: a valid *virtual* key is rejected as well as a missing one — a tenant
credential must not read other tenants' figures — and admin traffic writes no `request_log` row,
because `prism/main.py` only opens an audit context for `/v1/`. The second means `requests` cannot
measure how often someone refreshed the console, and `test_admin_traffic_does_not_appear_in_the_request_log`
keeps it that way.

### 4.20 Provider health publishes a verdict *and* `enforced: false`

`gateway_config.json` has shipped a `degradation` block since Day 1 — `error_rate_threshold`,
`window_seconds`, `p95_latency_ms` — parsed, validated, and read by nothing. This endpoint reads it
and answers the contract's "whether the gateway currently considers it healthy" with a real verdict:

```python
if not self.requests:
    return None
too_many_errors = self.error_rate > self.policy.error_rate_threshold
too_slow = (
    self.p95_latency_ms is not None
    and self.p95_latency_ms > self.policy.p95_latency_ms
)
return not (too_many_errors or too_slow)
```

There is still no circuit breaker. A provider reported `degraded` is tried first anyway and still
costs each request its retry budget before failover. Reporting a verdict while implying it changed
behaviour would be the dishonest version of this endpoint, so `enforced: false` is a top-level field
and `test_provider_health_says_the_verdict_is_not_enforced` asserts it — the claim cannot rot when a
breaker eventually lands, because the test will fail.

Two honesty properties fall out of the data rather than out of taste:

- **No traffic reports `unknown`, not `healthy`.** `healthy` is `None` when `requests == 0`. Silence
  is not health, and the provider nobody has called since the last restart is the one most likely to
  be broken. The provider universe comes from `config.provider_names`, sorted, so a completely dead
  provider still appears in the list.
- **Errors are under-attributed when a failover succeeds.** One row per request means a provider
  that failed and was successfully failed over from leaves a row naming the provider that
  *succeeded*; its failure shows up in `fallbacks` and `retries`, not in `errors`. Per-attempt
  accuracy needs a row per attempt, which is a schema change this slice did not make.
  `test_a_survived_failover_shows_up_as_a_fallback_not_an_error` pins the exact shape — alpha down,
  alpha `unknown` with zero errors, beta serving with `fallbacks == 1` — so the limitation is
  demonstrated rather than merely admitted.

`avg_latency_ms` uses `if row.avg_latency_ms is not None`, not a truthiness test. The in-process fake
upstream genuinely produces 0 ms averages, and a falsy check would report "no data" for traffic that
demonstrably happened. This was caught by reading the line before running it, and it is the sort of
bug that survives a green suite.

### 4.21 Money is rendered two ways, deliberately

Aggregates are JSON **numbers**: they exist to be summed and charted, the contract's example shows a
number, and they are computed as exact `Decimal` in Postgres and converted once at the boundary.
Per-request costs in `/admin/logs` are **strings**, produced by the same `format_usd` that wrote the
`x-prism-cost-usd` header the client saw — so an operator chasing a reconciliation gap can diff the
two byte for byte instead of arguing about float formatting.

`prism/money.py`'s scientific-notation argument is about headers read by shells and spreadsheets. A
JSON number is read by a parser, for which `2e-05` is unambiguous, so the argument does not transfer
and the two renderings are not an inconsistency.

### 4.22 Smaller decisions worth being able to defend

- **`to` is inclusive.** `from=2026-07-01&to=2026-07-31` has to mean July, which is the only reading
  under which the contract's own example is not off by a day. `Window.end` is midnight of
  `to + 1 day`, and `test_the_window_includes_the_whole_last_day` states it explicitly rather than
  leaving it to be inferred from a comparison operator.
- **An omitted window is the current budget period, not a rolling 30 days.** The point of the number
  is to be comparable against the monthly budget, and a rolling window is comparable against nothing.
  `resolve_window` calls `BudgetPeriod.period_for` so there is exactly one definition of "this
  period" in the codebase.
- **`from > to` is a 400 naming `from`**, not an empty result. An empty result for a typo reads as
  "no traffic", which is the wrong thing to believe.
- **`limit` is capped at 500 and an oversized value is refused, not clamped.** A silently clamped
  `limit=10000` returns 500 rows that look like the whole answer.
  `test_an_oversized_limit_is_refused_rather_than_clamped` pins the 400.
- **Log ordering is `created_at DESC, request_id DESC`.** The tiebreaker is what makes "the newest
  50" deterministic when rows share a timestamp, which they do under load.
- **Totals are summed in Python from the rows already fetched**, not by a second aggregate query.
  Two queries could disagree with each other under concurrent writes, and a total that does not equal
  the breakdown printed beside it destroys confidence in both.
- **`remaining_usd` is clamped at zero.** Budgets can overshoot — the last request to slip through
  the check is charged in full — and a negative "remaining" reads as a bug rather than as the
  overshoot it is. The overshoot itself stays visible in `spent_usd` versus `budget_usd`.
- **A de-priced model still reports its saved tokens.** `_entry_savings` prices each `served_model`
  through `config.price` and swallows `NotFoundError`, so an entry whose model has left the pricing
  file contributes tokens and zero dollars instead of failing the whole endpoint.
- **A cache-disabled tenant is listed with zeroes and `cache_enabled: false`**, driven from the
  tenant table rather than from the entries table. A tenant that is *absent* from a stats page is
  indistinguishable from one that is present and idle, and two of the four seeded keys have caching
  off — so this is the common case, not an edge one.
- **An enabled tenant with no `similarity_threshold` misses loudly.** The column is nullable so a
  misconfiguration is visible; honouring that means declining to invent a default, because a
  threshold picked by the gateway would be a number nobody chose guarding a data-leak boundary. It
  logs a warning and serves every request as a miss.

---

## 5. The five proofs

**1. One tenant's entry is invisible to another.**
`test_one_tenants_entry_is_invisible_to_another` stores an entry for `search`, then asks the
identical question as `research` and asserts a miss. This is the one test that maps directly onto a
sentence in the problem statement — "a cache hit that returns another team's response is a data
leak" — and it asserts the *identical* prompt rather than a paraphrase, because the exact path is the
one that would leak without ever consulting a threshold.

**2. Similarity alone would serve the wrong answer, measured in this repository.**
`test_similarity_alone_would_serve_the_wrong_answer` re-derives both guard cosines with the real
model — negation at 0.9620, amount at 0.9149 — asserts they clear the thresholds the gateway
actually runs at, and then asserts each is rejected by a different guard. The guards are therefore
justified by a measurement anyone can re-run on this machine, not by a plausible story about
embeddings. It is `model`-marked and skips where the artifact is absent.

**3. A rejected candidate does not stop the scan.**
`test_a_rejected_candidate_does_not_stop_the_scan` stores a valid entry, then a *newer* nearer one
that the literal guard will reject, then asks the question. The naive loop — take the best match,
check the guards, return — would find the newer entry, reject it, and report a miss, shadowing a
perfectly good answer with one that failed. The scan continues instead, and the reason string records
what was thrown out and why (`… rejected=literal:1`). This is the subtlest bug in the module and it
is a test rather than a comment.

**4. Reported usage reconciles against the headers the client saw.**
`test_usage_reconciles_with_the_cost_headers` sums the `x-prism-cost-usd` values from a series of
real requests and asserts `/admin/usage` returns that exact `Decimal` — reconciling against what was
*served*, never against a constant. A constant would have to be updated whenever the pricing file
changed, and the version of this test that does that passes while measuring nothing. The companion
`test_the_budget_counter_reconciles_against_the_log` closes the loop from the other side.

**5. The admin surface is authenticated by construction, and leaks neither prompts nor keys.**
Three tests together: `test_every_admin_route_needs_the_admin_token` parametrised over all five
paths, `test_the_log_never_carries_prompt_text` which searches the entire serialised response for a
nonsense word that was in the prompt, and `test_keys_reports_policy_without_any_key_material` which
asserts none of the three virtual keys and no substring `"hash"` appears anywhere in the body. The
first proves the guard was inherited; the other two prove that being past the guard still does not
grant access to prompt text or credentials.

---

## 6. Verification evidence

### The suite

**357 passed**, no failures and no skips on this machine (Postgres up on 5433, model present). Ninety-six
of those are new — 59 in `tests/test_cache.py`, 37 in `tests/test_admin.py` — against 261 at the end
of Day 4.

```
python -m pytest                                    # 357 passed
python -m pytest -m "not postgres"                  # no database needed
python -m pytest -m "not postgres and not model"    # the fast lane
python -m pytest -m model                           # 10 passed: the quality lane
python -m ruff check prism tests scripts            # All checks passed!
```

Lane breakdown for the two new files, because the split is the argument:

| File | Pure | Postgres | Model |
|---|---|---|---|
| `tests/test_cache.py` | 32 | 25 | 3 |
| `tests/test_admin.py` | 4 | 33 | — |

The cache's Postgres lane uses `FakeEmbedder`, which is lexical. That is deliberate and is stated in
the module docstring: those 25 tests grade *plumbing* — scoping, isolation, thresholds, TTL, hit
accounting, the HTTP path — and a lexical vector is enough to do that deterministically in
milliseconds. The three tests that grade *quality* load the vendored artifact. Confusing the two is
how a cache ends up with a green suite and a threshold nobody has measured.

Every guard test asserts its own premise first: that the pair really does clear the threshold, and
only then that the lookup still misses. Without the first assertion a guard test starts passing
trivially the day similarity drifts below the threshold, and would then prove nothing while staying
green.

### The measured numbers, re-derived during this pass

| Pair | Cosine | Consequence |
|---|---|---|
| `Can I cancel my annual plan?` / `Can I **not** cancel my annual plan?` | **0.9620** | clears 0.92 and 0.85; rejected by the polarity guard |
| `What is the refund on a 100 USD charge?` / `…200 USD…` | **0.9149** | clears 0.85, not 0.92; rejected by the literal guard |

Both were measured directly against the real model, and the numbers are reported honestly rather
than rounded toward the argument: 0.9149 does **not** clear the demoed 0.92, so at the `search` key
the threshold alone would already have rejected that pair. The literal guard is load-bearing at the
free tier's 0.85 — which is exactly the tenant a deployment lowers a threshold for.

### The bugs and inconsistencies of the slice

Four, and none of them were found by a failing test.

| Found | What | How it was found |
|---|---|---|
| Before running | `avg_latency_ms=round(x) if row and row.avg_latency_ms else None` reports `None` for a genuine 0 ms average, which the in-process fake upstream produces constantly | reading the line |
| Lint | `SIM103` in `ProviderHealth.healthy` | `ruff check` |
| Lint | `E501` in a `/admin/usage` past-window call | `ruff check` |
| Documentation pass | `prism/cache.py` and `tests/test_cache.py` cited the guard cosines as 0.9292 and 0.9056 — numbers from earlier phrasings of those pairs, no longer anywhere in `DESIGN_NOTES.md` | re-measuring both pairs while writing this log |

The last one is the one worth dwelling on. The test still passed, because it asserts `> 0.92` and
`> 0.85` rather than the exact values — so a citation drifted out of agreement with the document it
cited without anything going red. The fix was to re-measure and correct all three citations. The
general lesson, and it applies to every measured number in this repository: a number in a comment is
only as trustworthy as the last time someone re-derived it, which is why the pairs are now **named**
in `DESIGN_NOTES.md` instead of appearing as bare figures. A heading in the same section also said
the guards run "before the threshold" when the code and the table below it both say after; corrected.

The `SIM103` fix is worth one line because the rewrite reads better than the original: naming
`too_many_errors` and `too_slow` and returning `not (too_many_errors or too_slow)` states the health
rule in the vocabulary of the `degradation` config, which the inlined negation did not.

### Not yet run, and therefore not claimed

- **`scripts/smoke_test.py` and `scripts/load_test.py` have not been run against a live gateway in
  this slice.** The salted-prefix behaviour in [DESIGN_NOTES](DESIGN_NOTES.md#the-smoke-test-still-passes-with-a-salted-prefix)
  was verified by reading the script and measuring its prompt pairs directly with the real model,
  three times — which establishes that the cosines land where the demo needs them, and does *not*
  establish that the script passes end to end.
- **The three failure drills** — `latency_ms: 3000`, `fail_rate: 0.3`, and restoring a provider to
  confirm traffic returns to the primary — are unexercised against real sockets.
- **Added-latency measurement.** The cost of a cache lookup on a miss (one embedding plus a scan) has
  not been measured under concurrency.

All three are the next slice's work, and until then they are absent from the claims rather than
softened inside them.

### Re-running any of it

```bash
python -m pytest tests/test_cache.py -k tenant        # the isolation proof
python -m pytest tests/test_cache.py -m model         # the guard cosines and the paraphrase demo
python -m pytest tests/test_admin.py -k reconcile     # both reconciliation proofs
python -m pytest tests/test_admin.py -k token         # the router-level guard, all five paths
```

---

## 7. Earlier decisions revisited

**Day 2's "the headers and the log row are the same four facts" is now narrowed, on purpose.** That
claim was the argument for deriving both from one `LogContext`, and it still holds for three of the
four facts. The cache breaks it for the fourth: on a hit, `x-prism-provider` names the provider that
originally produced the answer while the row's `resolved_provider` is null. Both provided documents
require exactly that, each about its own artifact, and the reconciliation is that the header reports
provenance while the row reports activity (4.9). The mechanism is a single explicit override
parameter on `prism_headers` with the reasoning in its docstring — not a second header-building path,
which is what `docs/IMPLEMENTATION_GUIDE.md` warns retrofitting the contract turns into.

**Day 2's `context.cache = "miss"` placeholder is retired.** It was hard-coded for three slices,
survived Day 4 untouched, and `tests/test_chat.py` asserted it — which means the header contract was
being tested against a constant. That is exactly one line of test change now, and it is the reason
the placeholder was left as a literal rather than as a `TODO` in a comment: a test asserting a
constant is a placeholder that announces itself when the real thing arrives.

**Day 4's `MemoEmbedder` prediction was right, and is deliberately not depended on.** The Day 4 log
argued for replacing `LazyEmbedding` with a memo *because* a second consumer would embed a different
string, and named the cache as that consumer. It does: the router embeds the extracted ask, the cache
embeds the full final turn, and the memo is what keeps a routed request from paying twice when they
coincide. But `Lookup` still hands its vector back explicitly (4.11), because relying on a
performance optimisation for correctness is how an optimisation becomes impossible to remove.

**Day 1's admin token finally has consumers, and its limitation is unchanged.** `require_admin` and
`tests/test_admin_auth.py` were written on Day 1 with nothing behind them, and needed no
modification to guard five routes four days later — which is the payoff for having written the auth
plane as a boundary rather than as part of a route. What has *not* changed is that it is one shared
token with no per-operator identity: everyone who can read `/admin/usage` is the same principal in
the logs. It was in Known limitations on Day 1 and it still is.

---

## 8. Anticipated review questions

**"A semantic cache can return the wrong answer. How do you sleep?"**
By not relying on a single number. The threshold is per tenant and is the *first* filter, not the
only one; two rejection-only guards run after it and are justified by pairs measured in this
repository that clear the threshold while having different correct answers; the scope means a
candidate must already share a tenant, a tier, a conversation prefix and every sampling parameter
before similarity is even computed; and volatile prompts are never stored, which is the one class of
staleness no threshold can address. The residual risk is real and named in Known limitations: a
paraphrase pair that is semantically identical, shares its literals and its polarity, and yet has
different answers, would be served. I have not constructed one, and I would not claim there is none.

**"Why is pair B a permanent WARN at 0.92? Isn't that a failure?"**
It is a documented trade. `req_cache_b1`/`b2` measure 0.8856–0.9054 depending on the salt, so at the
demoed 0.92 that pair misses. Lowering to 0.88 to catch it also brings the amount pair (0.9149) and
every near-miss between into range, and the cost of a false hit — a confidently wrong answer with no
signal to the caller — is much higher than the cost of one extra upstream call. The free-tier key
runs at 0.85 precisely so the effect of that choice is visible in the same deployment rather than
argued about in the abstract.

**"You store prompts and full response bodies, but you refused to store them in `request_log`. Isn't
that inconsistent?"**
No, because the two have different justifications. A cache that does not keep the response is not a
cache; a log that keeps every prompt is a second copy of all traffic in the table an operator
casually exports. `docs/DATA_MODEL.md:78` asks for body storage to be a documented decision, and it
was declined for one table and accepted for the other for a stated reason. What limits the exposure
is that caching is **opt-in per tenant** — two of the four seeded keys have it off, so the "no
retention" path is exercised, not hypothetical — and that TTL is the only retention control, which is
in Known limitations.

**"`/admin/usage` reports less than my client's own token sum. Is the meter broken?"**
No, and the difference is exactly `tokens_saved` from `/admin/cache/stats`. Usage reports what the
*providers* were asked for, so it reconciles against an invoice; a replayed cache hit carries the
original response's `usage` block in its body, so a client counting what it received counts those
tokens again. `scripts/load_test.py:123` prints the client-side figure, so this will come up — it is
documented in the route docstring, in DESIGN_NOTES and in the README rather than left to be
discovered.

**"Why report a health verdict you don't act on? That seems worse than nothing."**
It is better than nothing and worse than a breaker, which is why the response says which one it is.
The `degradation` block has been parsed since Day 1 and read by nothing, so its thresholds were
config that could drift arbitrarily far from behaviour without anyone noticing. Now they compute a
verdict an operator can see, next to `enforced: false`, with a test asserting that flag — so when a
breaker lands, the test fails and the documentation cannot quietly stay wrong. The dishonest version
of this endpoint is the one that returns `degraded` and lets a reader assume routing changed.

**"Your per-provider error counts are wrong when failover works. Why ship that?"**
Because fixing it properly is a schema change — one row per attempt instead of one row per request —
and that is a bigger decision than this slice should make on the way past. What is shipped instead is
the limitation made visible: `fallbacks` and `retries` are in the same response, so `fallbacks`
rising while `errors` stays flat is the readable signal that something in a chain is sick, and a test
pins that exact shape with alpha down and beta serving. An under-attribution that is documented and
demonstrated is a known quantity; one that is neither is a lie in a dashboard.

**"Why is `key=` allowed to be a whole virtual key at all, if you think that's unsafe?"**
Literal compatibility with the contract's example, and nothing more. The parameter accepts it,
resolves it by hash, and echoes back only the prefix — so a client that followed the document works,
while the recommended form (the team name) puts no credential anywhere. Refusing the full-key form
outright would have been defensible too; accepting it costs one `or_` clause and removes a reason for
the endpoint to appear broken to someone reading the provided document.

**"Five hundred candidates. What happens to tenant number 501's oldest answer?"**
It stays in the table and stops being findable by the semantic path — though it is still served by
the exact path, since that is an indexed lookup with no window. So the degradation is "paraphrases
of old questions start missing", not "old answers vanish". It is in Known limitations with the
honest addition that the point at which a Python scan stops being affordable has not been measured
here, and that pgvector is what the fix looks like rather than a bigger constant.

**"What does a cache lookup cost on a miss?"**
One embedding plus up to 500 dot products. The embedding was measured on Day 4 at a median of 3.8 ms
warm, and for an `auto` request it is already paid for by the router, so the marginal cost is the
scan. What has *not* been measured is any of this under concurrency, where ONNX holding the GIL
matters, and that is stated in section 6 as not-claimed rather than estimated.

---

## 9. Known weaknesses, stated plainly

- **A false hit is possible in principle and I cannot bound it.** Two prompts that are semantically
  near-identical, share their literal multiset and their polarity, and nevertheless have different
  correct answers would be served from the cache. The guards close the two classes I could measure;
  they are not a proof.
- **The volatility guard is lexical.** It refuses to store an answer whose prompt contains time words.
  A time-sensitive question phrased with none — "what is the price of X" — is cached like anything
  else, and bare `"now"` is deliberately excluded because it is far more often ordinary English.
- **No size cap and no background sweeper.** With `PRISM_CACHE_TTL_SECONDS` unset — the default,
  which is what makes the paraphrase demo reproducible — entries never expire and `cache_entries`
  grows without bound per tenant. Purging is opportunistic and only ever removes rows that already
  had a TTL.
- **A lookup scans the newest 500 entries per `(tenant, scope)`.** A valid older entry can fall out
  of the window and be missed while still occupying storage.
- **Streamed answers are never cached.** A tenant whose traffic is entirely streaming gets no entries
  at all, so the cache is invisible to it in both directions.
- **`cache_entries` holds prompt text and response bodies** that `request_log` deliberately omits.
  Opt-in per tenant, and TTL is the only retention control — there is no delete-my-data endpoint.
- **The similarity scan is CPU-bound and single-process.** Embedding runs through
  `asyncio.to_thread` and ONNX holds the GIL, so lookup throughput under concurrency is bounded by
  the default executor. Unmeasured.
- **Provider health is observed, not enforced**, and its error counts under-attribute a provider
  whose failure was successfully failed over. Both are in the response's own shape and in a test.
- **Key management is read-only.** No create, rotate, disable, or budget-adjust endpoint; keys change
  by editing the seed file and re-running `scripts/init_db.py`.
- **One shared admin token, no per-operator identity.** Every admin read is the same principal.
  Outstanding since Day 1.
- **Cache statistics blend two time bases in one response** — windowed hits and lifetime savings.
  They are labelled, but a reader who skims will still compare them.
- **`/admin/usage` under-reports against a client's own token sum** by exactly `tokens_saved`. A
  property of caching, documented in three places, and still the first thing that will look like a
  bug.
- **The admin plane has no pagination.** `/admin/logs` caps at 500 rows and offers no cursor, so
  "everything last Tuesday" is not a question it can answer.
- **No admin endpoint is cached or rate limited.** A console that polls `/admin/usage` every second
  runs a full aggregate over `request_log` every second, on the same database the data plane writes
  to.
- **`scripts/load_test.py` and `scripts/smoke_test.py` have never been run against a live gateway**,
  and neither have the three provider failure drills. Section 6 says what that does and does not
  leave claimable.
