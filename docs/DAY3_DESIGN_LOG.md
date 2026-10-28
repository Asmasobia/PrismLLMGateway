# Day 3 design log — resilience and streaming

What was built, in what order, why, and what each decision cost. Written alongside the code rather
than reconstructed afterwards, so the reasoning is the real reasoning.

Companion documents:

- [DAY1_DESIGN_LOG.md](DAY1_DESIGN_LOG.md) — the spine: config, errors, schema, the concurrency proof
- [DAY2_DESIGN_LOG.md](DAY2_DESIGN_LOG.md) — the data plane this slice makes resilient
- [DESIGN_NOTES.md](DESIGN_NOTES.md) — measured numbers (cosines, thresholds, routing method)
- [../README.md](../README.md) — how to run it; **Known limitations** is the honest ledger

---

## Contents

- [1. Scope: what Day 3 is and is not](#1-scope-what-day-3-is-and-is-not)
- [2. File map](#2-file-map)
- [3. Build order and why that order](#3-build-order-and-why-that-order)
- [4. Design decisions](#4-design-decisions)
- [5. The three proofs](#5-the-three-proofs)
- [6. Verification evidence](#6-verification-evidence)
- [7. Two Day 2 decisions reversed](#7-two-day-2-decisions-reversed)
- [8. Anticipated review questions](#8-anticipated-review-questions)
- [9. Known weaknesses, stated plainly](#9-known-weaknesses-stated-plainly)

---

## 1. Scope: what Day 3 is and is not

Day 2 served one request against one provider. **Day 3 is the two things that happen when that stops
being enough:** the answer arrives token by token instead of all at once, and the provider that was
supposed to produce it does not.

They are one slice rather than two because they meet in a single question — *how much of the answer
has already left the building?* — and because answering it once, in the shape of the provider
interface, is what stops streaming from quietly having no failover.

| # | Built | Why it belongs in this slice |
|---|---|---|
| 1 | `open_stream` / iterate, on the provider interface | The retry rule for a stream depends entirely on which side of this line the failure happened. Making it two methods makes the rule structural — see [4.1](#41-the-openiterate-split-is-the-mid-stream-policy-written-as-an-interface) |
| 2 | `prism/dispatch.py` — retries, jittered backoff, failover | Guide Step 5. Two nested loops, one module, so the policy is statable rather than emergent |
| 3 | SSE relay with no buffering, `[DONE]` terminator | Guide Step 4. The demo criterion is that a human *sees* tokens appear (`docs/IMPLEMENTATION_GUIDE.md:96`) |
| 4 | Usage and cost read from the final chunk | A stream cannot be metered before it is sent, and `docs/API_CONTRACT.md:88` still requires the cost to reach the log row |
| 5 | A documented mid-stream failure policy | `docs/IMPLEMENTATION_GUIDE.md:172` asks for a decision, not a specific one. Error event, then `[DONE]`; never a splice |
| 6 | `x-prism-fallback` and `request_log.retries` telling the truth | Both existed on Day 2 with nothing behind them. This is the slice where they start being facts |

**818 lines of new `prism/` against 681 lines of new tests.** Slightly further from 1:1 than Day 2,
for one legible reason: `dispatch.py` is 218 lines of which roughly half is the docstring stating the
policy, and its test file needs neither a database nor a socket, so it is unusually cheap per branch.

### Deliberately not built

| Skipped | Defense |
|---|---|
| **A circuit breaker** | `data/gateway_config.sample.json` supplies `degradation` thresholds and they are parsed into `DegradationPolicy` and not consulted. Without a breaker a dead provider costs *every* request its full retry budget before failover; with one, the first few requests pay and the rest skip straight to the fallback. It is the right stretch item and it is a stateful, per-provider, time-windowed thing — i.e. a slice, not a corner of this one. The bound today is stated: `max_attempts × chain length` upstream calls per request |
| **Honouring an upstream `Retry-After`** | A provider's own 429 backoff is ignored in favour of Prism's schedule. Reading it as a *floor* is a five-line change; what makes it safe to defer is that neither `scripts/mock_provider.py` nor the fake sends one, so it would be untested code on the retry path |
| **Passing an upstream 400 through as a 400** | See [4.15](#415-smaller-decisions-worth-being-able-to-defend). The status is useful; the message that explains it is the upstream's and cannot be echoed, so it would be a status with no explanation |
| **Resuming a dead stream** | Not deferred — refused. `docs/IMPLEMENTATION_GUIDE.md:172` rules out splicing, and there is no partial-completion API to resume from |
| **The semantic cache, usage API, ops console** | Later slices. `x-prism-cache` still reports `miss`, which is true rather than absent |

---

## 2. File map

| File | Lines | Owns |
|---|---|---|
| `prism/dispatch.py` | 218 | **New.** The retry and failover policy: two nested loops, and nothing else |
| `tests/test_dispatch.py` | 337 | **New.** That policy, with no database and no socket — the fast lane |

Modified:

| File | Lines | What changed |
|---|---|---|
| `prism/providers/base.py` | 151 → 229 | `ProviderStream`, `open_stream` on the interface, `parse_sse_data`, `DONE_SENTINEL` |
| `prism/providers/http.py` | 157 → 294 | `HttpProviderStream`: a manually-entered `httpx` stream, line-by-line, usage captured as it passes |
| `prism/providers/fake.py` | 202 → 336 | `FakeProviderStream`, the `die_after_chunks` behaviour, and `unauthorized` (401) |
| `prism/api/chat.py` | 145 → 377 | Chain dispatch on both paths, `start_stream`, `relay`, `settle` |
| `prism/deps.py` | 71 → 83 | `DispatcherDep` |
| `prism/main.py` | 248 → 255 | The dispatcher is created once in the lifespan and lives on `app.state` |
| `tests/test_chat.py` | 476 → 785 | Four retry/failover tests, eleven streaming tests, one deletion |
| `tests/conftest.py` | 275 → 310 | `RecordingSleeper`, the `dispatcher` fixture, and both app factories taking one |

`prism/audit.py`, `prism/db/models.py` and `prism/routing.py` are **unchanged**. That is the payoff
from Day 2's decision to resolve the full chain and ship `fallback`, `retries` and `streamed`
columns before anything could set them: this slice is new behaviour behind an already-agreed
contract, not a schema change.

---

## 3. Build order and why that order

```
providers/base (the split + ProviderStream) → providers/http → providers/fake
      → dispatch → api/chat: non-streaming path → api/chat: streaming path → main (rewire)
```

### (a) The interface change before either implementation, again

Day 2's rule was "the interface before either implementation", and Day 3 obeys it for the same
reason at a higher stake. The **first** thing written was `open_stream` returning a `ProviderStream`,
and the docstring explaining why that is two operations rather than one.

Written the other way round — `httpx` first, extract the interface afterwards — the natural shape is
a single `stream()` async generator, because that is what `httpx` gives you. And a single generator
cannot express the one thing this slice exists to express: *failures before the first byte are
retryable, failures after it are not*. With one method, that distinction survives only as a comment
and a habit. With two, `dispatch.py` retries what `open_stream` raises because that is the callback
it was handed, and cannot retry what iteration raises because iteration happens somewhere it has
already returned from.

### (b) The policy before either caller

`dispatch.py` and its 337-line test file were finished before `api/chat.py` was touched. The route
then *chose* nothing: it hands the dispatcher a lambda and reads three fields off the result.

The alternative — write the retry loop inside the non-streaming path, then generalise it when
streaming needs it — is the standard way to end up with streaming that has no failover at all,
because at the moment streaming lands the retry loop is entangled with a `JSONResponse` and
"generalise it" is a refactor rather than a call.

### (c) Non-streaming before streaming, on the same loop

The non-streaming path was converted to `dispatcher.run` first, and its tests made green, before the
streaming branch existed. That ordering isolated the two hard things from each other: whether
failover *works* was settled against a code path with no generators, no deferred session and no
half-sent response, so every failure encountered in the streaming path afterwards was a streaming
failure.

### (d) The streaming route last, because it is the only place the request outlives its session

`relay` is the one function in the project where work continues after the route has returned, and
that is where both bugs of the slice were ([section 6](#6-verification-evidence)). It was written
last, with the dispatcher already proven and the non-streaming meter already reconciling, so its
tests were testing lifecycle rather than arithmetic.

Tests were written alongside each module, as Day 1 and Day 2. `test_dispatch.py` was written *with*
`dispatch.py` and deliberately given no database and no HTTP client, so control flow feedback stays
in the one-second lane.

---

## 4. Design decisions

Each one: the plain version, the senior version, and what it cost.

### 4.1 The open/iterate split is the mid-stream policy, written as an interface

`ProviderClient` has two streaming-related obligations and they are separate methods:

```python
async def open_stream(self, target, payload) -> ProviderStream: ...   # may fail freely
def __aiter__(self) -> AsyncIterator[str]: ...                        # failures are terminal
```

`open_stream` returns only once the upstream has accepted the request — status line and headers
received, **nothing forwarded to the client**. So a failure it raises is indistinguishable from a
non-streaming failure, and is retried and failed over by the same code. A failure raised while
*iterating* carries `retry_same=False, try_next=False`, and both `HttpProviderStream` and
`FakeProviderStream` set those flags at the raise site with the reason in a comment.

- **Plain** — once the customer has half the answer, you cannot start over with a different writer.
- **Senior** — `docs/IMPLEMENTATION_GUIDE.md:172` names splicing two providers' outputs as the
  unacceptable behaviour. The tempting way to honour that is a check in the dispatcher: *if this was
  a stream and it had started, do not fail over*. That check is a fact about the caller living in the
  callee, it is invisible at the raise site, and the day someone adds a third call style it is
  quietly wrong. Making it the difference between two methods means the rule is enforced by the
  shape of the interface: the dispatcher has already returned by the time iteration begins, so it
  *cannot* retry, whatever anyone later believes.
- **Cost** — two methods where one generator would do, and `HttpProviderStream` has to enter
  `httpx`'s stream context manager **manually** (`aclose` performs the matching `__aexit__`), because
  the response must outlive the function that opened it. That is a real leak risk, paid for with a
  `finally` in `relay` and an idempotent `aclose`.

### 4.2 The dispatcher never inspects a status code

`Dispatcher.run` reads exactly two things off a failure: `exc.retry_same` and `exc.try_next`. It
never looks at `exc.status`.

- **Plain** — the thing that saw the response decides what it means; the retry loop just obeys.
- **Senior** — the alternative is a second copy of `classify()`'s table, in a second module, and two
  copies of a policy table disagree the first time somebody adds a status to one of them. Worse, the
  duplicate is only reachable through the retry path, so the disagreement shows up as *intermittent*
  failover behaviour under load. Keeping the table in one function also means the transport-level
  failures that have **no** status at all — a connect timeout, a dropped socket — flow through the
  same two booleans as an HTTP 503 rather than needing a parallel branch.
- **Cost** — every adapter must set the flags correctly, including future ones. Mitigated by
  `classify()` being the only sensible way to produce them from a status, and by the fake reading
  them through the real `classify` rather than hard-coding pairs.

### 4.3 The operation is a callback, so streaming inherits failover for free

```python
Operation = Callable[[ResolvedTarget], Awaitable[T]]
```

`run(chain, operation)` is generic over what one attempt *is*. The non-streaming path passes
`providers.complete`; the streaming path passes `providers.open_stream`. There is no
`run_streaming`.

- **Plain** — one retry loop, two things to retry.
- **Senior** — a retry loop per call style is how a gateway ends up with a well-tested
  non-streaming failover and a streaming path that 502s on the first hiccup, and nobody notices
  because the streaming tests all use a healthy provider. Generic-over-`T` also keeps the dispatcher
  honest about its own ignorance: it cannot inspect the result, so it cannot grow a "was this
  actually a good answer" heuristic, which is the kind of thing that turns a retry policy into a
  quality filter nobody asked for.
- **Cost** — the callback closes over `payload` and the target's concrete model, so the
  alias-to-model substitution happens inside the lambda, once per attempt. That is correct rather
  than incidental — the second provider in a chain has a *different* model name — and
  `test_an_alias_reaches_the_upstream_as_a_concrete_model` plus the failover tests' payload
  assertions pin it.

### 4.4 Two nested loops, and the attempt budget is per target

```
for target in chain:                      # failover, outer
    for attempt in range(max_attempts):   # retry, inner
```

`max_attempts=3` against a two-provider chain is up to **six** upstream calls, not three.

- **Plain** — each provider gets its own three chances.
- **Senior** — the other reading (three attempts for the whole request) means a chain of four
  providers never reaches the last two, which makes the fourth entry in a config's fallback list
  decoration. `docs/IMPLEMENTATION_GUIDE.md:56` asks for both behaviours in one sentence — "retry
  transient errors with exponential backoff; on a down or rate-limited provider, move to the next in
  the chain" — and they only compose as nested loops. Flattened into one loop with a counter, the
  code cannot express "this provider is finished, that one is not".
- **Cost** — the worst case is `max_attempts × len(chain)` upstream calls and the backoff of all of
  them, per request, for as long as the primary is down. That is exactly the number a circuit
  breaker would cut, it is stated in the README, and `test_the_attempt_budget_is_per_target_not_per_request`
  asserts the six calls so the bound cannot drift silently.

### 4.5 Nothing sleeps before moving to the next provider

Backoff is applied **only** between retries of the same target. The move from `alpha` to `beta`
contributes no delay of its own.

- **Plain** — waiting does not make a *different* provider healthier.
- **Senior** — a backoff is a bet that the same endpoint will recover shortly; against a different
  endpoint the bet has no subject. The delay would be spent out of the caller's latency budget for
  nothing, and it compounds: with a sleep before each failover, a four-provider chain adds three
  pointless waits to every request during an outage, on top of the retries. The clean way to see the
  property is the recorded delay list — `[0.2, 0.4, 0.2, 0.4]` across a failover, not
  `[0.2, 0.4, x, 0.2, 0.4]` — which is what `test_backoff_grows_exponentially_between_retries`
  asserts.

### 4.6 Full jitter, with the jitter source injected

`delay_seconds(i)` is `policy.backoff_ms(i) * jitter() / 1000`, where `jitter` defaults to
`random.random`. The computed backoff is a **ceiling**, not the delay.

- **Plain** — if everything that failed together retries together, the pile-up happens again.
- **Senior** — N requests that hit the same provider at the same failure share a deadline, so
  without jitter the herd that just overwhelmed the provider arrives again intact, having only
  paused; the provider sees the same instantaneous load one backoff later and the outage extends
  itself. Full jitter — a uniform sample from `[0, backoff]` rather than the backoff itself — is
  what AWS's analysis of the three variants recommends on total work done, and it is one
  multiplication.
- **Cost** — the delay is no longer deterministic, so a test that asserted `[0.2, 0.4]` would be
  asserting on `random`. `sleep` and `jitter` are both constructor parameters: tests pin the jitter
  to `1.0` and get exact ceilings, `test_the_backoff_is_jittered_not_fixed` pins it to `0.25` and
  asserts `[0.05, 0.1]` so the multiplication is visible, and no test in the suite waits. A faithful
  test of one exhausted chain would otherwise add 1.2 s of pure sleeping to every future run.

### 4.7 `retries` counts the whole chain; `resolved_provider` is where it ended up

`Dispatched.attempts` counts every upstream call the request made, including calls to earlier
targets, and `retries = attempts - 1`.

- **Plain** — "how many extra upstream calls did this cost?"
- **Senior** — the alternative, counting only retries of the *final* target, reports `retries = 0`
  for a request that hammered a dead provider three times before failing over — which is precisely
  the request an operator is hunting for. The column then reads as "how healthy was the provider
  that served me", which nobody needs, instead of "what did this request cost us", which is what a
  usage API and a cost investigation both want. Same logic for `resolved_provider`: on a failure it
  records the **last** target attempted, so the row answers "where did this request end up" rather
  than "where did it start". Both are asserted end to end — a failed-over 502 logs
  `resolved_provider = "beta"`, `fallback = true`, `retries = 5`.
- **Cost** — `retries` and `fallback` are no longer independent readings; a `retries = 3` row may be
  three retries of one provider or two-plus-one across two. `fallback` disambiguates, and the
  `attempted` tuple on `ChainExhausted` keeps the exact sequence for the log line.

### 4.8 `ChainExhausted.target` comes from the loop, not from the exception

The exhaustion exception carries `target=last_target`, tracked by the failover loop, rather than
`last.target` from the final `ProviderCallFailed`.

- **Senior** — this was a test failure that turned into a design change. `test_dispatch.py`'s
  `failing()` helper builds a `ProviderCallFailed` with no `target`, because a unit test of the
  taxonomy has no reason to attach one; `exhausted.target` came back `None` and the assertion on
  `resolved_provider` failed. The easy fix is to make the helper attach a target. The better fix is
  to notice what the test was actually demonstrating: **an adapter that forgets to attach a target
  to its exception silently costs the audit trail the one field that says where the request ended
  up**, and that is a real class of bug in a future third adapter, not a test artefact. The
  dispatcher always knows which target it just called, so it is the right source of that fact.
- **Cost** — one more local in the loop, and a redundant path (`last.target` is usually the same
  value). Worth it: the redundancy is what makes the field robust rather than conventional.

### 4.9 Chunks are forwarded as raw JSON text, never re-serialised

`ProviderStream` yields `str`. `relay` interpolates it: `f"data: {chunk}\n\n"`.

- **Plain** — pass on exactly what the provider sent.
- **Senior** — the same rule as Day 2's verbatim response body, and it bites harder here. A
  parse-then-re-serialise round trip drops fields Prism has never heard of, reorders keys, and
  changes how floats render — per chunk, hundreds of times per response. Prism *does* need to read
  usage out of the chunks, and `HttpProviderStream._capture_usage` does exactly that: it parses a
  copy for accounting, forwards the original text, and swallows a parse failure so a dialect this
  gateway does not understand still reaches a client that might. `parse_sse_data` is also stricter
  than "split on `data:`" — SSE permits comment lines, `event:`, `id:` and blank separators, and
  treating a blank line as an empty chunk would emit `data: \n\n` for every separator the upstream
  sent, doubling the event count any client-side parser reports.
- **Cost** — the gateway cannot rewrite a chunk (to normalise a model name, say) without giving this
  up. Nothing in the contract asks it to.

### 4.10 The stream is opened before the response object exists

`start_stream` calls `open_with_failover` **first**, and only then constructs the
`StreamingResponse`.

- **Plain** — the headers have to be true when they are sent, and they are sent before the first
  token.
- **Senior** — `x-prism-provider` and `x-prism-fallback` flush with the status line, ahead of the
  body, so they must be facts by then; they only become facts once *some* upstream has accepted the
  request. Opening inside the generator instead — the shape that falls out naturally, since that is
  where the iteration lives — means the headers are guesses (the primary, `fallback: false`) that a
  failover then invalidates, unfixably, because they are already on the wire. It has a second payoff:
  a chain that cannot open **at all** raises before any response exists, so it becomes a normal JSON
  502 with a normal status line, rather than a `200 text/event-stream` whose entire body is one error
  event. A client that branches on status code gets the right answer.
  `test_a_chain_that_cannot_open_a_stream_is_a_json_502` asserts the status *and* the content type.
- **Cost** — time-to-first-byte now includes the whole failover walk. During an outage that is up to
  the full retry budget before the client sees anything at all, which is the same bound as the
  non-streaming path and is the circuit breaker's argument.

`x-prism-cost-usd` is **omitted** on a stream rather than sent as `0` — Day 2's decision, and this is
the slice where it stops being hypothetical. `prism_headers(context, with_cost=False)`. A zero there
would be a number that never reconciles; absence is what `docs/API_CONTRACT.md:88` asks for, and the
real cost reaches the log row when the stream ends.

### 4.11 A mid-stream death gets an error event — and `[DONE]` anyway

```
data: {"error": {"message": "The upstream provider stopped responding part-way …", …}}

data: [DONE]
```

- **Plain** — say what went wrong, then close the stream properly.
- **Senior** — this is the policy `docs/IMPLEMENTATION_GUIDE.md:172` asks every implementation to
  decide and document, and it is the acceptable option it names. Two sub-decisions inside it. First,
  the error is an **event in the stream**, not a status code: the status line went out as 200 many
  chunks ago and cannot be recalled. Second, `[DONE]` is emitted **after** the error event, which
  looks odd — the stream did not finish successfully — and is right anyway: a client that never
  receives a terminator sits in its read loop until its own timeout expires, so the truncation the
  user was going to see regardless arrives as a hang with no explanation instead of an error. Prism
  also swallows the *upstream's* `[DONE]` and emits its own, so an upstream that dies without one
  still yields a properly terminated client stream.
- **Cost** — a client that only checks "did I get `[DONE]`?" concludes success. That is why the error
  event is OpenAI-shaped (`{"error": {...}}`, via the same `UpstreamError.body()` the 502 path uses),
  so a client that parses events at all finds it in the place its own library already looks.

### 4.12 The row is written from the generator's `finally`, in its own session

`relay` ends with a `finally` that closes the stream, copies the final token counts off it, computes
the cost, and calls `settle` — which opens a **new** `Database` session, re-reads the tenant with
`session.get(Tenant, tenant_id)`, charges the budget, stages the row, commits, and never raises.

Four things are load-bearing here.

1. **A new session, because the request's session is gone.** FastAPI closes `yield` dependencies
   before the response body streams. Using `session` from the route would work on the version where
   the ordering happens to be lucky and raise on the next one — the worst kind of dependency.
2. **`session.get(Tenant, tenant_id)` rather than the `tenant` object.** That instance belongs to a
   closed session; touching an expired attribute on it raises `DetachedInstanceError` from inside a
   `finally`, which is where exceptions are hardest to attribute. The route therefore passes
   `tenant.id`, an `int`, across the boundary — not the ORM object.
3. **`context.deferred = True`.** The exception handlers write a row for anything that failed, and a
   streaming request looks, from their side, like a route that returned successfully with no row
   written. `audit.record_rejection` checks the flag and leaves it alone, so a stream cannot be
   logged twice.
4. **`settle` never raises.** By the time it runs the response has been delivered in full, so an
   exception cannot be reported to the caller — it can only replace a served stream with a broken
   one. It logs with `logger.exception` and returns. Same reasoning as Day 2's rejection path: a
   logging failure must not damage a request that succeeded.

And one rule that is a comment in the code because it cost a real debugging session: **you may
`await` inside that `finally`, but you must never `yield`.** A `yield` there is an extra chunk after
the terminator on the happy path, and during a client disconnect it raises "async generator ignored
`GeneratorExit`" — which surfaces as a mystery traceback with no request attached.

### 4.13 A failed stream logs `http_status = 200` and tells the truth in `status`

`settle(..., status="upstream_error")` still stages `http_status=200`.

- **Plain** — 200 is what actually went out on the wire.
- **Senior** — the two columns answer different questions and conflating them corrupts one of them.
  `http_status` is the transport fact, and it is the column someone joins against a proxy log or a
  client's own metrics; writing 502 there would mean the gateway's audit trail disagrees with every
  other record of the same request, and the person who notices is the person debugging at the worst
  moment. `status` is the outcome, and it is where `upstream_error` belongs — so the usage API can
  count failed streams without pretending a 502 was sent.
  `test_a_stream_that_dies_mid_response_is_terminated_not_spliced` asserts both: the row's `status`
  is `upstream_error` **and** its `http_status` is 200.
- **Cost** — a naive "count the 5xx rows" query misses failed streams. Any query that means "how
  many requests failed" should read `status`, which is why the column exists.

### 4.14 The dispatcher is app-scoped and holds no per-request state

One `Dispatcher` is built in the lifespan, lives on `app.state.dispatcher`, and is injected by
`DispatcherDep`. Its counters (`attempts`, `attempted`, `failure`, `last_target`) are **locals** of
`run`.

- **Plain** — one shared object, no shared mutable state.
- **Senior** — a `self._attempts` would pass every single-request test in this file and report
  nonsense the moment two requests overlapped, which is every moment in production; the symptom is a
  `retries` column that is wrong only under load, i.e. only where nobody can reproduce it. App-scoped
  rather than per-request because the object is genuinely stateless and because the policy is
  process-wide configuration — and because that is where a circuit breaker's state will eventually
  need to live, shared across requests by design.
- **Cost** — a test that wants no real sleeping has to inject its sleeper at construction, so the
  `dispatcher` fixture is threaded through both app factories in `conftest.py`.
  `test_one_dispatcher_serves_concurrent_requests_independently` is the guard.

### 4.15 Smaller decisions worth being able to defend

- **`Cache-Control: no-cache` and `X-Accel-Buffering: no` on every stream.** SSE behind a proxy is
  the classic place buffering appears: nginx will happily accumulate the whole response and deliver
  it in one write, which passes every automated check and fails the only one a human runs
  (`docs/IMPLEMENTATION_GUIDE.md:96`).
- **No `Content-Length` on a stream**, asserted by a test. Its presence would mean something
  buffered the body to measure it.
- **An upstream 400 becomes a 502.** `docs/API_CONTRACT.md:113` maps "all providers failed" to 502,
  and a request no provider will accept is that case. Passing 400 through would be more useful to
  the caller and is deliberately not done, because the *message* explaining it is the upstream's and
  may quote the request or the credential Prism sent (`docs/DATA_MODEL.md:44`) — so it would be a
  status with no explanation. Recorded in Known limitations rather than argued as ideal.
- **`stream: True` is set twice** — by `ChatCompletionRequest.upstream_payload(..., stream=True)` and
  again inside `HttpProviderClient.open_stream`. Not redundancy: it is a transport-level invariant of
  that method (an upstream answering with one JSON body would make `aiter_lines` produce a single
  unparseable line), so it is asserted where it is required rather than trusted from above.
- **An empty chain raises `ValueError`**, not a 502. It is unreachable through `routing.resolve`,
  whose `Route.chain` is never empty; making it explicit means a future caller gets an exception
  rather than a silent no-op that looks exactly like a provider outage.
- **`fake.py` gained `unauthorized` (401), which the provided mock has no mode for.** 401 is the one
  status where `retry_same` and `try_next` disagree, so it is the only way to demonstrate end to end
  that the two flags are independent rather than one flag renamed twice.
- **`die_after_chunks` is the second thing the mock provider cannot do**, and the more important
  one: it kills a stream that has *already delivered output*. That is the exact case the guide asks
  every implementation to decide, and it is untestable over a socket without deliberately crashing a
  server mid-write.
- **`_attempt()` is shared by the fake's `complete` and `open_stream`**, so a provider scripted as
  `down` is down for both. A fake where `set(mode="down")` affected only one would make the
  streaming failover tests pass for the wrong reason.
- **`aclose()` is idempotent** on both stream implementations and is called from a `finally`, so a
  client that disconnects halfway releases the upstream connection instead of leaking it for the
  rest of the process.

---

## 5. The three proofs

### (a) The two flags are genuinely independent — `test_dispatch.py::test_a_bad_credential_is_failed_over_without_being_retried`

A 401 from `alpha`, run through the real `classify`. The assertion is three lines and each one
matters: `calls == ["alpha/alpha-small", "beta/beta-small"]` (it did move on), `fallback is True`
(and said so), `sleeps.delays == []` (and did not back off first, because backing off before a
handover to a different provider is latency spent on nothing).

This is the test a single `is_retryable` boolean cannot pass. With one flag, a rotated provider key
either hammers the provider whose key is wrong or never reaches the provider whose key is right —
and a rotated key is an ordinary Tuesday, so the design either survives it or turns it into a total
outage. Its companion, `test_a_malformed_request_is_neither_retried_nor_failed_over`, pins the other
end: a 400 costs exactly **one** upstream call.

### (b) A dying stream is terminated, never spliced — `test_chat.py::test_a_stream_that_dies_mid_response_is_terminated_not_spliced`

`die_after_chunks=3` against a healthy fallback, through the real app over real HTTP. Four
assertions, in the order they matter:

- exactly **5** SSE events: three good chunks, one error event, `[DONE]`
- event index 3 parses as an OpenAI-shaped error with `type: upstream_error`
- **`providers.calls == ["alpha/alpha-small"]`** — the no-splice proof. `beta` is healthy, resolved,
  and next in the chain, and it is never called
- the `request_log` row has `status = upstream_error` and `http_status = 200`

The third assertion is the one to read twice. It passes not because the route checks whether output
has been sent, but because the failure was raised with `retry_same=False, try_next=False` and the
dispatcher had already returned. Deleting a check would not break it; only changing the flags at the
raise site would — which is the point of [4.1](#41-the-openiterate-split-is-the-mid-stream-policy-written-as-an-interface).

`test_a_dying_stream_leaks_nothing_to_the_client` is its shadow: the same scenario, asserting the
provider's key and the upstream's own message are absent from every byte the client received.

### (c) One dispatcher, concurrent requests — `test_dispatch.py::test_one_dispatcher_serves_concurrent_requests_independently`

Four `run()` calls on one shared `Dispatcher`, gathered, each scripted to fail a different number of
times: `attempts == [1, 2, 3, 4]` and `fallback == [False, False, False, True]` — the fourth
exhausted `alpha`'s three attempts and fell over.

Every other test in the file passes with the counters stored on `self`. This is the only one that
does not, and it is testing the property that actually holds in production, where no request ever
runs alone.

---

## 6. Verification evidence

| Check | Result |
|---|---|
| Test suite | **211 passed** in 33.2 s (Day 2: 176) |
| No-database lane (`-m "not postgres"`) | **112 passed, 99 deselected**, in 1.0 s (Day 2: 91) |
| Retry on the same provider | one recorded delay of 0.2 s, `retries = 1`, `fallback = false` |
| Failover after an exhausted target | delays `[0.2, 0.4]`, `x-prism-provider: beta/beta-small`, `retries = 3` |
| 400 from an upstream | exactly one upstream call, 502 to the client |
| 401 from an upstream | two upstream calls, no backoff, served by the fallback |
| Whole chain down | 502, and the response text contains neither the provider key nor the upstream status |
| Streamed content | reassembled deltas are byte-identical to the same prompt served non-streamed |
| Streamed metering | `streamed = true`, tokens from the final chunk, cost > 0, `budget_periods.spent_usd` equals the row |
| Mid-stream death | 5 events, error event at index 3, one provider called, row `upstream_error` / `http_status 200` |
| Stream that died before its usage chunk | logged, charged zero — the documented trade |
| Streaming under an exhausted budget | 402 with a JSON content type, no upstream call |

### Live verification against real sockets

The fake had been the only upstream until this slice, and an in-process fake cannot prove that bytes
leave the process one chunk at a time. So: two `scripts/mock_provider.py` instances on **9001/alpha**
and **9002/beta**, `scripts/init_db.py`, and uvicorn on **8080**.

`scripts/smoke_test.py` → **14 passed, 2 warnings, 2 failed**. Both failures and both warnings are
the semantic cache, which is not built. The streaming checks specifically reported:

```
content-type is text/event-stream
received multiple SSE chunks (19 data lines)
stream ends with [DONE]
```

Then failure injection, `POST /admin/config {"mode": "down"}` on alpha:

- non-streaming failed over — `x-prism-provider: beta/beta-small`, `x-prism-fallback: true`
- streaming failed over — 21 data lines, last one `data: [DONE]`
- both providers down — `502 {"error": {"message": "No upstream provider could serve this request.", …}}`

And the audit trail for those three requests, read straight out of `request_log`:

```
ok              http=200 prov=beta fb=True  retries=3 stream=False cost=0.0000077000 tok=5/18 lat=1359ms
ok              http=200 prov=beta fb=True  retries=3 stream=True  cost=0.0000077000 tok=5/18 lat=1781ms
upstream_error  http=502 prov=beta fb=True  retries=5 stream=False cost=0E-10        tok=0/0   lat=2500ms
```

That table is the whole slice in three rows: identical cost for the streamed and non-streamed
answer (so usage really is being read from the final chunk), `retries=3` for a failover after three
attempts at a dead primary, `retries=5` and `prov=beta` for the exhausted chain, and `lat=2500ms`
which is the retry budget being spent — the number a circuit breaker exists to cut.

### The two bugs of the slice

Both were in the same place, and both were the request outliving its session.

1. **`DetachedInstanceError` from inside `finally`.** `settle` was passed the route's `tenant`
   object. By the time the stream ended, the session that loaded it was closed. Fixed by passing
   `tenant.id` and re-reading — [4.12](#412-the-row-is-written-from-the-generators-finally-in-its-own-session), point 2.
2. **"async generator ignored `GeneratorExit`"** on a client disconnect, from a `yield` that had
   drifted below the metering code. Fixed by moving every `yield` above the `finally` and leaving a
   comment saying why nothing may be yielded there.

Neither is visible in a happy-path test, which is why the streaming suite includes a disconnect case
and a die-mid-response case rather than only asserting on well-behaved streams.

### Re-running any of it

```bash
python -m pytest                              # 211 tests
python -m pytest -m "not postgres"            # 112, no server needed
python -m pytest tests/test_dispatch.py       # the whole retry/failover policy, ~0.2 s
python -m pytest tests/test_chat.py -k stream
```

The suite creates and drops its own `prism_test` database, so it never touches development data.

---

## 7. Two Day 2 decisions reversed

**(a) Day 2 refused `stream: true` with a 400. Day 3 serves it.**

Day 2's [4.10](DAY2_DESIGN_LOG.md#410-streaming-is-refused-not-downgraded) argued that a caller who
asks for SSE and receives one JSON object gets a 200 in its logs and a client waiting for a
terminator that never comes — a hang with no error anywhere, strictly worse to debug than a 400 that
names the reason. That reasoning was about the *interim*, and it holds: refusing was right for a day
and downgrading would not have been.

This is the planned reversal rather than a change of mind, and the shape of it was pinned in
advance: `test_streaming_is_refused_explicitly_for_now` said in its own docstring that the streaming
slice deletes it. It is deleted, and eleven streaming tests stand in its place.

`InvalidRequestError`, added in Day 2 purely so that refusal could be a 400 instead of the base
`PrismError`'s 500, stays — it is what FastAPI's body validation now maps onto, which is the reason
it earns its keep independently.

**(b) `resolved_provider` on a failure now means the last provider tried, not the first.**

`test_all_providers_failing_is_502_and_leaks_nothing` asserted `resolved_provider == "alpha"` on
Day 2 and asserts `"beta"` on Day 3. On Day 2 there was no distinction to make — only `chain[0]` was
ever called — so the assertion was recording an accident of the implementation as though it were a
contract.

The reversal is deliberate and stated in `record_failure`'s docstring: the row answers "where did
this request end up", not "where did it start". The assertion was not merely updated but
strengthened, to `resolved_provider == "beta"`, `fallback is True`, `retries == 5`, and the exact
call sequence `["alpha/alpha-small"] * 3 + ["beta/beta-small"] * 3` — so the row now pins the whole
walk rather than one field of it.

---

## 8. Anticipated review questions

**Walk me through a streaming request.**
Identical to a non-streaming one through authentication, allowlist, rate limit, budget and chain
resolution — that is deliberate, they diverge as late as possible. Then `start_stream` calls the
dispatcher with `open_stream` as the operation, so the chain is walked with retries and backoff
*before* any response exists. Once one upstream has accepted, `context.streamed` and
`context.deferred` are set, and a `StreamingResponse` goes out with `x-prism-provider`,
`x-prism-cache`, `x-prism-fallback`, `Cache-Control: no-cache` and `X-Accel-Buffering: no` — and
deliberately no `x-prism-cost-usd`, because that number does not exist yet. `relay` then forwards
each chunk's raw JSON text as its own SSE event, emits `[DONE]`, and in a `finally` reads the token
counts off the stream object, prices them, and calls `settle`, which opens its own session, charges
the budget and writes the row in one transaction.

**Why is the retry logic not in the route?**
Because it would then exist twice — once per call style — and the second copy would be the streaming
one, written later, tested less. In `dispatch.py` it is one loop taking a callback, so
`open_stream` and `complete` get literally the same policy, and the policy is written down in one
docstring rather than distributed across a route function.

**What stops you from failing over in the middle of a stream?**
The interface, not a check. `open_stream` returns before any byte reaches the client, so failures it
raises are freely retryable; failures raised while *iterating* carry `retry_same=False,
try_next=False` and happen after the dispatcher has already returned, so there is no loop left to
retry them. `docs/IMPLEMENTATION_GUIDE.md:172` forbids splicing two providers' output onto one
answer, and the test proving Prism does not
(`test_a_stream_that_dies_mid_response_is_terminated_not_spliced`) asserts that a *healthy,
resolved* fallback is never called.

**Why two booleans instead of one `retryable` flag?**
Because 401 needs both answers to be different. An upstream 401 means Prism's stored credential for
that provider is wrong: retrying is pointless, failing over to a provider whose key works is exactly
right. One flag forces a choice between hammering the broken provider and never reaching the working
one, and a rotated key is a routine event that would then become a full outage.

**Three attempts at each provider — isn't that nine calls on a three-provider chain?**
Yes, and that is the intended reading. Three attempts for the whole *request* would mean the third
and later entries in a fallback list are never reached, which makes them decoration. The bound is
`max_attempts × len(chain)` upstream calls plus their backoff, it is stated in the README, and a
test asserts the exact six calls for the two-provider chain so it cannot drift. Cutting it is what
the `degradation` config is for, and that is honestly listed as not built.

**Why no backoff before trying the next provider?**
A backoff is a bet that the *same* endpoint will be healthier shortly. A different endpoint is not
more likely to answer because we waited 400 ms first, and the wait comes out of the caller's latency
budget. The recorded delay sequence across a failover is `[0.2, 0.4, 0.2, 0.4]`, with nothing at the
handover.

**Why jitter, and how do you test something random?**
Without jitter, every request that failed at the same instant retries at the same instant, so the
herd that overwhelmed a provider arrives again intact and the outage extends itself. Full jitter
samples uniformly from `[0, backoff]`, so the computed backoff is a ceiling. It is testable because
the jitter source is injected: pinned to `1.0` the delays are exact ceilings, pinned to `0.25` the
test asserts `[0.05, 0.1]` and the multiplication is visible. `sleep` is injected for the same
reason — a faithful test of one exhausted chain would add 1.2 s of sleeping to every future run of
the suite and prove nothing the recorded delays do not.

**Your `retries` column counts calls to providers that did not serve the request. Isn't that wrong?**
It is the useful definition. Counting only retries of the final target reports `retries = 0` for a
request that hit a dead provider three times before failing over — the exact request an operator is
looking for. The column means "how many extra upstream calls did this request cost", and `fallback`
plus the log line's `attempted` sequence say where they went.

**Why does a stream write its own log row instead of using the request's session?**
Because FastAPI closes `yield` dependencies before the response body streams, so that session is
gone by the time the last chunk is sent. `relay`'s `finally` opens a new one via the `Database`
object, re-reads the tenant by id — the route's ORM instance is detached, and touching it would
raise from inside a `finally` — and charges plus logs in one transaction. `context.deferred` tells
the exception handlers not to write a competing row. And `settle` never raises: the response has
already been delivered, so an exception there could only convert a served stream into a broken one.

**A failed stream logs `http_status = 200`. Isn't that a lie?**
It is the only true value. 200 is what went out on the wire, hundreds of chunks before the failure,
and `http_status` is the column someone joins against a proxy or client log. The outcome lives in
`status`, which reads `upstream_error`. Writing 502 into `http_status` would make the audit trail
disagree with every other record of the same request.

**Why send `[DONE]` after an error event? The stream failed.**
Because the alternative punishes the client for the upstream's failure. The user is going to see a
truncated answer either way; without a terminator they see nothing at all until the client's own read
timeout fires, and the error event that explains it is sitting unparsed in a buffer. The event is
OpenAI-shaped, so a client that parses events finds it where its library already looks.

**A client disconnects halfway. Who pays?**
The tenant. Those tokens were generated and billed by the upstream, so the charge and the row are
written from `relay`'s `finally` whether the client stayed or not. The alternative — cancelling the
charge on disconnect — makes hanging up early a way to get free inference. It is in Known
limitations because it is a trade, not an obvious right answer.

**What if the stream dies before the usage chunk?**
It is billed at zero, logged with zero tokens, and has its own test. Usage arrives in the final
chunk (`scripts/mock_provider.py:203`), so a stream cut short has none to read. Same trade as a
provider that omits `usage` entirely: a number no provider agreed to is worse than a visible zero
when the point of the metering is that it reconciles.

**What is the weakest part of Day 3?**
No circuit breaker. A provider that is down costs *every* request its full retry budget — three
attempts and roughly 600 ms of backoff before the failover even starts — indefinitely, and the live
run above shows it as a 2,500 ms latency on the both-down request. The config already carries the
thresholds; nothing reads them.

**What would you change with more time?**
Trip the breaker on `degradation`'s error-rate and p95 thresholds and probe occasionally; honour an
upstream `Retry-After` as a floor on the backoff; and add a `Retry-After` to Prism's own 502 now
that the retry budget is a known quantity.

---

## 9. Known weaknesses, stated plainly

Kept here in full because the README's **Known limitations** section is a graded deliverable and
cannot be reconstructed honestly at the end.

1. **A stream that dies mid-response cannot be salvaged.** The client keeps the partial answer plus
   an error event; Prism never restarts on another provider.
   See [4.11](#411-a-mid-stream-death-gets-an-error-event--and-done-anyway).
2. **A stream that dies before its final chunk is billed at zero.** Usage arrives last, so there is
   none to read.
3. **A client that disconnects mid-stream is still charged.** Those tokens were generated upstream.
4. **There is no circuit breaker; `degradation` is parsed and unused.** A down provider costs every
   request `max_attempts × chain length` upstream calls and their backoff, indefinitely.
5. **An upstream `Retry-After` is ignored.** A provider's 429 is backed off on Prism's schedule, so a
   retry can arrive before the provider is ready.
6. **An upstream 400 becomes a 502.** The status is not passed through, because the message that
   would explain it is the upstream's and cannot be echoed
   (`docs/DATA_MODEL.md:44`). See [4.15](#415-smaller-decisions-worth-being-able-to-defend).
7. **`auto` still routes on prompt length alone.** Labelled as such in the header and the log row.
8. **Rate-limit state is still in memory.** A restart forgives outstanding usage; a second process
   doubles every effective limit.
9. **A provider that omits `usage` is billed at zero**, and **budgets can overshoot by one burst** —
   both unchanged from Day 2, both bounded and tested.
10. **No cache yet**, so `x-prism-cache` is `miss` on every request.
11. Everything still outstanding from Day 1: no migrations, one shared admin token, no ANN index,
    provider ownership inferred from the model name, single-process embedding.
