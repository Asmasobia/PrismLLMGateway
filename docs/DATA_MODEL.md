# Prism Data Model

This is a language-agnostic data model. You may use SQL, NoSQL, document stores, or a mix (for example Redis for counters plus PostgreSQL for logs), as long as you preserve the core entities and the correctness guarantees.

## Source Data Files

- `data/model_pricing.json`
- `data/seed_keys.json`
- `data/gateway_config.sample.json`
- `data/sample_requests.jsonl` (test inputs only — not entities to store)
- `data/routing_eval.jsonl` (labeled router test cases — for evaluation only; do not use the labels inside your routing logic)

These files are the canonical seed data. `gateway_config.sample.json` shows one reasonable shape for provider/alias configuration; you may restructure it, but keep the provider names, model names, alias names, and seed key values, because the demo flow and scripts reference them.

## Entities

### Virtual Key (Tenant)

Required fields:

- `virtual_key`
- `team`
- `monthly_budget_usd`
- `rate_limit.requests_per_minute`
- `model_allowlist`
- `semantic_cache.enabled`
- `semantic_cache.similarity_threshold` (when enabled)
- `status` (active/disabled)
- `created_at`

Good To Have fields:

- `rate_limit.tokens_per_minute`
- guardrail configuration

### Provider

Required fields:

- `name`
- `base_url`
- `api_key`

Provider API keys are gateway secrets. They must never appear in responses, logs, or client-visible errors.

### Model Alias

Required fields:

- `alias`
- `primary` (provider model)
- `fallbacks` (ordered list)

### Model Price

Required fields:

- `model`
- `input_per_1m`
- `output_per_1m`

### Request Log Entry

One per data-plane request, including rejected ones. Required fields:

- `request_id`
- `virtual_key`
- `requested_model` (alias or model as sent by the caller)
- `resolved_provider` and `resolved_model` (null for rejections and cache hits)
- `status` (`ok`, `cache_hit`, `rejected_auth`, `rejected_allowlist`, `rejected_rate_limit`, `rejected_budget`, `upstream_error`, ...)
- `prompt_tokens`, `completion_tokens`
- `cost_usd`
- `cache` (`hit`/`miss`)
- `fallback` (boolean)
- `route_reason` (nullable; for `auto` requests: the chosen tier and why)
- `retries` (count)
- `latency_ms`
- `created_at`

This is the minimal trace needed to debug AI traffic. Full prompt/response body logging is a deliberate design decision — if you store bodies, document retention and privacy implications.

### Usage Record

Spend and token aggregates per key per month (or a scheme of your choice) used for budget checks and the usage API.

Correctness requirements:

- Budget enforcement reads must be cheap (do not scan all logs on every request).
- Increments must be atomic — two concurrent requests must both be counted.
- Decide and document whether a request that is admitted concurrently with another may slightly overshoot the budget, and by how much.
- Streaming complicates admission: the final cost is unknown when the request is admitted. Admitting a stream when budget remains at the start — and letting in-flight streams overshoot the budget by their own final cost — is acceptable if documented.

### Cache Entry

Required fields:

- `virtual_key` (cache is scoped per key)
- `model` (or alias) the entry was created under
- prompt representation (embedding vector, or your documented substitute's representation)
- the stored response payload
- `created_at`
- `hit_count`

Good To Have fields:

- `ttl` / `expires_at`

## Relationships

- Many request log entries belong to one virtual key.
- Usage records aggregate request log entries per key.
- Cache entries belong to one virtual key.
- Aliases reference models that must exist in the price table; fallbacks reference models served by registered providers.

## Storage Expectations

- Keys, logs, and usage survive a gateway restart.
- Rate-limit state may be in memory for Must Have (single instance), but must be race-safe: token bucket, sliding window with atomic operations, or equivalent — not read-then-write.
- The cache store choice is yours (same database, a dedicated table, or an in-memory store with persistence); document eviction behavior.
