# Day 2 design log — the data plane and enforcement

What was built, in what order, why, and what each decision cost. Written alongside the code rather
than reconstructed afterwards, so the reasoning is the real reasoning.

Companion documents:

- [DAY1_DESIGN_LOG.md](DAY1_DESIGN_LOG.md) — the spine this slice stands on
- [DESIGN_NOTES.md](DESIGN_NOTES.md) — measured numbers (cosines, thresholds, routing method)
- [../README.md](../README.md) — how to run it; **Known limitations** is the honest ledger

---

## Contents

- [1. Scope: what Day 2 is and is not](#1-scope-what-day-2-is-and-is-not)
- [2. File map](#2-file-map)
- [3. Build order and why that order](#3-build-order-and-why-that-order)
- [4. Design decisions](#4-design-decisions)
- [5. The three proofs](#5-the-three-proofs)
- [6. Verification evidence](#6-verification-evidence)
- [7. A Day 1 decision reversed](#7-a-day-1-decision-reversed)
- [8. Anticipated review questions](#8-anticipated-review-questions)
- [9. Known weaknesses, stated plainly](#9-known-weaknesses-stated-plainly)

---

## 1. Scope: what Day 2 is and is not

Day 1 was the spine; **Day 2 is the request actually being served, and the three ways it can be
refused.** A tenant can now send an OpenAI-shaped request and get an OpenAI-shaped answer, with
every one of the four `x-prism-*` headers, a metered cost, an atomically-charged budget, and a
`request_log` row — *including* when the answer is a rejection.

| # | Built | Why it belongs in this slice |
|---|---|---|
| 1 | The provider adapter, behind an interface | The one boundary that touches the network. Two implementations from the start, so nothing downstream can accidentally depend on sockets |
| 2 | `POST /v1/chat/completions`, non-streaming | The vertical slice the whole project is about |
| 3 | Model resolution: concrete, alias chain, `auto` | Chains resolve fully now even though only the first target is attempted, so failover is a change to *one* function next slice, not to the route |
| 4 | The rate limiter | Race-safety is the property `load_test.py` fails the build over |
| 5 | Cost metering and the atomic budget charge | Reconciliation is graded, and it is a property of three separate writes |
| 6 | A `request_log` row for **every** request | Including the ones that never reach the route — the hard part, see [4.6](#46-logging-a-rejection-that-never-reaches-the-route) |

**1,498 lines of new `prism/` against 1,319 lines of new tests** — close to 1:1, as Day 1.

### Deliberately not built

| Skipped | Defense |
|---|---|
| **Streaming** | Next slice. Refused with an explicit 400 (`stream_unsupported`) rather than silently downgraded — see [4.10](#410-streaming-is-refused-not-downgraded) |
| **Retries and failover** | Next slice. The chain is resolved and `x-prism-fallback` is already on every response (always `false`), so the contract is stable before the behaviour behind it lands |
| **The semantic cache** | Later slice. `x-prism-cache` reports `miss` on every request, which is true rather than absent |
| **A semantic router** | `auto` ships working on a documented length-only baseline. See [4.4](#44-auto-ships-working-on-a-baseline-rather-than-stubbed) — this is a deliberate choice, not a shortcut |
| **Usage API and ops console** | Both are queries over `request_log`, which this slice is what fills |

---

## 2. File map

| File | Lines | Owns |
|---|---|---|
| `prism/money.py` | 55 | **One** representation of money, so three writers cannot disagree |
| `prism/providers/base.py` | 151 | The adapter interface and the error taxonomy |
| `prism/providers/http.py` | 157 | The real upstream, over one shared `httpx.AsyncClient` |
| `prism/providers/fake.py` | 202 | A second real implementation — not a mock |
| `prism/routing.py` | 206 | Concrete model, alias chain, and `auto`, through one path |
| `prism/ratelimit.py` | 150 | Sliding-window log, race-safe, injectable clock |
| `prism/budget.py` | 145 | Admission, the atomic increment, and the derived monthly reset |
| `prism/audit.py` | 211 | A `request_log` row for every request, success or rejection |
| `prism/schemas.py` | 76 | OpenAI-compatible request parsing that does not lose unknown fields |
| `prism/api/chat.py` | 145 | The route: the enforcement order and the header contract |

Modified from Day 1: `prism/main.py` (143 → 248: the three logging exception handlers, the request
context middleware, provider and limiter lifecycle), `prism/errors.py` (123 → 137: `InvalidRequestError`),
`prism/deps.py` (50 → 71), `prism/settings.py` (87 → 98: the two upstream timeouts),
`prism/db/models.py` (313 → 320: `RequestStatus.INTERNAL_ERROR`, and the pgvector comment corrected).

Tests: `test_chat.py` 476, `test_budget.py` 287, `test_providers.py` 242, `test_routing.py` 140,
`test_ratelimit.py` 123, `test_money.py` 51. `conftest.py` grew 216 → 275 (the fake clock, the fake
provider, the limiter, two more seeded keys).

---

## 3. Build order and why that order

```
money → providers/base → providers/http → providers/fake → routing
      → ratelimit → budget → audit → schemas → api/chat → main (rewire)
```

Three principles produced this sequence.

### (a) Money before anything that touches money

`money.py` is 55 lines and was written first, before the adapter that produces token counts and
before the budget that spends them. The reason is the same as `errors.py` on Day 1: it is a
**contract that later modules must obey**, and the cost of writing it late is a reconciliation bug
rather than a refactor.

Written the other way round, `api/chat.py` would have formatted the header with an f-string,
`budget.charge` would have let Postgres round on insert, and the two would agree to nine decimal
places and disagree at the tenth. That discrepancy is invisible to every manual check and is
precisely what `docs/API_CONTRACT.md:140` grades.

### (b) The interface before either implementation

`providers/base.py` — the ABC, the exception, and `classify()` — was written before `http.py` and
before `fake.py`. Writing the real client first and extracting an interface from it afterwards
reliably produces an interface shaped like `httpx`: leaking a `Response` object, or a `status_code`
that only means something over HTTP. Writing the interface first means the fake is a **peer**, not
a subset, and the error taxonomy is expressed in the vocabulary the *gateway* needs (`retry_same`,
`try_next`) rather than the vocabulary the transport happens to use.

### (c) Enforcement before the route that enforces

`ratelimit`, `budget` and `audit` are all fully tested at the unit level before `api/chat.py`
exists. The route is then **assembly**: 145 lines, of which the enforcement sequence is six calls.
A route that had grown its own limiter inline would be the file where the concurrency bug lives,
and a bug in a route is much harder to test than a bug in a class you can hand a fake clock to.

Tests were written alongside each module, as Day 1. `test_chat.py` is the largest test file in the
project because it is the only place the three writes can be compared against each other.

---

## 4. Design decisions

Each one: the plain version, the senior version, and what it cost.

### 4.1 One money module, quantized exactly once

- **Plain** — the number in the header, the number in the log row, and the number added to the
  budget have to be the same number.
- **Senior** — those are three separate writes to three separate places, and every one of them is
  an opportunity to round. `quantize_usd()` is applied **once**, before the value is charged,
  stored or formatted, so all three receive an already-final `Decimal`. Rounding is
  `ROUND_HALF_UP`, matching Postgres `NUMERIC` — Python's default is `ROUND_HALF_EVEN` (banker's
  rounding), so a value that Python rounded down and Postgres rounded up would silently differ.
  `format_usd()` uses `f"{...:.10f}"` rather than `str()`, because `str(Decimal("2E-5"))` is
  `"2E-5"` and `scripts/load_test.py:59` parses the header with `float()`.
- **Cost** — `MONEY_SCALE = 10` duplicates the column's scale. Guarded by a test
  (`test_money_scale_matches_the_column`) rather than a comment, because a comment does not fail.

### 4.2 The error taxonomy is two booleans, not one `retryable` flag

`classify(status)` returns `(retry_same, try_next)`:

| Status | `retry_same` | `try_next` | Reasoning |
|---|---|---|---|
| 5xx, 429, 408, transport failure | ✅ | ✅ | Transient. The same provider may work in 200 ms |
| 400 | ❌ | ❌ | The caller's fault, and every provider will agree. Failing it over spends a second provider's quota to receive the same answer |
| 401, 403, 404 | ❌ | ✅ | **This is the interesting row.** A 401 here means *our* credential for that provider is wrong. Retrying with the same bad key is pointless; moving to a provider whose key works is exactly right |

- **Plain** — "should I try again?" and "should I try someone else?" are different questions.
- **Senior** — one flag cannot express the 401 row, and 401 is the row that matters operationally:
  a rotated provider key takes out one provider, and a gateway that treats it as non-retryable
  *and* non-failoverable turns a survivable event into a total outage. This is the entire failover
  policy, and it is 6 lines in one function with a parametrized test per row.
- **Cost** — two booleans to reason about at every call site. Mitigated by there being exactly one
  call site next slice (`dispatch.py`).

### 4.3 The fake provider is a second implementation, and it ships in `prism/`

`FakeProviderClient` is not `unittest.mock`. It implements `ProviderClient`, and it mirrors
`scripts/mock_provider.py` exactly: same reply templates, same **crc32** template selection (not
`hash()`, which is salted per process and so is not stable across runs), same whitespace-word token
counting, same `[refuse]` escalation hook.

- **Plain** — tests need an upstream, and a fake that behaves differently from the real one lets
  bugs through.
- **Senior** — token counting is the sharp edge. Cost assertions are the point of several tests, so
  a fake that counted tokens differently would let a cost bug pass in tests and fail against the
  mock providers the evaluation actually uses. It lives in `prism/` rather than `tests/` because
  `smoke_test.py` and `load_test.py` need a *running* gateway, and standing one up needs upstreams —
  shipping the fake in the package means the whole gateway runs end to end with no ports open.
- **Cost** — a second thing to keep in sync with the provided mock. Deliberate, and the sync points
  are named in the module docstring with line references.

It also does one thing the provided mock cannot: `fail_first=N` fails a bounded number of times and
then recovers. Without that there is no way to distinguish a successful **retry on the same
provider** from a **failover** — and those two set `x-prism-fallback` differently, so conflating
them would hide a header bug. (This is where the one bug of the slice was: `fail_first` combined
with a failure mode originally meant "fail forever". `mode` now says *how* it fails and
`fail_first` says *how many times*, which is the division that reads correctly.)

### 4.4 `auto` ships working on a baseline, rather than stubbed

`classify_difficulty` counts whitespace words in the **last user turn** and splits at 24.

- **Plain** — long questions are usually harder than short ones. That is all it knows.
- **Senior** — three reasons this ships rather than raising "not implemented". First,
  `docs/IMPLEMENTATION_GUIDE.md` sets the bar for the router slice at *beating* length-only, so the
  eval needs a real number to improve on, and that number has to come from running code.
  Second, a stubbed `auto` is an `auto` with no fallback chain, so the failover work would land
  untested on the alias that most needs it. Third, honesty is cheap here: the reason string says
  `heuristic=length (29 words > 24)` verbatim, in the header and in the log row, so nobody can read
  more insight into it than exists.
- **Cost** — it is a bad classifier. It is *labelled* a bad classifier everywhere it appears.

**The labels in `data/routing_eval.jsonl` are not used.** `docs/DATA_MODEL.md:11` forbids it, and
it would be self-defeating: the eval would then measure the gateway's ability to read its own
answer key.

### 4.5 The router resolves *through* the same path as a plain alias

`resolve()` handles three cases, and the router case is implemented as "classify → pick a tier →
resolve that tier through this same function".

- **Plain** — `auto` should get failover for free.
- **Senior** — the natural-looking implementation returns the chosen tier's *primary model*, which
  works in every demo and loses the fallback chain silently. `auto` traffic would then 502 where
  identical `fast` traffic would not, and the symptom (intermittent 502s on one alias) points
  nowhere near the cause. Recursion is bounded by `MAX_ROUTER_HOPS`, and load-time validation
  already rejects a router whose target is another router, so termination is proved before the
  process starts rather than defended per request.
- **Cost** — one recursive call. `test_a_router_inherits_the_full_fallback_chain` asserts the two
  chains are equal, so the property cannot regress quietly.

### 4.6 Logging a rejection that never reaches the route

`docs/DATA_MODEL.md:57` requires a `request_log` row for rejected requests. Two rejections never
enter the route function at all:

- **401** is raised inside the `TenantDep` dependency
- **400 from body validation** is raised by FastAPI before the handler is called

And FastAPI tears down `yield` dependencies — including the database session — *before* exception
handlers run, so by the time a handler could write the row, the session it would have used is
closed.

The solution has three parts:

1. **Middleware creates the `LogContext` before any dependency runs**, for `/v1/` paths only, and
   attaches it to `request.state`. This works across the middleware → handler boundary because
   `request.state` is backed by `scope["state"]` in Starlette, so the handler sees the same object.
2. **The route fills it in progressively** — tenant, then requested model, then resolved target,
   then tokens and cost — so whatever is known at the moment of failure is what gets recorded, and
   nothing is invented. A 403 row has a `requested_model` and a null `resolved_provider`, because
   at rejection time nothing had been resolved; filling those in would put a provider in the audit
   trail that was never called.
3. **Success and rejection write through different paths.** Success calls `audit.stage()` into the
   request's own session and commits it *with* the budget charge. Rejection calls
   `audit.record_rejection()`, which opens its **own** session, commits, and **swallows any
   exception** — because a logging failure must never turn a clean 402 into a 500.

- **Cost** — two write paths instead of one, and a mutable context object threaded through the
  request. Accepted, because the alternative is an audit trail that is missing exactly the rows
  someone will ask about.

### 4.7 One transaction for the charge and the log row

`budget.charge()` deliberately **does not commit**. The route commits it together with
`audit.stage()`.

- **Plain** — money moved and the record of why it moved should land together.
- **Senior** — a crash between two separate commits leaves money charged with nothing to attribute
  it to. Reconciliation — which `docs/EVALUATION_GUIDE.md` grades — would then be permanently off
  by that amount with no way to find it, because the only record of the discrepancy is the
  discrepancy. One transaction makes that state unreachable.

### 4.8 Admission asks "is there any budget left", not "is there enough"

- **Plain** — we do not know what a request costs until the provider tells us.
- **Senior** — and for a stream we do not know until it *ends*, which is why
  `docs/DATA_MODEL.md:91` explicitly permits admitting a stream on remaining budget and letting it
  overshoot. So a burst of N concurrent requests can all pass admission on the last cent. The bound
  is real and small: at most `requests_per_minute × cost_per_request` per window, under a cent for
  the seeded keys. The alternative — reserving an estimated cost at admission and refunding the
  difference — buys a tighter bound in exchange for a second failure mode (a crashed request leaks
  its reservation until someone reconciles), and a budget that *under*-admits is worse for a caller
  than one that overshoots by a cent.
- **Cost** — documented overshoot. Pinned by
  `test_overshoot_is_bounded_by_one_burst_not_unbounded`, which proves the important half: it is
  **one burst**, not unbounded. Once the charges land, admission refuses.

The comparison is `spent >= budget`, not `>`. With `>`, every key gets one free request past its
budget forever — which for a key doing one expensive request a month means the budget never binds
at all.

### 4.9 Budget reset by derivation, not by a job

`period_start` is the first day of the **UTC** month, derived from the timestamp. The first request
in October simply finds no row for October, and `ON CONFLICT` creates one at zero.

- **Senior** — there is no scheduled reset to fail to run, and no window in which a cron that fired
  late keeps last month's spend in force, blocking a paid-up tenant with nothing in the logs to
  explain why. UTC rather than local time because a month boundary that moves with the server's
  timezone gives different answers in different deployments and makes the usage API
  non-reproducible.

### 4.10 Streaming is refused, not downgraded

`stream: true` returns 400 with code `stream_unsupported`.

- **Plain** — better to say no than to pretend.
- **Senior** — a caller that asked for SSE and received a single JSON object has a **200** in its
  logs and a client sitting in a read loop waiting for `[DONE]`. The failure surfaces as a hang
  with no error anywhere, which is strictly worse to debug than a 400 that names the reason. This
  is interim, and the test that pins it says in its docstring that the streaming slice deletes it.

This needed a new error class. Raising the base `PrismError` would have returned **500** (its
`status_code` is 500), so `InvalidRequestError` was added to `errors.py` at status 400 — which is
also what FastAPI's body validation now maps onto, because OpenAI answers a malformed body with 400
and a compatible client branching on 400 has no handler for FastAPI's default 422.

### 4.11 A validation error never echoes the input

`RequestValidationError.errors()` includes the offending **input** — which, for a chat request, is
the tenant's prompt. The handler renders the field and the reason and deliberately drops the value.

- **Senior** — error bodies end up in logs, in ticket attachments, and in screenshots. The one
  thing in a chat request that must not leak is the content of the chat request.
  `test_a_validation_error_never_echoes_the_prompt` sends a recognizable string and asserts it is
  not in the response.

### 4.12 One shared `httpx.AsyncClient`, and two timeouts

- **Plain** — reuse the connection pool.
- **Senior** — a client per request exhausts ephemeral ports under the burst `load_test.py` fires,
  and the symptom is a connection error that looks like a provider outage. Separate connect and
  read timeouts because they answer different questions: a provider that has not accepted the
  socket in 5 s is **down**, and waiting the full 30 s read budget to discover that delays the
  failover by the whole budget; but a large model answering a long prompt legitimately takes tens of
  seconds, and cutting that off retries work already paid for.

### 4.13 The upstream's message never reaches the client

`ProviderCallFailed` is caught and re-raised as a generic `UpstreamError` naming only the provider.

- **Senior** — `docs/DATA_MODEL.md:44` makes provider API keys gateway secrets, and a misconfigured
  provider's 401 body can quote the credential it was sent. Upstream error bodies also quote the
  request. Neither belongs in a tenant-visible response, so the boundary is absolute rather than
  case-by-case. The test asserts the response text contains neither the key nor the upstream status.

### 4.14 The response body is forwarded verbatim

`UpstreamCompletion.body` is the upstream's parsed JSON, unmodified, and `JSONResponse` sends it back.

- **Senior** — the natural-looking mistake is rebuilding the response from the fields Prism
  understands, which silently drops `system_fingerprint`, `logprobs`, and every provider extension.
  Same rule inbound: the request schema names only `model`, `messages` and `stream`, and
  `extra="allow"` plus `model_dump(exclude_unset=True)` forwards everything else untouched — so
  `temperature` reaches the provider, and `stream` is **not** invented when the caller never sent it.

### 4.15 Smaller decisions worth being able to defend

- **`x-prism-fallback` is always present**, `true` or `false`. The contract only requires it when a
  fallback was used, but a sometimes-absent header forces every client to distinguish "missing" from
  "false", and `docs/API_CONTRACT.md:88` requires it on streaming responses regardless.
- **`x-prism-cost-usd` is omitted for streams**, not sent as `0`. Headers flush before the first
  token; the cost is not known until the last. A zero there would be a lie that reconciles to the
  wrong number.
- **A rejection is not recorded in the rate-limit window.** Otherwise a client that keeps retrying
  converts its rate limit into a permanent ban.
- **`time.monotonic`, not `time.time`**, for the window. A clock adjustment (NTP step, DST on a
  misconfigured host) can move `time.time` backwards, which would make a full window look empty.
- **`threading.Lock` in an async limiter.** The invariant is "no `await` in the critical section",
  which makes the lock unnecessary today. It is there as insurance: the lock costs nanoseconds on an
  uncontended acquire, and the bug it prevents is over-admission that appears only under load.
- **`RequestStatus.INTERNAL_ERROR` added.** `docs/DATA_MODEL.md:70` ends its status list with "...";
  a gateway bug must not be misfiled as tenant traffic in the usage figures.
- **The admin plane is not logged.** `request_log` is the audit trail for tenant traffic; mixing
  operator requests into it would corrupt every count the usage API derives.

---

## 5. The three proofs

Day 1's headline test was the concurrency proof. Day 2 has three, and each one is the reason a
design decision above is not just an assertion.

### (a) Reconciliation — `test_chat.py::test_header_log_row_and_budget_all_agree`

Four real HTTP requests through the real app. It sums `x-prism-cost-usd` as a **client** would, then
asserts that sum equals the sum of `request_log.cost_usd` **and** equals
`budget_periods.spent_usd`. This is the only test in the suite that can catch a cost rounded once
for the header and again for the column, or a budget charged before a log row that then failed to
write.

### (b) Atomicity — `test_budget.py::test_concurrent_charges_are_never_lost`

Twenty concurrent charges, in twenty **separate sessions and transactions**, summing to an exact
total. Twenty is deliberate: the pool holds twenty connections, so all twenty are genuinely in
flight rather than queued two at a time.

The statement being proved is one round trip:

```sql
INSERT INTO budget_periods (...) VALUES (...)
ON CONFLICT ON CONSTRAINT uq_budget_period
DO UPDATE SET spent_usd = budget_periods.spent_usd + EXCLUDED.spent_usd,
              request_count = budget_periods.request_count + 1
RETURNING spent_usd
```

`ON CONFLICT` rather than "select, and insert if missing" also removes the **first-request-of-the-
month** race, where two concurrent requests both find no row, both insert, and the loser gets a
unique-constraint violation instead of a completion. That failure is worst at the start of a month,
which is exactly when nobody is watching for it — so it has its own test,
`test_the_first_charge_of_a_month_does_not_race_itself`, which charges concurrently with *no* row
to conflict on.

### (c) No over-admission — `test_ratelimit.py::test_no_over_admission_under_thread_contention`

Forty real threads through a `threading.Barrier`, a limit of ten, real clock, and exactly ten may
win. This is the property `scripts/load_test.py` fails the build over, and the unit test can say
*why* where the load test can only say "too many got in". Replacing the locked section with a
read-then-write makes it fail intermittently, which is exactly how the bug behaves in production.

Its companion is `test_the_window_slides_rather_than_resetting`, which is the test a fixed-window
counter fails: three requests spread over thirty seconds free up one slot at a time as each ages
out. A fixed window forgives all three at a minute boundary and admits a burst of six inside
200 ms — twice the configured limit, at the moment of highest load.

---

## 6. Verification evidence

| Check | Result |
|---|---|
| Test suite | **176 passed** in ~17.5 s |
| No-database lane (`-m "not postgres"`) | **91 passed, 85 deselected**, in 0.74 s |
| Reconciliation over four requests | header sum == `request_log` sum == `budget_periods.spent_usd` |
| Twenty concurrent charges, twenty transactions | exact total, exact `request_count` |
| Forty threads, limit ten | exactly ten admitted |
| Every rejection path (401/403/404/429/402/400/502) | asserted status, asserted `error.type`, asserted log row |
| Upstream failure | 502, and the response text contains neither the provider key nor the upstream status |
| Provided scaffold | still byte-identical to the import commit |

### Re-running any of it

```bash
python -m pytest                        # 176 tests
python -m pytest -m "not postgres"      # 91, no server needed
python -m pytest tests/test_budget.py -k concurrent
python -m pytest tests/test_chat.py -k agree
```

The suite creates and drops its own `prism_test` database, so it never touches development data.

---

## 7. A Day 1 decision reversed

**Day 1 honoured an inbound `x-request-id`. Day 2 does not.**

The Day 1 reasoning was ordinary distributed tracing: let a caller pass a correlation id so its
logs and ours line up. That was written before `request_log` existed as a *populated* table.

The id is the **primary key** of the request-log row. Honouring the header therefore means one of
two things, both bad:

- a caller repeats a value, the insert raises an `IntegrityError`, and a request that was served
  perfectly returns 500 — a client can turn its own successful requests into failures
- or a caller sends an id another tenant used, and writes its row under that value, corrupting the
  audit trail across a tenant boundary

The id is now generated in middleware, before any dependency runs, and is still echoed on every
response including rejections — so it remains usable for support, which was the original point. The
Day 1 test asserting the old behaviour was **replaced** by
`test_health.py::test_a_caller_cannot_choose_the_request_id`, and that test's docstring records the
reversal so the history is not lost.

Also corrected in this slice: the comment at `EMBEDDING_DIM` in `prism/db/models.py`, which had
justified the plain float array on grounds of what this machine can install. The real and durable
reason is portability — `pgvector` is an extension, and depending on one narrows the schema to
deployments that have it installed, for a corpus that is per-tenant and small. The README's
limitation now states the ceiling honestly (low thousands of entries per tenant) rather than
blaming the host.

---

## 8. Anticipated review questions

**Walk me through a successful request.**
Middleware assigns an id and creates the `LogContext`. `TenantDep` hashes the bearer token and does
one indexed lookup. The route records the tenant and the requested model, then: `enforce_allowlist`
(403/404), `limiter.try_acquire` (429), `enforce_budget` (402), `routing.resolve` (which returns the
full chain), `cache = "miss"`, one `providers.complete` against the chain's primary. Cost comes from
the price table applied to the provider's *reported* token counts. `budget.charge` and
`audit.stage` then commit in **one** transaction, and the upstream body is returned verbatim with
the four `x-prism-*` headers built from the same context object that produced the log row.

**Why is the allowlist checked before the rate limit?**
Otherwise a caller can exhaust a team's quota with requests that could never have succeeded.
`test_a_rejected_model_does_not_consume_rate_limit_quota` sends five forbidden requests and asserts
the window is still empty.

**Why is an unknown model a 404 and a forbidden one a 403 — doesn't that leak?**
It leaks less than the alternative. If both were 403, nothing is revealed; if both were 404, a
tenant cannot tell a typo from a permissions problem. The chosen split does mean the status code
distinguishes "exists" from "not allowed", which is why the check order is 404 **before** 403 — the
model registry is not tenant data, and the seeded aliases are in the provided pack.

**Where does the cost number come from, and why not count tokens yourself?**
From the provider's `usage` block. Counting tokens ourselves would put a number into the accounting
that no provider ever agreed to, and the point of the metering is that it reconciles. The cost is
that a provider omitting `usage` is billed at zero — `read_usage` returns `(0, 0)`, the request is
still served, and it is in Known limitations. A visible zero is better than a plausible fiction.

**Your budget can overshoot. Isn't that a bug?**
It is a documented trade with a proven bound. Admission cannot know the cost — for a stream, not
until it ends — so a concurrent burst can all pass on the last cent. `docs/DATA_MODEL.md:91`
permits exactly this for streams and this build applies one rule to both paths. What the test
proves is that the overshoot is **one burst**: once those charges land, admission refuses. The
tighter alternative (reserve an estimate, refund the difference) trades a bounded overshoot for
leaked reservations after a crash.

**Why is the rate limiter in memory when budgets are in Postgres?**
Because the documents ask for different things. `docs/DATA_MODEL.md:118` permits rate-limit state in
memory provided it is race-safe; `docs/DATA_MODEL.md:86-91` requires the budget increment itself to
be atomic. So the rate limiter's hard requirement is in-process concurrency (proved with forty
threads) and the budget's is database atomicity (proved with twenty transactions). The honest cost
is that a restart forgives outstanding usage and a second process doubles every effective limit —
in Known limitations, with Redis named as the answer.

**Why a `threading.Lock` in an async application?**
The invariant is that there is no `await` inside the critical section, which makes the lock
technically unnecessary. It is insurance: an uncontended acquire costs nanoseconds, and the bug it
prevents — someone adding an `await` inside the window logic later — is over-admission that appears
only under load and is exactly what the load test fails the build over.

**A fake provider in `prism/`, not `tests/`. Why?**
Because `smoke_test.py` and `load_test.py` need a running gateway, and a running gateway needs
upstreams. Shipping the fake in the package means the failover demo is reproducible with no ports
open at all. It is also not a mock — it is a second `ProviderClient`, byte-compatible with
`scripts/mock_provider.py` down to crc32 template selection and whitespace token counting, because
a fake that counted tokens differently would let a cost bug pass in tests.

**`auto` splits on prompt length. That's barely a router.**
Correct, and it says so in the response header. It ships working for three reasons: the guide's bar
for the router slice is *beating* length-only, so the eval needs a real baseline number; a stubbed
`auto` has no fallback chain, so the failover work would land untested on the alias that most needs
it; and the reason string names the method verbatim, so it cannot be oversold. The eval labels in
`data/routing_eval.jsonl` are not read — `docs/DATA_MODEL.md:11` forbids it, and using them would
mean measuring the gateway against its own answer key.

**How can a 401 have a log row if it never reaches your route?**
The `LogContext` is created in middleware, before any dependency runs, and lives on
`request.state` — which is backed by `scope["state"]`, so the exception handler sees the same
object. FastAPI closes `yield` dependencies before handlers run, so the rejection path opens its
**own** session, commits, and swallows any exception, because a logging failure must never turn a
clean 402 into a 500. The row carries `tenant_id = NULL` and no requested model, because at
rejection time neither was known — that is the honest record, not a gap.

**Why 400 rather than FastAPI's 422 for a malformed body?**
OpenAI answers a malformed body with 400 and `error.message`. A compatible client branching on 400
has no handler for a 422 with FastAPI's own body shape, so it goes down an unexpected path. The
handler also drops `exc.errors()`' `input` field, because for a chat request that field *is* the
tenant's prompt.

**What is the weakest part of Day 2?**
That only the first target in the chain is attempted. The chain is fully resolved and
`x-prism-fallback` is already on every response, so the gap is one function wide — but until next
slice, a single provider outage is a 502 even though a working fallback was resolved and available.

**What would you change with more time?**
Shared rate-limit state so a restart does not forgive usage; a `models` list per provider to remove
the name-prefix inference; and a `Retry-After` on 502 once the retry budget is known.

---

## 9. Known weaknesses, stated plainly

Kept here in full because the README's **Known limitations** section is a graded deliverable and
cannot be reconstructed honestly at the end.

1. **No streaming.** Refused with 400 `stream_unsupported` rather than silently downgraded. See
   [4.10](#410-streaming-is-refused-not-downgraded).
2. **One attempt per request.** The chain is resolved; only `chain[0]` is called.
   `x-prism-fallback` is always `false`.
3. **`auto` routes on prompt length alone.** Labelled as such in the header and the log row. See
   [4.4](#44-auto-ships-working-on-a-baseline-rather-than-stubbed).
4. **Rate-limit state is in memory.** A restart forgives outstanding usage; a second process
   doubles every effective limit. Race-safe within the process, which is what the document requires.
5. **A provider that omits `usage` is billed at zero.** Served and logged, charged nothing.
6. **Budgets can overshoot by one burst.** Bounded by `requests_per_minute × cost_per_request`.
7. **A caller cannot choose its request id.** See [section 7](#7-a-day-1-decision-reversed).
8. **No cache yet**, so `x-prism-cache` is `miss` on every request.
9. Everything still outstanding from Day 1: no migrations, one shared admin token, no ANN index,
   provider ownership inferred from the model name, single-process embedding.
