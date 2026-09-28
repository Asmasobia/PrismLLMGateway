# Prism Capstone Pack

This repository contains a self-contained capstone package for building **Prism**, an AI-first LLM gateway with smart difficulty-based routing, failover, streaming, per-tenant metering, budgets, and a semantic cache. The gateway makes two AI decisions on every request: how hard is this prompt (which model tier deserves it), and have we answered it before (semantic cache).

The capstone is language agnostic. You may implement it in Node.js, Java, Python, Go, Ruby, or any stack you are comfortable with, as long as you satisfy the API, correctness, and verification requirements.

## Contents

- `PRISM_PROBLEM_STATEMENT.md` - capstone problem statement.
- `docs/IMPLEMENTATION_GUIDE.md` - suggested build order, demo scenarios, and FAQ.
- `docs/API_CONTRACT.md` - language-agnostic API contract, header contract, and error shapes.
- `docs/DATA_MODEL.md` - suggested entities, relationships, and storage expectations.
- `docs/EVALUATION_GUIDE.md` - how your gateway is verified and what to include in the verification report.
- `data/model_pricing.json` - price table for cost accounting.
- `data/seed_keys.json` - four seed tenants with budgets, limits, allowlists, and cache settings (including a tiny-budget key for the budget-exhaustion demo).
- `data/gateway_config.sample.json` - sample provider registry, model aliases, and fallback chains.
- `data/sample_requests.jsonl` - annotated request bodies, including cache-test prompt pairs and negative cases.
- `data/routing_eval.jsonl` - 20 labeled prompts for grading the `auto` router, with short-but-hard and long-but-trivial traps.
- `scripts/mock_provider.py` - zero-dependency OpenAI-compatible mock provider with live failure injection.
- `scripts/smoke_test.py` - checks your gateway against the required API and header contract.
- `scripts/load_test.py` - checks rate-limit over-admission and accounting accuracy under concurrency.
- `scripts/validate_pack.py` - validates that the package data is parseable and internally consistent.

## Optional Local Utilities

All scripts use only Python standard library modules (Python 3.9+). From this folder:

```bash
python3 scripts/validate_pack.py
```

Start two mock providers (two terminals):

```bash
python3 scripts/mock_provider.py --port 9001 --name alpha
python3 scripts/mock_provider.py --port 9002 --name beta
```

You now have 2 "providers" serving `alpha-small`, `alpha-large`, `beta-small`, `beta-large` — free, offline, and OpenAI-compatible. Try one:

```bash
curl -s http://localhost:9001/v1/chat/completions \
  -H "Authorization: Bearer anything" -H "Content-Type: application/json" \
  -d '{"model": "alpha-small", "messages": [{"role": "user", "content": "hello"}]}'
```

Inject failures live — no restarts needed:

```bash
# take alpha down; your gateway should fail over to beta
curl -s -X POST http://localhost:9001/admin/config -d '{"mode": "down"}'

# make alpha slow (3s) - do your timeouts hold?
curl -s -X POST http://localhost:9001/admin/config -d '{"mode": "ok", "latency_ms": 3000}'

# make alpha flaky - do your retries handle a 30% error rate?
curl -s -X POST http://localhost:9001/admin/config -d '{"latency_ms": 0, "fail_rate": 0.3}'

# back to healthy
curl -s -X POST http://localhost:9001/admin/config -d '{"mode": "ok", "fail_rate": 0}'
```

Verify your gateway once it is running:

```bash
python3 scripts/smoke_test.py --url http://localhost:8080 --key prism-sk-search-1a2b3c --model fast
python3 scripts/load_test.py  --url http://localhost:8080 --key prism-sk-free-7g8h9i --model fast --requests 30 --concurrency 10 --rpm-limit 10
```

The mock providers are the intended upstreams for development, demos, and load testing — they cost nothing and make failover deterministic. You may swap one for a real provider (or Ollama) if you want.

## Language-Agnostic Expectations

Your implementation should provide:

- An OpenAI-compatible `POST /v1/chat/completions` data plane matching `docs/API_CONTRACT.md`, including the `x-prism-*` header contract.
- Streaming pass-through without buffering.
- Virtual-key authentication with per-key allowlists, rate limits, and monthly budgets.
- Alias-based routing with retries and provider failover.
- An `auto` alias that classifies prompt difficulty and routes to a model tier, evaluated against `data/routing_eval.jsonl`.
- A per-tenant semantic response cache.
- Accurate cost metering, request logs, and a usage API.
- A lightweight ops console for usage, recent requests, and cache stats.
- A provider adapter that can be mocked in tests.

The console can be vibe-coded or AI-assisted. It does not need to be visually complex, but it should let you demonstrate usage by key, recent traffic, and cache hit rates.

## Recommended Reading Order

1. Read `PRISM_PROBLEM_STATEMENT.md`.
2. Follow `docs/IMPLEMENTATION_GUIDE.md` for the build path.
3. Use `docs/API_CONTRACT.md` and `docs/DATA_MODEL.md` while designing your implementation.
4. Use `docs/EVALUATION_GUIDE.md` before running the verification scripts and writing your report.

## Correctness Notes

The pack is designed to expose the classic gateway failure modes: buffered "streaming", read-then-write rate limiters that over-admit under concurrency, cost computed from client-declared tokens, caches shared across tenants, and gateways that hang when a provider goes silent. The smoke test, load test, and live failure injection exist so you can prove your implementation does not have them.
