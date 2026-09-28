# Day 1 design log — the spine

What was built, in what order, why, and what each decision cost. Written alongside the code rather
than reconstructed afterwards, so the reasoning is the real reasoning.

Companion documents:

- [DESIGN_NOTES.md](DESIGN_NOTES.md) — measured numbers (cosines, thresholds, routing method)
- [../README.md](../README.md) — how to run it; **Known limitations** is the honest ledger
- This file — *why the code looks the way it does*, and the answers to the obvious objections

---

## Contents

- [1. Scope: what Day 1 is and is not](#1-scope-what-day-1-is-and-is-not)
- [2. File map](#2-file-map)
- [3. Build order and why that order](#3-build-order-and-why-that-order)
- [4. Design decisions](#4-design-decisions)
- [5. The concurrency proof](#5-the-concurrency-proof)
- [6. Verification evidence](#6-verification-evidence)
- [7. Anticipated review questions](#7-anticipated-review-questions)
- [8. Known weaknesses, stated plainly](#8-known-weaknesses-stated-plainly)

---

## 1. Scope: what Day 1 is and is not

Day 1 is **the spine, not the gateway**. No request reaches a provider yet; there is no
`POST /v1/chat/completions`. What exists is the four things every later feature stands on:

| # | Built | Because everything downstream needs it |
|---|---|---|
| 1 | Configuration loading | Routing cannot resolve an alias that has not been loaded and validated |
| 2 | The schema | Budget enforcement needs money arithmetic and an atomic increment |
| 3 | Authentication | Every request passes through this gate |
| 4 | The error contract | Every failure path needs a status code, and they must not drift |

Roughly **1,468 lines of `prism/` against 1,199 lines of tests** — close to 1:1.

### Why not a vertical slice first?

A vertical slice through a wrong foundation is worse than no slice, because it has to be built
twice. Routing needs the alias table. Budgets need money that does not lose cents. Every failure
path needs the status-code table. Doing the request path first means doing it again.

### Deliberately not built

| Skipped | Defense |
|---|---|
| **Alembic migrations** | There is no deployed database to migrate. During the phase when the schema changes hourly, a migration per tweak costs review time and buys nothing. Upgrade path is one `alembic init` plus one autogenerate against the existing schema. What migrations usually protect you from *is* handled: the serving process cannot run DDL. |
| **The data plane** | Depends on all four foundations above. |
| **pgvector** | Requires a Postgres extension. Similarity is computed in Python over a per-tenant scan; an ANN index is a documented limitation. |
| **Rate limiter and budget enforcement logic** | The schema and the enforcement *order* are in place; the enforcing code is the next slice. This puts the concurrency proof before the code that relies on it. |

---

## 2. File map

| File | Lines | Owns |
|---|---|---|
| `prism/settings.py` | 87 | Deployment environment (`PRISM_*`), validated, fail-fast |
| `prism/errors.py` | 123 | **Every status code in the system** |
| `prism/config.py` | 332 | Providers, aliases, prices, retry policy — immutable after load |
| `prism/keys.py` | 37 | Key hashing and prefix extraction |
| `prism/db/models.py` | 313 | The four tables |
| `prism/db/session.py` | 70 | Engine, sessionmaker, schema create/drop |
| `prism/deps.py` | 50 | FastAPI dependency wiring |
| `prism/auth.py` | 134 | Both auth planes, and the enforcement order |
| `prism/seed.py` | 109 | Idempotent tenant upsert from `data/seed_keys.json` |
| `prism/api/health.py` | 44 | `/healthz`, `/readyz` |
| `prism/main.py` | 143 | App factory, lifespan, middleware, handler registration |
| `scripts/init_db.py` | 78 | The only thing that runs DDL |

Tests: `test_schema.py` 285, `conftest.py` 216, `test_config.py` 183, `test_seed.py` 147,
`test_auth.py` 128, `test_errors.py` 60, `test_settings.py` 59, `test_health.py` 45,
`test_admin_auth.py` 40, `test_keys.py` 36.

---

## 3. Build order and why that order

```
errors → settings → config → keys → db/models → db/session
       → deps → auth → seed → api/health → main → scripts/init_db
```

Two principles produced this sequence.

### (a) Contract before mechanism

`errors.py` was written **first**, before anything capable of raising an error — even though
`settings.py` has fewer dependencies and is "lower level". The status-code table is what every
later module must obey. Written the other way round, `auth.py` would have chosen its own status
codes inline, budget enforcement would have chosen different ones, and the reconciliation would be
a later refactor.

The structural consequence: **no route is able to choose a status code.** A route can only raise a
`PrismError` subclass that already carries one. The mapping cannot drift because there is nowhere
for it to drift to.

### (b) Strict dependency order thereafter

Every module imports only modules that already exist. The payoff is concrete: each module was
importable and testable the moment it was written — no stubs, no `TODO: wire this up`, no circular
imports to untangle. `main.py` is last because it is pure assembly and cannot precede its parts.

Tests were written **alongside** each module, not after. `test_schema.py` is the largest test file
and was written with `models.py`, because the concurrency claim is the entire reason for choosing
Postgres and an unproven claim is just an assertion.

---

## 4. Design decisions

Each one: the plain version, the senior version, and what it cost.

### 4.1 Two separate configuration objects

`settings.py` reads environment variables. `config.py` reads a JSON file.

- **Plain** — where the database lives is a *deployment* fact. Which models exist and what they
  cost is a *product* fact.
- **Senior** — different lifecycles, sources, and failure modes. Env vars are set by whoever
  deploys; gateway config is authored by whoever runs the product. One combined object makes "move
  the database" and "change a price" the same kind of edit, and forfeits differentiated validation:
  `config.py` does deep cross-validation (does every model in every fallback chain have a price?),
  which is meaningless for `PRISM_PORT`.
- **Cost** — two places to look. Mitigated by splitting on *source*, which is easy to remember.

Implementation note: the field is named `embedding_cache_dir`, not `model_cache`, because pydantic
reserves the `model_` prefix and warns on collisions. The env var is still `PRISM_MODEL_CACHE`, via
an explicit `alias=`.

### 4.2 Postgres, not SQLite

- **Plain** — many requests increment the same budget at once. SQLite runs writers one at a time,
  so a broken increment would still look correct in tests.
- **Senior** — the requirement is that `UPDATE budget_periods SET spent = spent + :cost` is atomic
  under concurrency. SQLite serialises writers, so a read-then-write implementation passes there
  and loses updates in production. That is the worst class of test: one that passes for the wrong
  reason.
- **Cost** — a real database to run in development rather than a file. Paid down by
  `docker compose up -d`, with `scripts/pg.sh` as the path where Docker is unavailable.

See [section 5](#5-the-concurrency-proof) for how this is demonstrated rather than asserted.

Scope note: this argument is about **budget accounting, not rate limiting**.
`docs/DATA_MODEL.md:118` permits in-memory rate-limit state provided it is race-safe, so the
over-admission `scripts/load_test.py` hunts for is an in-process concurrency bug, not a database
one. Conflating the two is a common way to get this answer half right.

### 4.3 `Decimal` and `NUMERIC(18,10)` for money, never `float`

- **Plain** — floats do not add up predictably. Money must.
- **Senior** — float addition is not associative, so summing identical costs in a different order
  yields a different total, and the graded reconciliation would fail intermittently and
  irreproducibly. Two specifics: use `Decimal(str(v))` and never `Decimal(v)`, since the latter
  inherits the float's error before you begin. And the **scale of 10 is derived, not chosen** — the
  `budget-demo` tenant's monthly budget is `$0.00001`, so any scale coarser than ~1e-8 rounds that
  tenant's spend to zero and the budget-exceeded demo silently never fires.
- **Cost** — `NUMERIC` arithmetic is slower and does not vectorise. Irrelevant at a few operations
  per request.

### 4.4 SHA-256 for virtual keys, not bcrypt or argon2

The decision most often mistaken for an error. The answer has two halves.

- **Plain** — the key is a long random string we generate, not a password a human chose.
- **Senior, part one: threat model.** Slow hashes exist to make brute-forcing a *low-entropy*
  secret expensive. A virtual key is high-entropy and machine-generated; there is nothing to brute
  force, so key-stretching buys nothing.
- **Senior, part two: bcrypt is structurally unusable here.** It is salted per row, so a key cannot
  be looked up by its hash. Authentication would have to load every tenant and compare one at a
  time — O(n) with a deliberately slow function, on every request. SHA-256 is unsalted and
  deterministic, so `key_hash` is a unique indexed column and authentication is one index hit.
- **The deliberate asymmetry** — the *admin* token is compared with `secrets.compare_digest`,
  because it is operator-chosen and may well be low-entropy, making a byte-by-byte `==` a genuine
  timing oracle. A virtual key needs no constant-time compare because there is no comparison loop
  to time: it is an index lookup.

### 4.5 Four tables for seven entities

`docs/DATA_MODEL.md` lists seven entities; the schema has four. The collapsing is argued in the
module docstring of `prism/db/models.py`.

- **Provider, Model Alias, Model Price are not tables.** They are deployment configuration: no
  per-request mutable state, no foreign keys pointing at them. As tables, changing a price needs a
  migration and reading one needs a join on the hot path.
- **Usage Record is not a table.** It is a query over `request_log`. Writing an aggregate row
  beside every log row is a **dual write, and dual writes drift** — the moment one succeeds and the
  other does not, the usage API and the log disagree with no way to adjudicate.
  `docs/EVALUATION_GUIDE.md` grades exactly that reconciliation, so `request_log` is the single
  source of truth.
- **`budget_periods` is the one deliberate denormalisation.** `docs/DATA_MODEL.md:86` requires
  budget reads to be cheap, and scanning the log per admission is not. So it is a counter,
  incremented atomically, reconcilable against `request_log` at any time: *a cache with a
  correctness proof, not a second source of truth.*

The condition that would reverse this: multi-tenant **self-service** pricing, where tenants edit
prices at runtime. Then config becomes tenant data and belongs in the database.

### 4.6 The error contract lives in exactly one file

Six subclasses, each fixing a `status_code` and an `error_type`. Bodies are OpenAI-shaped
(`{"error": {message, type, code, param}}`) because the data plane must stay OpenAI-compatible and
existing clients parse `error.message` / `error.type`.

Three deviations from the obvious, each defended in the code:

| Deviation | Reason |
|---|---|
| **402 for budget, not 429** | The contract permits either. `scripts/load_test.py:106` classifies *every* 429 as "rate limited", so 429 here would make a budget-exhausted key look like a rate-limiter result and corrupt the one number that test exists to measure. 402 is also more honest: retrying later cannot help. Correspondingly `Retry-After` is set for 429 and deliberately **not** for 402. |
| **404 before 403 for models** | If unknown models 404 and forbidden models 403, the status code is an oracle for the model registry to anyone holding any key. Unknown-model is therefore checked *first* and answers identically for everyone. |
| **Missing, malformed and unknown keys are indistinguishable** | All three produce the same 401 message. Distinguishing them tells an attacker which part of the guess was structurally correct. |

The 500 handler **never echoes `str(exc)`**. An unexpected exception may carry a provider API key, a
DSN, or another tenant's row, and `docs/DATA_MODEL.md:44` forbids gateway secrets reaching a
client-visible error. Detail goes to the server log, keyed by request id.

`tests/test_errors.py` transcribes the contract table **from the document, not from the code**, so
the test fails if the code drifts from the spec rather than agreeing with itself.

### 4.7 Enforcement order

```
1. authentication      401
2. tenant status       401   (a disabled key is not a valid key)
3. model allowlist     403
4. rate limit          429
5. budget              402
```

Cheapest and most certain first. Authentication precedes everything because without a tenant there
is no limit to apply.

The non-obvious step is **allowlist before rate limit**: a request for a model the team may not use
is wrong regardless of remaining quota. Checking the limit first would spend quota to discover the
request was invalid — and would let a caller **burn a tenant's rate limit with requests that could
never succeed**, a small denial-of-service against your own tenant.

### 4.8 App factory, lifespan, and `app.state` — no module-level globals

- **Plain** — `create_app()` builds an app; it does not reach for globals.
- **Senior** — globals make tests share state and force import-time side effects: importing the app
  would open a database connection. A factory lets each test build an isolated app with injected
  fakes, and `lifespan` gives every resource a deterministic acquire/release tied to process
  lifetime. Tests drive it explicitly via `app.router.lifespan_context(app)`.

Related: tests use **httpx `ASGITransport` + `AsyncClient`, not `TestClient`**. `TestClient` runs
the app on its own event loop, so the app and the test would sit on *different* loops and async
database fixtures would fail with cross-loop errors.

### 4.9 One session per request — not one transaction per request

- **Plain** — a rejected request still has to write its log row.
- **Senior** — the tempting pattern is a transaction spanning the request, committed at the end. It
  breaks here: a 429 must **commit** its `request_log` row and *then* return an error. Under
  transaction-per-request the error path rolls back the very row that proves the rejection
  happened, and usage reconciliation comes up short by exactly the number of rejections.

### 4.10 Fail-fast startup

Three ways to misconfigure Prism; each **refuses to start**, naming the fix:

| Misconfiguration | Message names |
|---|---|
| `PRISM_MODEL_CACHE` unset or not a directory | the variable, and the README section |
| Schema not initialised | `python scripts/init_db.py` |
| Bad gateway-config path | the `cp` command that creates it |

**Senior framing** — a gateway that boots with a broken config and fails on the first *request* has
converted a deploy-time error into a production incident, and a confusing one, because the failure
surfaces far from its cause. All three paths were exercised and their real output captured.

### 4.11 The serving process never runs DDL

`create_schema()` exists but the app never calls it; only `scripts/init_db.py` does, and `--reset`
requires typing the literal word `reset` at a prompt.

**Senior** — schema changes are an operator action with a blast radius. If the app could create
tables, N replicas would race at startup, and a typo'd `PRISM_DATABASE_URL` would silently create a
fresh empty database rather than failing loudly. Startup instead *verifies* the schema
(`SELECT count(*) FROM tenants`) and refuses to serve without it.

### 4.12 Smaller decisions worth being able to defend

| Decision | Reason |
|---|---|
| `ARRAY(DOUBLE_PRECISION)` for embeddings, not `ARRAY(Numeric)` | 384 `Decimal` conversions per row per lookup, for values that are measurements rather than money |
| `DateTime(timezone=True)` everywhere, `datetime.now(timezone.utc)` never `utcnow()` | `utcnow()` returns a naive datetime, which silently compares wrong against aware ones |
| JSONB for `model_allowlist` | A short list read whole; a join table would add a query to the admission path for no gain |
| String columns, not native Postgres ENUMs | Adding a status value would need `ALTER TYPE`; the Python enum still gives type safety in application code |
| Nullable `tenant_id` on `request_log` | A 401 has no tenant by definition. `team` and `key_prefix` are denormalised onto the row so rejections stay attributable and deleting a tenant does not erase the audit trail |
| `Provider.__repr__` masks `api_key` as `'***'` | So a credential cannot reach a log line or a traceback by accident |
| Provider ownership via `model.rsplit("-", 1)[0]` | Nothing in the config states ownership. Confined to one function (`GatewayConfig.provider_for_model`) and listed in Known limitations. `scripts/validate_pack.py:64` relies on the same convention |

### 4.13 Config validation rejects, at load time

`GatewayConfig._validate()` refuses to start on: no providers; no prices; an alias with both a
router and a primary; an alias with neither; a chain model absent from the price table; a router
target that is not a configured alias; and **a router target that is itself a router** — the
infinite-recursion guard.

The last one is the interesting one: it is not a typo check, it is a termination proof for the
routing resolver, enforced at load rather than defended against per request.

---

## 5. The concurrency proof

The Postgres choice is demonstrated, not asserted. `tests/test_schema.py` contains a matched pair.

**The claim** — `test_concurrent_increments_are_all_counted` fires `CONCURRENCY = 40` coroutines,
each performing a single statement:

```sql
UPDATE budget_periods
   SET spent_usd = spent_usd + :cost,
       request_count = request_count + 1
 WHERE ...
RETURNING spent_usd
```

and asserts the final total is exactly `COST * 40`. The row lock is held for the duration of the
statement, so the read and the write cannot be separated.

**The counter-example** — `test_read_then_write_loses_updates` does `SELECT`, then
`await asyncio.sleep(0.05)`, then `UPDATE`, across 10 tasks, and asserts the total is **less than**
`COST * 10`.

The second test is the one to talk about. It does not test the implementation; it **demonstrates
the bug the design avoids**, and it is deterministic — the sleep guarantees the damaging interleaving
rather than hoping for it. A flaky test proving a race is worthless; this one cannot pass by luck.

Also in `test_schema.py`: one budget period per tenant per month enforced by
`UniqueConstraint(tenant_id, period_start)`; `period_for()` returning the first of the UTC month;
a rejection logged with `tenant_id=None`; two tenants caching the same prompt hash independently
(the isolation boundary); the same prompt under two aliases producing two entries; and deleting a
tenant cascading their cache entries while leaving log rows with `tenant_id=None` and `team` intact.

---

## 6. Verification evidence

| Check | Result |
|---|---|
| Test suite | **83 passed** |
| Suite with no Postgres at all | **41 passed, 42 skipped, in ~4.5 s** |
| `scripts/validate_pack.py` | Pack OK — 4 priced models, 2 providers, 3 aliases, 4 seed tenants, 14 sample requests, 20 routing eval cases |
| Provided scaffold vs the import commit | `data/` and the four provided scripts byte-identical |
| `scripts/init_db.py` | creates the schema and seeds 4 tenants |
| Server boot | `/healthz` and `/readyz` respond, both carrying `x-request-id` |
| All three fail-fast paths | exercised, real output captured |

### The skip-path bug worth remembering

The no-Postgres run originally took **172 seconds**: 42 skips, each paying a ~4-second TCP connect
timeout. The tests were skipping *correctly* and the suite was still useless — a correct result
delivered too slowly to run is not a passing suite. A module-level probe cache attempts the
connection once and reuses the verdict, giving **4.5 seconds**. Verified by pointing
`PRISM_DATABASE_URL` at a dead port.

### Re-running any of it

```bash
python -m pytest                                   # 83 tests
PRISM_DATABASE_URL=postgresql+asyncpg://x:x@localhost:59999/x python -m pytest   # skip path
python scripts/validate_pack.py
python scripts/init_db.py
```

The test suite creates and drops its own `prism_test` database, so it never touches development
data. Tests needing a live server are marked `postgres` and skip cleanly when it is unreachable.

---

## 7. Anticipated review questions

**Why Postgres and not SQLite for a project this size?**
Budget accounting requires an atomic increment. SQLite serialises writers, so a read-then-write
implementation passes there and loses updates in production — a test passing for the wrong reason.
There is a paired test: 40 concurrent atomic increments sum exactly; a deliberate read-then-write
across 10 tasks provably loses updates.

**SHA-256 for API keys? Shouldn't that be bcrypt?**
Not for this secret. Slow hashes defend low-entropy human-chosen passwords; a virtual key is
high-entropy and machine-generated. More decisively, bcrypt is per-row salted, so you cannot look up
by hash — you would load every tenant and run a deliberately slow comparison per row, per request.
SHA-256 gives a unique indexed column and one index hit. The admin token *does* use
`compare_digest`, because it is operator-chosen and a byte-wise compare would be a timing oracle.

**The data model lists seven entities and you built four tables.**
Three are deployment config with no mutable state and no inbound foreign keys; as tables they would
need a migration to change a price and a join to read one. Usage Record is a query over
`request_log`, because an aggregate written beside every log row is a dual write and dual writes
drift — and that reconciliation is graded, so the log is the single source of truth.
`budget_periods` is the one denormalisation, because budget reads sit on the admission path.

**Why 402 for budget exhaustion? Everyone returns 429.**
The contract permits either. `load_test.py` classifies every 429 as "rate limited", so a 429 here
would corrupt the over-admission figure that test exists to measure. And 402 is more honest to the
client: retrying later cannot help, which is why `Retry-After` is set for 429 and not for 402.

**Why is an unknown model a 404 rather than a 403?**
Because 403 would confirm the model exists. If unknown models 404 and forbidden models 403, the
status code becomes an oracle for the model registry to anyone holding any key.

**No migrations — isn't that a red flag?**
It is a deliberate deferral, recorded in Known limitations. There is no deployed database to
migrate, and while the schema changes hourly a migration per tweak costs review time and buys
nothing. The upgrade path is one `alembic init` plus one autogenerate. What migrations usually
protect you from is already handled: the serving process cannot run DDL, and startup verifies the
schema instead of creating it.

**How do you know the enforcement order is right?**
It is documented in the module that implements it, ordered cheapest-and-most-certain first. The one
non-obvious placement is allowlist before rate limit: otherwise a caller can burn a tenant's quota
with requests that could never succeed.

**Walk me through a request with a revoked key.**
`extract_bearer` pulls the credential; `require_tenant` hashes it and does one indexed lookup; the
row is found but `is_active` is false, so it raises `AuthenticationError` with the *identical*
message an unknown key receives. `prism_error_handler` renders an OpenAI-shaped body with
`type: authentication_error` and status 401, and no `Retry-After`. The response carries
`x-request-id`, and a `request_log` row is written with `tenant_id=NULL` and `team` denormalised
onto it.

**Why is `tenant_id` nullable on `request_log`?**
A 401 has no tenant — that is what the rejection means. `team` and `key_prefix` are denormalised
onto the row so rejections stay attributable and deleting a tenant does not erase the audit trail.
There is a test for precisely that.

**41 of 83 tests pass with no database. Why does that matter?**
It makes the suite usable in CI and on a fresh clone. Getting there meant fixing a real bug: the
no-Postgres path took 172 seconds because each of 42 skips paid a TCP connect timeout. Skipping
correctly but unusably slowly is still a broken suite.

**What is the weakest part of this?**
Provider ownership inferred from the model-name prefix. Nothing in the config states which provider
serves `alpha-large`, so it splits on the last dash. It is in Known limitations, the provided
validator relies on the same convention, and it is confined to one function — but it is a
convention masquerading as a contract.

**What would you change with more time?**
An ANN index for the cache; per-operator admin identity instead of one shared token; and an explicit
`models` list per provider to remove the name-prefix inference.

---

## 8. Known weaknesses, stated plainly

Kept here in full because the README's **Known limitations** section is a graded deliverable and
cannot be reconstructed honestly at the end.

1. **No data plane yet.** Everything in [section 1](#1-scope-what-day-1-is-and-is-not).
2. **No migrations.** `create_all` only; see 4.11 for what mitigates it.
3. **The admin plane is one shared token.** No rotation, no per-operator identity. The contract
   permits "any simple documented mechanism"; this is that, and its weakness is documented rather
   than disguised.
4. **No ANN index on the cache.** Similarity is a per-tenant scan in Python. Fine at fixture scale,
   linear in entries per tenant.
5. **Provider ownership is inferred from the model name.** See 4.12.
6. **Embedding is CPU-bound and single-process.** ONNX inference runs in `asyncio.to_thread`, so
   cache-lookup throughput is bounded by the default thread-pool executor.
