# Prism API Contract

This document describes the expected product and API behavior without requiring any specific programming language or framework.

You may change admin route names, request shapes, or response shapes if you document the changes clearly. The data plane is the exception: `POST /v1/chat/completions` must stay OpenAI-compatible and must return the `x-prism-*` headers, because the provided smoke test and load test depend on them.

## Personas

- `application caller`: A team's service calling the gateway with a virtual key. Never sees provider credentials.
- `platform admin`: Operates the gateway — tenants, usage, logs, provider health.

For Must Have, admin APIs can be protected by a simple demo token. Key-management APIs are Good To Have.

## Core Resources

- Virtual key (tenant)
- Provider
- Model alias
- Request log entry
- Usage record
- Cache entry

Good To Have resources:

- Provider health snapshot
- Guardrail configuration

## Authentication

- Data plane: `Authorization: Bearer <virtual-key>` using keys from `data/seed_keys.json`.
- Admin plane: any simple documented mechanism (separate admin token is enough).

## Core API Flows

### 1. Chat Completion (Non-Streaming)

`POST /v1/chat/completions`

Expected behavior:

- Authenticate the virtual key.
- Enforce model allowlist, rate limit, and budget — in a documented order.
- Resolve the alias (`fast`, `smart`) or model name to a provider model. For `auto`, classify the prompt's difficulty first, pick a tier, and record the decision and its reason in the request log.
- Check the semantic cache; on miss, forward upstream with a timeout and retries.
- Compute cost from provider-reported usage and the price table.
- Log the request and return an OpenAI-shaped response.

Example request:

```json
{
  "model": "fast",
  "messages": [{"role": "user", "content": "What is a message queue?"}]
}
```

Example response body (OpenAI-compatible):

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "alpha-small",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 5, "completion_tokens": 17, "total_tokens": 22}
}
```

Required response headers:

| Header | Value |
|---|---|
| `x-prism-provider` | upstream provider/model that served it, e.g. `alpha/alpha-small` |
| `x-prism-cache` | `hit` or `miss` |
| `x-prism-fallback` | `true` only when a fallback provider served it |
| `x-prism-cost-usd` | computed cost, e.g. `0.000014` (use `0` for cache hits) |

### 2. Chat Completion (Streaming)

Same endpoint with `"stream": true`.

Expected behavior:

- Respond with `Content-Type: text/event-stream`.
- Forward upstream chunks as they arrive (OpenAI chunk objects in `data:` lines) — no buffering the full completion.
- Terminate with `data: [DONE]`.
- Record usage and cost for streamed requests too (the mock providers include `usage` in the final chunk).
- Headers are sent before the body, so the cost is not known yet: `x-prism-cost-usd` is not required on streaming responses. `x-prism-provider`, `x-prism-cache`, and `x-prism-fallback` are still required up front, and the stream's final cost must be recorded in the request log and usage API.

### 3. Error Responses

Every rejection returns an OpenAI-style error body with a distinct, documented reason:

```json
{
  "error": {
    "message": "Monthly budget of $5.00 exhausted for this key",
    "type": "budget_exceeded",
    "code": "budget_exceeded"
  }
}
```

Suggested mapping:

| Case | Status | `type` |
|---|---|---|
| Missing/unknown virtual key | 401 | `authentication_error` |
| Model not on the key's allowlist | 403 | `model_not_allowed` |
| Rate limit exceeded | 429 | `rate_limit_exceeded` |
| Monthly budget exhausted | 429 or 402 | `budget_exceeded` |
| Unknown model or alias | 404 | `not_found_error` |
| All providers failed | 502 | `upstream_error` |

You may choose different status codes if documented, but each case must be distinguishable from the body.

### 4. Usage Summary

`GET /admin/usage?key=...&from=...&to=...`

Expected behavior:

- Return spend and token totals for a key over a window.

Example response:

```json
{
  "key": "prism-sk-search-1a2b3c",
  "from": "2026-07-01",
  "to": "2026-07-31",
  "requests": 412,
  "prompt_tokens": 18734,
  "completion_tokens": 9211,
  "cost_usd": 0.0141,
  "cache_hits": 96
}
```

The totals here must reconcile with the sum of `x-prism-cost-usd` headers — the load test prints client-side totals to compare against this API.

### 5. Request Logs

`GET /admin/logs?key=...&limit=...`

Expected behavior:

- Return recent request log entries, newest first, filterable by key.
- Each entry carries the fields listed in `docs/DATA_MODEL.md` (Request Log Entry).

### 6. Cache Stats

`GET /admin/cache/stats`

Expected behavior:

- Report hits, misses, and hit rate — overall or per key.

### 7. Provider Health (Good To Have)

`GET /admin/providers/health`

Expected behavior:

- Report per-provider status derived from recent traffic: error rate, latency, and whether the gateway currently considers it healthy.
