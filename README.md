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
- [API overview](#api-overview)
- [Verification: smoke and load tests](#verification-smoke-and-load-tests)
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

Then start Postgres and copy the environment template:

```bash
docker compose up -d     # Postgres 16 on port 5433
cp .env.example .env     # then edit PRISM_MODEL_CACHE and PRISM_ADMIN_TOKEN
```

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
`HF_HUB_OFFLINE=1`. `scripts/fetch_model.py` fetches it at a pinned revision with a SHA256 check.
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
| `PRISM_HOST` | no | `0.0.0.0` | Bind address. |
| `PRISM_PORT` | no | `8080` | Listen port. The provided test scripts default to `:8080`. |

## Seeding keys and configuring providers

Four demo tenants ship in [data/seed_keys.json](data/seed_keys.json) with budgets, rate limits,
model allowlists, and per-key cache settings. The `budget-demo` key carries a deliberately tiny
budget so budget exhaustion is demonstrable live. Provider registry, model aliases, and fallback
chains come from [data/gateway_config.sample.json](data/gateway_config.sample.json); pricing for
cost accounting from [data/model_pricing.json](data/model_pricing.json).

Seed procedure: _TBD._

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

Starting the gateway: _TBD._

## API overview

Data plane is `POST /v1/chat/completions`, OpenAI-compatible, streaming and non-streaming. Every
response carries the `x-prism-*` header contract. The normative spec is
[docs/API_CONTRACT.md](docs/API_CONTRACT.md); this section will summarise the implemented surface,
including the usage API and ops console routes. _TBD._

## Verification: smoke and load tests

```bash
python scripts/smoke_test.py --url http://localhost:8080 --key prism-sk-search-1a2b3c --model fast
python scripts/load_test.py  --url http://localhost:8080 --key prism-sk-free-7g8h9i --model fast \
  --requests 30 --concurrency 10 --rpm-limit 10
```

Both require the gateway running on `:8080`. Recorded output, reconciliation against the usage
API, routing-eval accuracy with per-case results, and measured added latency: _TBD_ (see
[docs/EVALUATION_GUIDE.md](docs/EVALUATION_GUIDE.md) for what the report must contain).

## Architecture and design decisions

_TBD._ To cover, at minimum:

- **Routing** — how prompt difficulty is classified for the `auto` alias, and why that beats a
  length-only baseline on [data/routing_eval.jsonl](data/routing_eval.jsonl).
- **Failover** — retry policy, timeouts, what counts as a retryable failure, and how streams
  terminate cleanly when an upstream dies mid-response.
- **Cache scoping** — the tenant isolation boundary, embedding choice, similarity threshold, and
  how multi-turn conversations key into the cache.
- **Budget accounting** — why cost is computed from measured rather than client-declared tokens,
  and how totals stay accurate under concurrency.

## Known limitations

Maintained continuously as work proceeds, not written at the end.

- **No gateway yet.** Beyond the provided scaffold, only the environment is set up; every
  functional section above is _TBD_.
- **`scripts/fetch_model.py` is unverified against a live host.** The model was vendored out of
  band, so the download path has never been exercised end to end. The script is written for
  reviewers, who should expect first use to be its first real run.
- **Cache paraphrase pair B is a permanent WARN at the demoed threshold.** `req_cache_b1`/`b2`
  measure 0.8693, below the `search` key's seeded 0.92, so that pair does not hit. This is a
  deliberate trade: no single threshold satisfies every fixture, and the alternative (0.85) clears
  the must-miss pair by only 0.0013. `scripts/smoke_test.py` fails only if *neither* pair hits, and
  pair A hits at 0.9825. Full analysis in [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md).
- **Embedding is CPU-bound and single-process.** ONNX inference runs in `asyncio.to_thread`, so
  cache-lookup throughput under concurrency is bounded by the default thread-pool executor.
- **Development Postgres runs from unpacked binaries, not a managed service.** Single instance, no
  replication, trust-free but locally-scoped credentials. `docker-compose.yml` is the reproducible
  path for reviewers.

## License

[MIT](LICENSE).
