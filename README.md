# Prism — LLM Gateway and Semantic Cache

One OpenAI-compatible API in front of multiple LLM providers. Prism makes two decisions on every
request — **how hard is this prompt** (which model tier deserves it) and **have we answered this
before** (semantic cache) — while enforcing per-tenant keys, rate limits, and cost budgets, and
streaming tokens through without buffering.

> `data/`, `scripts/`, and the provided documents in `docs/` (`PROVIDED_PACK.md`,
> `PRISM_PROBLEM_STATEMENT.md`, `API_CONTRACT.md`, `DATA_MODEL.md`, `EVALUATION_GUIDE.md`,
> `IMPLEMENTATION_GUIDE.md`) are the supplied project scaffold, imported unmodified in commit
> `84cf8cb`. Everything else is my own work.

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

Dependency install and database bootstrap: _TBD._

Postgres is required rather than SQLite: SQLite serialises writers, which would mask the
read-then-write rate-limiter race that `scripts/load_test.py` is designed to detect.

## Environment variables

_TBD_ — table of name, required/optional, default, and purpose.

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

- Nothing implemented yet beyond the provided scaffold; every functional section above is _TBD_.

## License

[MIT](LICENSE).
