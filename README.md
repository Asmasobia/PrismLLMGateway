# Prism — LLM Gateway and Semantic Cache

One OpenAI-compatible API in front of multiple LLM providers. Prism makes two decisions on every
request — **how hard is this prompt** (which model tier deserves it) and **have we answered this
before** (semantic cache) — while enforcing per-tenant keys, rate limits, and cost budgets, and
streaming tokens through without buffering.

> The supplied project scaffold is exactly the contents of commit `84cf8cb`: all of `data/`, the
> four scripts `validate_pack.py`, `mock_provider.py`, `smoke_test.py`, `load_test.py`, and the six
> provided documents now in `docs/`. Everything else is my own work — including later additions to
> those same directories, such as `docs/DESIGN_NOTES.md` and `scripts/pg.sh`.

**Status: in development.** Sections below marked _TBD_ are not yet implemented. This README is
written incrementally alongside the build rather than at the end.

## Contents

- [Setup](#setup)
- [Environment variables](#environment-variables)
- [Seeding keys and configuring providers](#seeding-keys-and-configuring-providers)
- [Running the gateway and mock providers](#running-the-gateway-and-mock-providers)
- [Reproducing the demo](#reproducing-the-demo)
- [Tests](#tests)
- [API overview](#api-overview)
- [Verification: smoke, load, and the routing eval](#verification-smoke-load-and-the-routing-eval)
- [Architecture and design decisions](#architecture-and-design-decisions)
- [Known limitations](#known-limitations)

## Setup

Requires **Python 3.12+** and **Postgres**.

```bash
py -3.12 -m venv .venv          # Windows; use python3.12 elsewhere
source .venv/Scripts/activate   # Git Bash; .venv/bin/activate on macOS/Linux
python --version                # expect 3.12.x
```

Validate the provided data pack before anything else:

```bash
python scripts/validate_pack.py
# Pack OK: 4 priced models, 2 providers, 3 aliases
#          4 seed tenants, 14 sample requests, 20 routing eval cases
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Then start Postgres and write the two local config files:

```bash
docker compose up -d                                  # Postgres 16 on port 5433
cp .env.example .env                                  # edit PRISM_MODEL_CACHE + PRISM_ADMIN_TOKEN
cp data/gateway_config.sample.json gateway_config.json # provider registry and aliases
```

Both are gitignored: `.env` holds machine-specific paths and the admin token, and
`gateway_config.json` holds provider API keys, which are gateway secrets
(`docs/DATA_MODEL.md:44`). The sample in `data/` is committed and safe — its keys are the mock
providers' placeholders.

Create the schema and seed the tenants:

```bash
python scripts/init_db.py
# schema created (existing tables left untouched)
# created 4 tenant(s): search, research, free-tier, budget-demo
# 4 tenant(s) ready. 2 provider(s), 3 alias(es), 4 priced model(s).
```

This is a separate step from starting the gateway on purpose. The serving process **verifies** the
schema but never creates it: a gateway that runs its own DDL will happily build an empty schema in
the wrong database after a typo in `PRISM_DATABASE_URL`, then reject every request with a 401 that
looks like an auth bug. `init_db.py` is idempotent — re-run it after editing `data/seed_keys.json`.

### Postgres without Docker

Postgres is required rather than SQLite. Concurrent `UPDATE ... SET spent = spent + ?` needs to be
genuinely atomic; SQLite serialises writers, so budget accounting would reconcile without the
increment ever being atomic — passing for the wrong reason. (This is about budget accounting, not
rate limiting: `docs/DATA_MODEL.md:118` permits in-memory rate-limit state provided it is
race-safe, so the over-admission `scripts/load_test.py` hunts for is an in-process bug.)

Where Docker is unavailable or unwanted, Postgres also runs from EnterpriseDB's **binaries zip**,
which needs no installer, no Windows service and no elevation. One-time setup:

```bash
PGH="$LOCALAPPDATA/prism-postgres"; mkdir -p "$PGH"
curl -Lo "$PGH/pg.zip" https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip
unzip -q "$PGH/pg.zip" -d "$PGH"

printf 'prism' > /tmp/pgpw
"$PGH/pgsql/bin/initdb.exe" -D "$PGH/data" -U prism \
    --auth=scram-sha-256 --pwfile=/tmp/pgpw -E UTF8 --locale=C
rm -f /tmp/pgpw
```

Thereafter:

```bash
./scripts/pg.sh start      # then createdb once: ./scripts/pg.sh psql -c 'create database prism'
./scripts/pg.sh status
./scripts/pg.sh psql
./scripts/pg.sh stop
```

Uninstall by deleting `%LOCALAPPDATA%\prism-postgres`. Nothing is registered with the OS.

### The embedding model is not installed by pip

The semantic cache uses `BAAI/bge-small-en-v1.5` as quantized ONNX (384 dimensions, 65 MB). It is
treated as a **vendored artifact**: it must be on disk before the gateway starts, and is never
fetched on the request path — a gateway that reaches a model host mid-request has an unadvertised
dependency and an unbounded tail latency. Point `PRISM_MODEL_CACHE` at the cache directory and set
`HF_HUB_OFFLINE=1`. `scripts/fetch_model.py --fetch` downloads it at a pinned revision and then
checks every file's SHA256; run with no arguments it verifies what is already on disk, offline.
Rationale and the measured similarity numbers are in [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md).

## Environment variables

Copy [.env.example](.env.example) to `.env`; `.env` is gitignored because it holds machine-specific
paths and the admin token.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PRISM_MODEL_CACHE` | **yes** | — | Directory holding the pre-downloaded ONNX embedding model. Startup fails fast if unset or empty. |
| `HF_HUB_OFFLINE` | recommended | — | Set to `1` to refuse any network fetch of model weights even on a cache miss. |
| `PRISM_DATABASE_URL` | **yes** | — | Async Postgres DSN, e.g. `postgresql+asyncpg://prism:prism@localhost:5433/prism`. |
| `PRISM_ADMIN_TOKEN` | **yes** | — | Bearer token for the admin plane, separate from tenant virtual keys (`docs/API_CONTRACT.md:31`). |
| `PRISM_GATEWAY_CONFIG` | no | `gateway_config.json` | Provider registry, aliases and fallback chains; start from `data/gateway_config.sample.json`. |
| `PRISM_PRICING_FILE` | no | `data/model_pricing.json` | Price table used for cost accounting. |
| `PRISM_SEED_FILE` | no | `data/seed_keys.json` | Tenants loaded by `scripts/init_db.py`. |
| `PRISM_UPSTREAM_TIMEOUT_SECONDS` | no | `30` | Read timeout per upstream attempt. Generous on purpose: a large model answering a long prompt legitimately takes tens of seconds, and cutting it off retries work already paid for. |
| `PRISM_UPSTREAM_CONNECT_TIMEOUT_SECONDS` | no | `5` | Connect timeout. Short on purpose: a provider that has not accepted the socket in five seconds is down, and waiting the read timeout to learn that delays failover by the whole budget. |
| `PRISM_CACHE_TTL_SECONDS` | no | — (never expire) | Cache entry lifetime, deployment-wide. Unset means entries never expire, which is what makes a paraphrase demo reproducible — an entry written during setup is still there when the camera is on. |
| `PRISM_HOST` | no | `0.0.0.0` | Bind address. |
| `PRISM_PORT` | no | `8080` | Listen port. The provided test scripts default to `:8080`. |

Startup fails immediately, with a message naming the fix, if the model cache directory is absent,
if `PRISM_DATABASE_URL` does not use the `postgresql+asyncpg://` driver, if the gateway config is
missing or internally inconsistent, or if the schema has not been initialised. All four are
deployment mistakes; discovering any of them on the first request turns a config typo into a 500.

## Seeding keys and configuring providers

Four demo tenants ship in [data/seed_keys.json](data/seed_keys.json) with budgets, rate limits,
model allowlists, and per-key cache settings. The `budget-demo` key carries a deliberately tiny
budget so budget exhaustion is demonstrable live. Provider registry, model aliases, and fallback
chains come from [data/gateway_config.sample.json](data/gateway_config.sample.json); pricing for
cost accounting from [data/model_pricing.json](data/model_pricing.json).

`python scripts/init_db.py` loads them (see [Setup](#setup)). Two things it deliberately does:

- **It never stores a raw key.** The `tenants` table holds SHA-256 of each virtual key plus a
  non-secret prefix (`prism-sk-search`) for admin display. Lookup hashes the presented key and finds
  the row by index, so a database dump yields no working credentials. SHA-256 rather than
  bcrypt/argon2 because a slow hash defends *low-entropy human-chosen* secrets against offline
  guessing; a virtual key is high-entropy and machine-generated, so a slow hash would buy nothing
  and cost ~100 ms on every request.
- **It validates each allowlist against the loaded config.** A tenant granted a model that does not
  exist would get a 404 for a model it was explicitly allowed — which reads as a gateway bug rather
  than the config typo it is.

Tenants live in Postgres because they have mutable state that must survive a restart and rows that
point at them. Providers, aliases and prices do not, so they stay in config files and are loaded and
validated once at startup — no migration to change a price, no join on the hot path to read one.

## Running the gateway and mock providers

Two zero-dependency mock upstreams, in separate terminals:

```bash
python scripts/mock_provider.py --port 9001 --name alpha
python scripts/mock_provider.py --port 9002 --name beta
```

This serves `alpha-small`, `alpha-large`, `beta-small`, `beta-large` — free, offline, and
OpenAI-compatible, which makes failover deterministic. Failures can be injected live without a
restart (`{"mode": "down"}`, `{"mode": "rate_limited"}`, `{"fail_rate": 0.3}`,
`{"latency_ms": 3000}`) via `POST /admin/config`; see [docs/PROVIDED_PACK.md](docs/PROVIDED_PACK.md).

Starting the gateway:

```bash
python -m uvicorn prism.main:app --host 127.0.0.1 --port 8080
# INFO  prism 0.1.0 ready: 2 providers, 3 aliases
curl -s localhost:8080/readyz
# {"status":"ready","version":"0.1.0","providers":2,"aliases":3,"priced_models":4}
```

## Reproducing the demo

The seeded keys in [data/seed_keys.json](data/seed_keys.json) are chosen so that every behaviour worth
demonstrating can be triggered from a cold start, with no editing of config. Their differences are the
point:

| Key | Budget | Rate | `model_allowlist` | Cache | Demonstrates |
|---|---|---|---|---|---|
| `prism-sk-search-1a2b3c` | $50 | 60 rpm | `fast` | on, 0.92 | the header contract, streaming, semantic cache |
| `prism-sk-research-4d5e6f` | $500 | 300 rpm | `fast` `smart` `auto` | **off** | `auto` routing, and failover without cache interference |
| `prism-sk-free-7g8h9i` | $5 | 10 rpm | `fast` | on, 0.85 | rate limiting — 15 requests shows the 429s |
| `prism-sk-budget-demo-0j1k2l` | $0.00001 | 60 rpm | `fast` | off | budget exhaustion — one request spends it |

With the stack running (above), [scripts/demo_env.sh](scripts/demo_env.sh) defines those keys plus
thin `curl` wrappers, so no request has to be typed by hand:

```bash
source scripts/demo_env.sh
# demo_env loaded. keys: S R F B A | helpers: ask err stream burst | drills: down up slow flaky | reset
```

`ask KEY MODEL PROMPT` prints the status line and the four `x-prism-*` headers and nothing else;
`err` adds `Retry-After` and the error body; `stream` timestamps each SSE line to the millisecond;
`burst` sends 15 sequential requests; `down` / `up` / `slow` / `flaky` inject failures into mock
alpha. Source it from the repository root — `reset` calls `./scripts/pg.sh` by relative path.

### The demo, in order

```bash
curl -s localhost:8080/readyz                              # 2 providers, 3 aliases, 4 priced models

ask "$S" fast "What is a bloom filter used for?"           # alpha/alpha-small, miss, cost > 0
stream "$S" fast "Explain what a write-ahead log does"     # progressive SSE, no cost header

ask "$S" fast "$P1"                                        # miss  — a new question
ask "$S" fast "$P1"                                        # hit   — exact hash match, zero cost
ask "$S" fast "$P2"                                        # hit   — paraphrase, cosine 0.98 vs 0.92

ask "$R" auto "$HARD"                                      # alpha-large — short but hard
ask "$R" auto "$EASY"                                      # alpha-small — long but trivial

burst                                                      # 10 × 200, then clean 429s
err "$B" fast "first and last request"                     # 200 — spends the whole budget
err "$B" fast "this one should be refused"                 # 402 budget_exceeded, no Retry-After

down                                                       # kill mock alpha, live
ask "$R" fast "What is a circuit breaker?"                 # beta/beta-small, fallback: true
up
ask "$R" fast "What is a circuit breaker?"                 # alpha/alpha-small, fallback: false
```

Then open `http://localhost:8080/console` — any username, `PRISM_ADMIN_TOKEN` as the password.

Two things that will otherwise surprise you:

- **Cache entries never expire** unless `PRISM_CACHE_TTL_SECONDS` is set, which is deliberate so a
  demo entry survives setup. A prompt used once is a `hit` forever after; clear with
  `./scripts/pg.sh psql -c "delete from cache_entries;"` before a run that needs to show a `miss`.
- **Use the research key for the failover steps.** On a cache hit, `x-prism-provider` names the
  provider that *originally produced* the answer — so with a cache-enabled key the restore step
  replays beta's outage-era answer at zero cost and looks like failover failing to recover. Research
  has caching off, so every request there reaches a provider.

## Tests

```bash
python -m pytest
```

The suite splits along two lines, and both **skip** rather than fail when their dependency is
absent, so config parsing, pricing arithmetic, key hashing and the error table stay runnable on a
machine with no database, no model and no network:

| Marker | Needs | Notes |
|---|---|---|
| `postgres` | a reachable server | Runs against a separate `prism_test` database, created on demand, schema dropped and rebuilt per test — the suite can never touch the database you demo from. |
| `model` | `PRISM_MODEL_CACHE` populated | The only tests that load the vendored ONNX model, and so the only ones that can say anything about routing or similarity *quality*. Seconds rather than milliseconds. |

```bash
python -m pytest -m "not postgres"          # no database
python -m pytest -m "not postgres and not model"   # no database, no model: the fast lane
python -m pytest -m model                   # routing and similarity quality
```

A skip is a loud one: the `model` lane resolves `PRISM_MODEL_CACHE` from the environment *and* from
`.env`, because an earlier version read only the environment and silently skipped the whole lane on a
machine where the model was present — a green run that proved nothing.

## API overview

Data plane is `POST /v1/chat/completions`, OpenAI-compatible, streaming and non-streaming. Every
response carries the `x-prism-*` header contract. The normative spec is
[docs/API_CONTRACT.md](docs/API_CONTRACT.md).

Implemented so far:

| Route | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | virtual key | Chat completions, streaming and non-streaming. Enforces the allowlist, the rate limit and the budget; retries and fails over down the resolved chain; meters cost; writes a `request_log` row. |
| `GET /admin/usage` | admin token | Requests, tokens and cost for a date window, broken down by key and totalled. |
| `GET /admin/logs` | admin token | The newest request-log rows, newest first. Metadata only — never prompt or completion text. |
| `GET /admin/cache/stats` | admin token | Hits, misses, hit rate, stored entries, and the tokens and dollars the cache saved. |
| `GET /admin/providers/health` | admin token | Per-provider request counts, error rate, latency, and a health verdict computed from the `degradation` thresholds. |
| `GET /admin/keys` | admin token | Per-key policy and this period's spend. No key material — prefixes only. |
| `GET /console` | admin token, over Basic **or** Bearer | The ops console: one server-rendered HTML page over the same queries the five `/admin/*` routes use. |
| `GET /healthz` | none | Liveness. Touches no dependency. |
| `GET /readyz` | none | Readiness: database reachable, config loaded. Returns counts only, never provider names or base URLs. |

The two probes are unauthenticated deliberately — a health probe that needs a credential reports "unhealthy"
the moment the credential rotates. They are also split deliberately: conflating liveness with
readiness means an orchestrator restarts a healthy gateway because Postgres blinked, dropping
in-flight streams and making the outage worse.

Every response carries `x-request-id`, generated per request. An inbound `x-request-id` is
deliberately **not** honoured: the id is the primary key of the request-log row, so accepting the
caller's value would let it collide with an existing row — turning a served request into a 500 — or
write its row under an id another tenant chose. The id is assigned in middleware, before any
dependency runs, so a rejection that never reaches a route still has one to log under.

### The semantic cache

Every non-streaming answer a cache-enabled tenant receives is stored, and every request from one is
looked up first. `x-prism-cache` says `hit` or `miss` on every response, and a hit costs zero tokens
and zero dollars — the tokens were purchased once, on the request that created the entry.

A lookup runs in this order, and each step can only reject:

```
scope ─▶ exact hash ─▶ embed ─▶ threshold ─▶ literal guard ─▶ polarity guard
```

- **Scope** is part of the key, not a filter on it: `(tenant, tier, conversation prefix, sampling
  parameters)`. A cross-tenant hit is not expressible, which is how
  `docs/PRISM_PROBLEM_STATEMENT.md:66` is enforced.
- **The exact path** is SHA-256 of the whitespace- and case-normalised prompt, indexed. A repeated
  identical prompt costs one index lookup and no embedding at all.
- **The threshold** is per tenant (`cache_similarity_threshold` in the seed file: `0.92` for `search`,
  `0.85` for the free tier). Cosine against the newest 500 entries in the same scope.
- **Two guards** then reject a candidate whose numbers, IDs, currency amounts, month names or quoted
  strings differ as a multiset, or whose negation profile differs. Both are rejection-only, so
  neither can cause a hit; the pairs that justify them are measured in
  [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md).

On the write side, a **time-sensitive prompt is served normally and never stored** — the failure it
guards against is the same question asked twice, which a threshold cannot help with. Error bodies are
never stored. Streamed answers are never stored either, though a stored answer *can* be replayed as a
stream. Caching is opt-in per tenant, and two of the four seeded keys have it off.

Eviction is TTL only, purged opportunistically after a write. With `PRISM_CACHE_TTL_SECONDS` unset
entries never expire, which is what makes a paraphrase demo reproducible.

### The admin plane

`/admin/*` is read-only and guarded by `PRISM_ADMIN_TOKEN` as a bearer token, checked on the
router rather than per route — a per-route dependency is one forgotten parameter away from
publishing every tenant's spend, and a test asserts all five paths reject an unauthenticated call.
A valid *virtual* key is rejected too: a tenant credential must not read other tenants' figures.
Admin requests write no `request_log` row, so reading the usage API cannot change what it reports.

Every endpoint takes an optional `key=` selector, and `/admin/usage` and `/admin/logs` take
`from=`/`to=` (UTC days, `to` inclusive, defaulting to the current budget period so the number is
comparable with the monthly budget). `key=` accepts a **team name, a key prefix, or a whole virtual
key** — matched by hash, never stored, and only ever echoed back as the prefix. An ambiguous
selector is a 400 rather than a silently chosen row. That and three other documented deviations
from the contract's sketch — `virtual_key` rendered as `key`, per-request cost returned as the
header's exact string, and cache hit rate measured against lookups rather than all requests — are
argued in [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) under "Admin plane — as built".

The queries live in [prism/usage.py](prism/usage.py), separate from the routes, so the ops console
reuses them without going through HTTP.

### The ops console

`GET /console` is one server-rendered page: totals across the top, then usage by key, keys with
their budgets and limits, per-key cache statistics, provider health, and the newest 25 requests.

Three choices worth naming.

**No JavaScript, no external asset.** A console that fetched `/admin/usage` from the browser would
need the admin token in client-side code — putting it in the page source, in `fetch` history and in
the devtools network log, for a page whose whole purpose is to display sensitive figures. Rendering
server-side keeps the credential in the request that asked for it. The CSS is inline for a second
reason: a CDN stylesheet makes an offline machine render unstyled and a firewalled one hang. A test
asserts the page contains no `<script` and no absolute URL.

**Basic *or* Bearer, same token.** The address bar cannot send `Authorization: Bearer`, so an
unauthenticated request answers 401 with `WWW-Authenticate: Basic` and the browser prompts for the
admin token; any username is accepted, because there is one shared token and no per-operator
identity to check. Bearer still works so scripts need no special case. `?token=` was rejected
deliberately — it would put the secret in shell history, in the referrer of any outbound link, and
in every access log in between. The JSON plane deliberately sends *no* challenge, so `curl` and XHR
callers get a plain 401 instead of a native password box they cannot fill.

**It calls [prism/usage.py](prism/usage.py) directly, not its own HTTP client.** Same process, so
going over HTTP would add a second home for the admin token, turn one page load into six
authenticated requests, and let the console's numbers disagree with the API's for timing reasons
alone. The headline cache hit rate is totalled by one shared `combine_cache_savings`, and a test
asserts the page and `/admin/cache/stats` report the same figure.

Timestamps are labelled UTC and carry a date. `created_at` is stored timezone-aware, so a bare
`09:16:53` is *correct* and still misleading: an operator reading it from another zone concludes the
gateway's clock is broken, and a row from last week looks like it just happened.

### Streaming

`"stream": true` returns `text/event-stream`, one `data:` line per upstream chunk, forwarded as it
arrives and terminated with `data: [DONE]`. Chunks are relayed as the **raw JSON text** the provider
sent, never parsed and re-serialised, so fields Prism has never heard of reach the client intact.

`x-prism-provider`, `x-prism-cache` and `x-prism-fallback` are flushed with the headers, before the
first token. `x-prism-cost-usd` is **not** sent: the cost is unknown until the final chunk, and a
header claiming `0` would be a number that never reconciles. The final cost still reaches the
`request_log` row, which is written when the stream ends — see
[docs/DAY3_DESIGN_LOG.md](docs/DAY3_DESIGN_LOG.md).

If an upstream dies **mid-stream**, the client stream is terminated with an OpenAI-shaped error
event followed by `[DONE]`. Prism never restarts on another provider and splices the two answers
together: `docs/IMPLEMENTATION_GUIDE.md:172` rules that out, and the partial answer the client
already holds cannot be un-sent. Failover *before* the first token is a different case and does
happen — see below.

### Retries and failover

An alias resolves to an ordered chain (primary, then fallbacks). Each target gets up to
`retry.max_attempts` attempts with exponential backoff and full jitter; a target that cannot serve
the request hands over to the next one in the chain, and `x-prism-fallback` goes `true`.

What is worth retrying is decided by the response, not by the retry loop. Two independent
questions, answered where the response was seen:

| Upstream | Retry same provider | Try next provider | Why |
|---|---|---|---|
| 5xx, 408, 429, timeout, transport error | yes | yes | Transient. Says nothing about whether the request was valid. |
| 400 | no | no | The request is malformed; every provider will agree. Failing it over spends money and latency to be told the same thing. |
| 401, 403, 404 | no | yes | *Our* stored credential or route for that provider is wrong. Retrying cannot fix it; a provider with a working key is exactly right. |

Backoff is applied only between retries of the *same* target, never before moving to the next one —
waiting does not make a different provider healthier, and the delay would be spent out of the
caller's latency budget. `request_log.retries` counts every extra upstream call the request cost,
across the whole chain, so `retries = 0` means "served first try".

### Enforcement order and error contract

Checks run cheapest-and-most-certain first:

| # | Check | Status | `type` |
|---|---|---|---|
| 1 | virtual key present and known | 401 | `authentication_error` |
| 2 | key is `active` | 401 | `authentication_error` |
| 3 | model or alias exists | 404 | `not_found_error` |
| 4 | model on the key's allowlist | 403 | `model_not_allowed` |
| 5 | rate limit | 429 | `rate_limit_exceeded` |
| 6 | budget | **402** | `budget_exceeded` |

Bodies are OpenAI-shaped (`{"error": {"message", "type", "code", "param"}}`), and each case is
distinguishable from the body alone, as `docs/API_CONTRACT.md:115` requires.

Three of these are documented choices rather than transcriptions of the suggested mapping:

- **Budget exhaustion returns 402, not 429.** The contract permits either.
  `scripts/load_test.py:106` classifies *every* 429 as "rate limited", so returning 429 here would
  make a budget-exhausted key show up as a rate-limiter result in the one measurement that test
  exists to produce. 402 also tells a client something true that 429 does not: retrying later cannot
  help. `Retry-After` is sent on 429 and deliberately omitted on 402.
- **Unknown model is checked before the allowlist.** Answering 403 for a model that does not exist
  would confirm its existence, leaking the model registry to any caller holding any valid key.
- **A disabled key is byte-identical to an unknown key**, as are a missing header, a malformed
  header, and a wrong key. Distinguishing them would tell an attacker which part of their guess was
  structurally correct.

## Verification: smoke, load, and the routing eval

```bash
python scripts/smoke_test.py --url http://localhost:8080 --key prism-sk-search-1a2b3c --model fast
python scripts/load_test.py  --url http://localhost:8080 --key prism-sk-free-7g8h9i --model fast \
  --requests 30 --concurrency 10 --rpm-limit 10
```

Both require the gateway running on `:8080`, with two mock providers behind it.

Current results, measured against a live stack and written up in full with commands and raw output in
[docs/DAY6_VERIFICATION.md](docs/DAY6_VERIFICATION.md):

| Check | Result |
|---|---|
| `scripts/smoke_test.py` | **17 passed, 1 warning, 0 failed.** The warning is the documented 0.92-threshold trade: one of the two paraphrase pairs sits below `search`'s threshold and is an honest miss. The script requires *at least one* pair to hit; one does. |
| `scripts/load_test.py`, 40 requests at concurrency 20 against an rpm-10 key | **No over-admission: exactly 10 accepted ≤ limit 10.** |
| `scripts/load_test.py`, 100 requests at concurrency 30 against an rpm-60 key | **No over-admission: exactly 60 accepted ≤ limit 60.** |
| Accounting | Snapshotted before and after the second burst, so it is a delta rather than a plausible-looking total: all seven `/admin/usage` fields move by exactly the client-side figures (100 requests, 60 served, 40 rejected, 720 + 1,587 tokens, $0.00106020). `spent_usd` and `logged_cost_usd` — two different queries over two different tables — agree to the tenth decimal for every key. |
| Provider failure drills | Alpha `down` → beta serves with `x-prism-fallback: true`; restore → traffic returns to the primary unaided; `latency_ms: 3000` → served slowly without breaching the 5000 ms p95 threshold; `fail_rate: 0.3` over eight requests → 8/8 succeeded, one via fallback. |
| Timeout drill, alpha stalled **past** the 30 s read timeout | Found a real defect and fixed it: the wait was **90.5 s** because a read timeout was retried twice on the same stalled provider before failover. Now **30.0 s** non-streaming and 30.4 s streaming, and a both-providers-stalled chain gives up cleanly in 60.1 s instead of 180 s. |
| Added latency, non-streaming | **~19 ms** over calling the mock directly (auth, allowlist, rate limit, budget, routing, the HTTP hop, the log write). Difficulty classification adds **~16 ms** observed, of which 3.8 ms is the embedding itself and the rest is a thread hop plus this box's ~15.6 ms scheduler tick. A cache **miss** adds **~49–64 ms** depending on how many entries the tenant has stored, because similarity is a linear scan (~0.06 ms per entry, capped at the newest 500, so ~30 ms at the ceiling); an exact **hit** is 22 ms end to end. An earlier draft of this table said the cache cost ~8 ms; that measurement used a cache-*enabled* key as its control and was wrong. |

One finding from that measurement is worth repeating here, because it will bite anyone running this
on Windows: pointing the provider `base_url` at `localhost` rather than `127.0.0.1` cost **~265 ms
per upstream call**. `localhost` resolves dual-stack, the mock closes the connection after each
response so the name is re-resolved every time, and a *pooled* `httpx` client pays it too
(127.0.0.1: 0.0 ms median; localhost: 265.5 ms). Added latency measured ~305 ms before the change
and tens of milliseconds after. Nothing in `prism/` changed. `gateway_config.json` now uses
`127.0.0.1` and says why in a `_comment`.

Routing accuracy is graded offline — no gateway, no database, no network:

```bash
python scripts/routing_eval.py                        # semantic, held-out set, human table
python scripts/routing_eval.py --classifier length    # the baseline it has to beat
python scripts/routing_eval.py --json                 # machine-readable, with per-case reasons
```

**19/20 = 95%** semantic against **8/20 = 40%** for the length baseline, on
[data/routing_eval.jsonl](data/routing_eval.jsonl), which is scored as held out and never tuned
against. No code under `prism/` reads `expected_tier` at all — the answer key reaches the scorer and
never the classifier. Verify the artifact all of this depends on:

```bash
python scripts/fetch_model.py       # SHA256-verify the vendored model, offline
```

Recorded output in full, reconciliation against the usage API, the failure drills and the latency
breakdown are in [docs/DAY6_VERIFICATION.md](docs/DAY6_VERIFICATION.md), which is written against
the checklist in [docs/EVALUATION_GUIDE.md](docs/EVALUATION_GUIDE.md).

## Architecture and design decisions

### Storage model

Seven entities in `docs/DATA_MODEL.md`, four tables. The collapsing is the decision:

| Entity | Where it lives | Why |
|---|---|---|
| Virtual Key | `tenants` | Mutable state that must survive a restart, and rows point at it. |
| Provider, Model Alias, Model Price | config files, loaded at startup | No per-request mutable state, nothing references them by key. A table would mean a migration to change a price and a join to read one. |
| Request Log Entry | `request_log` | One row per request, **including rejections** (`docs/DATA_MODEL.md:57`). |
| Usage Record | *derived* — a query over `request_log` | Writing an aggregate row alongside every log row is a dual write, and dual writes drift. The moment one succeeds and the other fails, the usage API and the log disagree with no way to say which is right — and that reconciliation is exactly what `docs/EVALUATION_GUIDE.md` grades. |
| Cache Entry | `cache_entries` | Scoped by `tenant_id` as part of the entry's identity, so isolation lives in the schema rather than in a `WHERE` clause someone can forget. |
| — | `budget_periods` | The one deliberate denormalisation. Budget reads must be cheap (`docs/DATA_MODEL.md:86`) and scanning the log per request is not, so this is a per-tenant per-month counter incremented atomically and reconcilable against `request_log` at any time. A cache with a correctness proof, not a second source of truth. |

Two schema choices worth naming. **Money is `NUMERIC(18,10)` and `Decimal` in Python, never
`float`** — binary floats do not sum associatively, so the same set of costs totals differently
depending on the order requests happened to finish, and the load test compares a client-side total
against a server-side one. The scale is set by the `budget-demo` tenant's $0.00001 budget: anything
coarser rounds it to zero and the budget demo stops demonstrating anything. **Embeddings are
`double precision[]`, not `pgvector`** — `pgvector` is an extension, and depending on one narrows
the schema to deployments that have it installed, for a corpus that is per-tenant and small. So
similarity is a per-tenant scan in Python, and the ceiling that buys is stated in Known limitations.

`tests/test_schema.py` proves the atomicity claim rather than asserting it: forty concurrent
`UPDATE ... SET spent = spent + ? RETURNING spent` all land, and the neighbouring test shows the
read-then-write version losing updates deterministically. That pair is why this project uses
Postgres and not SQLite.

Covered in full, with the reasoning behind every decision and the answers to the obvious
objections, in the per-slice design logs:

- [docs/DAY1_DESIGN_LOG.md](docs/DAY1_DESIGN_LOG.md) — the spine: config, schema, auth, the error
  contract, and the concurrency proof that chose Postgres.
- [docs/DAY2_DESIGN_LOG.md](docs/DAY2_DESIGN_LOG.md) — the data plane: the provider boundary and its
  error taxonomy, model resolution, the rate limiter, cost metering, the atomic budget charge, and
  how a rejection that never reaches a route still gets a log row.
- [docs/DAY3_DESIGN_LOG.md](docs/DAY3_DESIGN_LOG.md) — resilience and streaming: the retry/failover
  loop, why backoff is jittered, the open/iterate split that makes the mid-stream policy structural,
  and how a response that has already been sent still gets metered.
- [docs/DAY4_DESIGN_LOG.md](docs/DAY4_DESIGN_LOG.md) — smart routing: ask extraction, the kNN vote
  over self-authored exemplars, how the never-tune rule was kept operational, and the measured
  95% held out against the length baseline's 40%.
- [docs/DAY5_DESIGN_LOG.md](docs/DAY5_DESIGN_LOG.md) — the semantic cache and the admin plane: the
  scope key and the tenant isolation boundary, the two rejection-only guards and the measured pairs
  that justify them, why a volatile prompt is refused at write time, and how usage stays a query
  rather than a stored aggregate.
- [docs/DAY6_VERIFICATION.md](docs/DAY6_VERIFICATION.md) — the verification report: every provided
  script run against a live stack with its raw output, reconciliation as a before/after delta, the
  per-case routing table with the one miss explained, the cosine similarities behind the cache WARN,
  the added-latency breakdown that found a 265 ms cost in one word of config, and the timeout drill
  that found a 90-second wait and the one-line classification change that made it 30.

## Known limitations

Maintained continuously as work proceeds, not written at the end.

- **A stream that dies mid-response cannot be salvaged.** The client keeps the partial answer and
  receives an error event, then `[DONE]`. That is the acceptable behaviour
  `docs/IMPLEMENTATION_GUIDE.md:172` names, and the alternative it forbids — restarting on another
  provider and splicing — is prevented structurally: such a failure is raised with both
  `retry_same` and `try_next` false, so the dispatcher cannot fail it over even if a future caller
  asked it to. `[DONE]` is still sent after the error, because a client that never gets a terminator
  waits out its own read timeout before showing the user anything.
- **A stream that dies before its final chunk is billed at zero.** Usage arrives last
  (`scripts/mock_provider.py:203`), so a stream cut short has none to read, and the request is logged
  with zero tokens and zero cost. Same trade as the `usage`-less provider below, and the same reason:
  a number no provider agreed to is worse than a visible zero.
- **A client that disconnects mid-stream is still charged.** Those tokens were generated and paid for
  upstream, so the charge and the log row are written from the relay generator's `finally` whether the
  client stayed or not. The alternative — cancelling the charge on disconnect — would make hanging up
  early a way to get free inference.
- **There is no circuit breaker; the `degradation` config is reported but not enforced.** A provider
  that is down costs every request its full retry budget before failover — three attempts and
  ~600 ms of backoff, per request, indefinitely. `data/gateway_config.sample.json` supplies
  error-rate, window and p95 thresholds for exactly this. `GET /admin/providers/health` now reads
  them and publishes a `healthy` / `degraded` verdict, but *nothing routes on that verdict* — a
  provider reported `degraded` is still tried first — which is why the response carries a top-level
  `enforced: false`. Honouring the thresholds (trip the primary, skip straight to the fallback,
  probe occasionally) is the stretch item that makes a sustained outage cheap. The bound today is
  `max_attempts × chain length` upstream calls per request — except for timeouts, which are capped
  at one call per provider for the reason in the next bullet.
- **There is no total deadline for a request; the bound is chain depth × the read timeout.** A
  *read* timeout is no longer retried against the same provider (`prism/providers/http.py:_timeout_retry_same`),
  because a read timeout has by definition already spent the whole read budget and retrying it
  spends the budget again. Verification measured that directly: with alpha stalled past the 30 s
  timeout, the client waited **90.5 s** before the fix and **30.0 s** after it, and with *both*
  providers stalled the worst case went from 180 s to 60.1 s. What is still missing is a deadline on
  the request as a whole, so a deeper chain is proportionally slower — five stalled providers would
  be 150 s, and no `x-prism-*` header or config value caps that. The clean answer is a per-request
  budget that shrinks as attempts consume it and is passed down as each call's timeout; it is not
  built, because it changes the signature of every provider call and the drill above showed the
  retry multiplier was where the damage actually was.
- **An upstream `Retry-After` is ignored.** A 429 from a provider is backed off on Prism's own
  schedule rather than the provider's stated one, so a gateway retry can arrive before the provider
  is ready and be refused again. Reading the header and using it as a floor is a small change and is
  not made; the reason it is safe to defer is that the mock providers do not send one, so it would be
  untested code on the retry path.
- **An upstream 400 becomes a 502, not a 400.** `docs/API_CONTRACT.md:113` maps "all providers
  failed" to 502, and a request no provider will accept is that case. The cost is that a caller whose
  request really is malformed in a way Prism's own validation does not catch learns only that the
  upstream would not serve it. Passing the status through would be more useful and is deliberately
  not done, because the accompanying *message* is the upstream's and may quote the request or the
  credential we sent (`docs/DATA_MODEL.md:44`) — so it would be a status with no explanation.
- **The router's exemplar set is 52 prompts I wrote myself.** `auto` classifies by kNN against them,
  which means routing quality is bounded by how well 52 hand-authored prompts cover the space of
  real traffic. It scores 95% on the held-out eval, but that eval is 20 cases from the same pack the
  exemplars were reasoned about; a genuinely different traffic mix could do materially worse and
  nothing here would detect it. The honest read is "beats length by a wide margin on the only
  held-out set available", not "95% accurate in general". A production answer collects real routed
  prompts, has them labelled, and retrains — none of which is built.
- **One held-out case routes to the expensive tier and shouldn't.** `route_009`, "Is 91 divisible by
  7?", classifies `smart`: its nearest neighbours are `proof`, `proof`, `arithmetic-check`, because
  number-theory wording is shared between checking one instance and proving a general claim. Left
  unfixed on purpose — a targeted exemplar would be fitting the answer key, which
  `docs/DESIGN_NOTES.md` forbids. The direction is the safe one: it overspends rather than
  under-answers.
- **Ask extraction can be fooled by payload with no punctuation.** Long prompts are reduced to their
  ask before classification, by dropping quoted spans and keeping only sentences that carry a
  question or an instruction cue. Payload with no sentence boundaries at all — a space-joined run of
  tokens with a question tacked on the end — is a single "sentence" containing the ask, so it is
  kept whole and merely clipped to 60 words. It degrades rather than breaks (the clip keeps head and
  tail, and the question is at the tail), and real pasted payload is newline-separated and splits
  correctly. Where nothing recognisable as an ask survives, the *whole* prompt is classified and the
  reason says `dropped=no-cue`: dropping a real instruction is the expensive failure, so the
  fallback errs toward classifying too much.
- **The length baseline scores 40% here, not the ~60% the guide predicts.**
  `docs/EVALUATION_GUIDE.md:82` describes a length-only heuristic as ~60% by design; this build's
  threshold of 24 words measures 8/20. The threshold was chosen by reading prompts rather than by
  fitting the eval, and most of the eval's hard cases are short. Reported as measured — tuning the
  baseline up would have meant fitting the answer key to make a comparison look fairer.
- **`auto` pays an embedding on every request that reaches routing.** The classification runs after
  every rejection check (auth, allowlist, rate limit, budget), so refused requests cost nothing, but
  a served `auto` request now has ONNX inference on its critical path. There is no cache of
  prompt → tier, and no configuration to route by length instead per tenant; `--classifier length`
  exists only in `scripts/routing_eval.py`.
- **Rate-limit state is in memory, so a restart forgives outstanding usage.** A tenant that has
  used nine of ten requests per minute gets a fresh ten if the process restarts, and a second
  process would double every tenant's effective limit. `docs/DATA_MODEL.md:118` permits this
  explicitly; what it requires is that the counter be race-safe within the process, which is what
  `tests/test_ratelimit.py::test_no_over_admission_under_thread_contention` pins. Shared state
  (Redis) is the multi-process answer and is not built.
- **A provider that omits `usage` is billed at zero.** Cost is computed from the token counts the
  provider reports; if that block is missing or malformed, `read_usage` returns `(0, 0)` and the
  request is served, logged and charged nothing. The alternative — counting tokens ourselves —
  would put a number into the accounting that no provider ever agreed to, which is worse than a
  visible zero when the point of the metering is that it reconciles.
- **Budgets can overshoot by one burst.** Admission asks whether *any* budget remains, not whether
  there is enough for this request, because the cost is unknown until the provider reports usage
  and for a stream until it ends. So a concurrent burst can all pass on the last cent. The bound is
  one window's worth of requests — at most `requests_per_minute × cost_per_request` — and after
  those charges land, admission refuses. `docs/DATA_MODEL.md:91` permits this for streams; this
  build applies the same rule to both paths so there is one policy rather than two.
- **A caller cannot choose its own request id.** An inbound `x-request-id` is ignored and a fresh
  one is generated. The id is the `request_log` primary key, so honouring the header would let a
  caller either collide with an existing row — turning a served request into a 500 — or write its
  row under an id another tenant chose. The generated id is still echoed on every response,
  including on rejections, so it remains usable for support.
- **No migrations.** `scripts/init_db.py` calls `create_all`. Alembic earns its keep when a schema
  must change without losing data that already exists, and there is none yet; a migration chain
  written before the first schema is stable is a chain you end up squashing. Deferred to the buffer
  days, not forgotten — `alembic` is already pinned in `requirements.txt`.
- **The admin plane is one shared bearer token.** `docs/API_CONTRACT.md:33` permits "any simple
  documented mechanism", and this is the simple one: no rotation, no per-operator identity, no audit
  of *which* admin acted. It is compared with `secrets.compare_digest` because unlike a virtual key
  it may be operator-chosen and therefore low-entropy.
- **The semantic cache has no ANN index.** Lookup scans one tenant's entries and computes cosine in
  Python, which is O(entries-per-tenant) per miss. Embeddings are stored as a plain float array
  rather than a `pgvector` column: `pgvector` is an extension, and depending on one narrows the
  schema to deployments that have it installed, for a corpus that is per-tenant and small. That
  trade inverts as soon as a single tenant's corpus outgrows a linear scan — the honest ceiling is
  low thousands of entries per tenant, and past that `pgvector` with an HNSW index is the answer. Now
  measured rather than asserted: **~0.06 ms per stored entry**, so the 500-candidate cap holds the
  scan at ~30 ms and the ceiling above is about *recall*, not speed — see the bullet below and
  [docs/DAY6_VERIFICATION.md](docs/DAY6_VERIFICATION.md#6-gateway-added-latency).
- **Provider ownership of a model is inferred from its name.** The provided config never states
  which provider serves `alpha-small`; the pack encodes it as `{provider}-{size}` and
  `scripts/validate_pack.py:64` relies on that too. Prism follows the data it was given, but in a
  single function (`GatewayConfig.provider_for_model`) and without naming a provider anywhere in
  source, so an explicit `models: [...]` field per provider would change one function.
- **`scripts/fetch_model.py --fetch` has never been run.** The model was vendored out of band and
  re-downloading it was out of scope, so the *download* half of that script is unexercised code and
  reviewers should expect first use to be its first real run. The *verify* half is exercised and
  passes: it confirms all five files of the pinned revision against SHA256 digests computed from the
  vendored copy, which is what makes the measured cosines in `docs/DESIGN_NOTES.md` attributable to
  specific bytes. Its failure paths (absent revision, changed file) were tested too; what has not
  been tested is the network transfer.
- **Cache paraphrase pair B is a permanent WARN at the demoed threshold.** `req_cache_b1`/`b2`
  measure 0.8693, below the `search` key's seeded 0.92, so that pair does not hit. This is a
  deliberate trade: no single threshold satisfies every fixture, and the alternative (0.85) clears
  the must-miss pair by only 0.0013. `scripts/smoke_test.py` fails only if *neither* pair hits, and
  pair A hits at 0.9825. Full analysis in [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md).
- **Streamed answers are never cached, though cached answers can be streamed.** Replaying a stored
  completion as SSE loses nothing, but going the other way would mean assembling a completion body
  out of deltas — and `system_fingerprint`, `logprobs` and any provider extension are simply not in
  the delta stream, so a later non-streaming caller would get the gateway's reconstruction instead
  of a provider's response. The cost is real: a tenant whose traffic is entirely streaming gets no
  cache entries at all.
- **The cache has no size cap and no background sweeper.** Expiry is by TTL only, checked on read
  and purged opportunistically after a successful write. `PRISM_CACHE_TTL_SECONDS` is unset by
  default — which is what makes the paraphrase demo reproducible, since an entry written during
  setup is still there when the camera is on — and with no TTL, entries never expire and the table
  grows without bound. A deployment that cares about staleness or size sets the variable; a real
  answer is a TTL plus an LRU cap plus a sweeper.
- **A lookup only scans the newest 500 entries for a key and scope.** Past that, a valid entry can
  fall outside the candidate window and be missed while still occupying storage, so the hit rate
  degrades quietly rather than the lookup getting slower. It is the same ceiling as the no-ANN-index
  entry above, seen from the other side.
- **The volatility guard is lexical, so it cannot catch a time-sensitive prompt with no time words.**
  Prompts containing "today", "right now", "latest" and similar are answered but never stored. "What
  is the price of X" has no such word, is stored, and can be replayed indefinitely. Bare "now" is
  deliberately *not* a trigger — it appears in ordinary phrasing like "now explain the trade-off" —
  so "right now" and "just now" are matched as phrases instead.
- **The cache stores prompt text and response bodies, which the request log deliberately does not.**
  `docs/DATA_MODEL.md:78` asks for body storage to be a documented decision: it was declined for
  `request_log` and accepted for `cache_entries`, because a cache that does not keep the response is
  not a cache. What limits the exposure is that caching is opt-in per tenant — two of the four seeded
  keys have it off — and that TTL is the only retention control. There is no per-entry redaction and
  no "forget this tenant's cache" endpoint.
- **`/admin/usage` reports fewer tokens than a client counts, and that is correct.** A cache hit is
  charged zero tokens and zero dollars, so the totals reconcile against a provider invoice — but a
  replayed hit carries the original response's `usage` block in its body, so a client summing what it
  received gets a larger number. The difference is exactly `tokens_saved` from
  `/admin/cache/stats`. `scripts/load_test.py:123` prints the client-side figures for this
  comparison, so it is a discrepancy someone will meet.
- **Per-provider error counts under-report failures that were successfully failed over.** A request
  whose primary died and whose fallback succeeded is logged against the provider that *succeeded*,
  so the dead provider's failure shows up in `fallbacks` and `retries`, not in `errors`. Getting this
  right needs a log row per attempt rather than per request. Until then, `fallbacks` climbing while
  `errors` stays flat is the signal that something upstream in a chain is sick.
- **Key management is read-only.** `GET /admin/keys` reports each key's policy, its spend this month
  from both the budget counter and the log, and whether the two reconcile. `POST /admin/keys`, which
  `docs/PRISM_PROBLEM_STATEMENT.md:186` lists as good to have, is not built — tenants come from
  `data/seed_keys.json` via `scripts/init_db.py`.
- **`key=` on the admin API is a selector, not the secret the contract's example shows.** It accepts
  a team name, a key prefix, or the full virtual key resolved by hash; only the prefix is ever echoed
  back. `docs/API_CONTRACT.md:119` shows a whole virtual key in the response, which would mean
  putting a live credential in a query string — where it lands in access logs, shell history and the
  console's own URL. `docs/API_CONTRACT.md:5` permits documented changes to admin shapes; this is
  one, along with `key=` being optional and `served`/`rejected`/`failed` partitioning `requests`.
- **The admin plane has no pagination and no read-side protection.** `/admin/logs` caps at 500 rows
  and offers no cursor, so "everything last Tuesday" is not a question it can answer. No admin
  endpoint is cached or rate limited either: a console polling `/admin/usage` every second runs a
  full aggregate over `request_log` every second, against the same database the data plane writes to.
  `/console` runs six such aggregates per page load and is not exempt from any of this — what keeps
  it cheap in practice is that it does not poll.
- **The console shows the newest 25 requests and nothing else.** No pagination, no filtering, no
  search, and one consequence worth stating because it shows up immediately after a load test: a
  burst of 100 rate-limited requests fills the whole table with 429s, so the successful traffic that
  preceded it is pushed off the page. `/admin/logs?limit=` is the answer for anyone who needs more,
  and the console does not link to it.
- **The console does not refresh itself.** It is a static render of the moment it was requested; a
  stale tab looks exactly like a live one. Polling was left out rather than forgotten — a
  `<meta refresh>` would re-run six aggregates on a timer whether anyone was looking or not, and the
  JavaScript alternative would need the admin token in client-side code, which is the thing the whole
  page is designed to avoid.
- **Basic auth on the console sends the admin token base64-encoded, which is not encryption.** Over
  plain HTTP the token is recoverable by anyone on the path — the same is true of the Bearer token on
  `/admin/*`, and neither is worse than the other, but a browser prompt invites use from further away
  than `curl` on localhost does. Any deployment beyond a loopback demo needs TLS in front of this,
  and there is none here.
- **A tenant with caching switched off still counts toward the overall hit rate.** `lookups` means "a
  request that reached the cache", and a disabled tenant's requests do reach it — the cache simply
  declines to look. Its rows therefore sit in the denominator of the headline figure, so with two of
  four seeded keys opted out the overall hit rate understates how well the cache works for the keys
  actually using it. The per-key breakdown carries `cache_enabled` so nothing is hidden, and the
  clean fix — leaving `request_log.cache` null when caching is off — was declined because it would
  break the invariant that the `x-prism-*` headers and the log row state the same facts, for a
  reporting nuance.
- **Embedding is CPU-bound and single-process.** ONNX inference runs in `asyncio.to_thread`, so
  throughput under concurrency is bounded by the default thread-pool executor — for cache lookups
  and, since `auto` classifies semantically, for routed requests too. One embedder instance is
  shared process-wide and warmed at startup; there is no batching across concurrent requests.
- **Development Postgres runs from unpacked binaries, not a managed service.** Single instance, no
  replication, trust-free but locally-scoped credentials. `docker-compose.yml` is the reproducible
  path for reviewers.

## License

[MIT](LICENSE).
