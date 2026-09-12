# Prism Evaluation Guide

This guide defines language-agnostic verification expectations for Prism. Unlike an answer-quality capstone, Prism is infrastructure: it is verified by behavior under contract checks, concurrency, and injected failures — not by judging model output.

Verification has four parts:

1. the provided **smoke test** (contract compliance),
2. the provided **load test** (correctness under concurrency),
3. a **routing eval** you build over the provided labeled cases (AI decision quality),
4. a **manual failure drill** you record in your demo video.

## 1. Smoke Test

```bash
python3 scripts/smoke_test.py --url <gateway> --key <virtual-key> --model fast
```

What each check means:

| Check | What it proves |
|---|---|
| Non-streaming 200 with `choices` and `usage` | OpenAI compatibility |
| `x-prism-provider`, `x-prism-cache`, `x-prism-cost-usd` present | header contract |
| First salted request is a `miss` | cache does not false-positive |
| SSE chunks ending in `data: [DONE]` | streaming works and terminates |
| Invalid key rejected 401/403 | authentication |
| Unknown model rejected 4xx with an error body | clean error mapping |
| Identical repeat is a `hit` | caching works at all |
| Two paraphrase pairs; each WARN-level alone, at least one must `hit` | matching is semantic, not exact-only |
| Unrelated prompt is a `miss` | threshold is not absurdly low |

Each individual paraphrase check is WARN-level because the right threshold depends on your embedding choice — but if neither pair hits, the test fails: exact-match-only caching does not meet the Must Have. Document your threshold and report the behavior of both sample pairs (`req_cache_a1`/`req_cache_a2` and `req_cache_b1`/`req_cache_b2`) in your verification report.

A passing smoke test is the baseline, not the goal.

## 2. Load Test

```bash
python3 scripts/load_test.py --url <gateway> --key prism-sk-free-7g8h9i --model fast \
    --requests 30 --concurrency 10 --rpm-limit 10
```

Run it against the mock providers so it is free and deterministic. It fires concurrent, cache-busting requests and reports:

### Over-Admission

With `--rpm-limit` set to the key's configured limit, the number of accepted (2xx) requests in the burst must not exceed the limit. Accepted > limit means your limiter has a race (read-then-write) — fix it. Target: **zero over-admission**.

### Accounting Totals

The script sums, client-side: accepted requests, prompt/completion tokens from response bodies, and `x-prism-cost-usd` headers. Compare these against your usage API for the same key and window. Target: **exact match** (within documented rounding of the cost value).

### Latency

Client-observed average and p95 for the burst — a sanity number for your report, not a pass/fail gate.

## 3. Routing Eval

Use `data/routing_eval.jsonl`. Each line contains:

- `id`
- `expected_tier` (`fast` or `smart`)
- `note` (why the case is labeled that way)
- `prompt`

Build one command, endpoint, or test that sends every prompt through your `auto` router (classification only — you do not need to complete the upstream call) and reports:

```json
{
  "total": 20,
  "correct": 17,
  "accuracy": 0.85,
  "cases": [
    {"id": "route_011", "expected": "smart", "actual": "smart", "reason": "proof request; judged complex"}
  ]
}
```

Rules and expectations:

- Do not use `expected_tier` inside your routing logic — it is the answer key, for evaluation only.
- The set is trapped: short-but-hard prompts (`route_011`, `route_012`, `route_013`, `route_019`, `route_020`) and long-but-trivial ones (`route_006`, `route_007`, `route_010`). A length-only heuristic scores around 60% by design.
- Report your accuracy honestly and explain the misses. A documented method reaching 80%+ is a solid baseline; beating it with a cleverer classifier is visible, gradable work.
- Log the routing reason per case so a reviewer can see *why* the router chose a tier.

## 4. Manual Failure Drill

Record this in your demo (see the problem statement's Recommended Demo Flow):

- Kill provider `alpha` live (`POST /admin/config {"mode": "down"}`) → next request served by `beta`, `x-prism-fallback: true`, logged as a fallback.
- Make `alpha` slow (`latency_ms: 3000` or more) → your timeout fires; the client gets a timely response (fallback or clean error), never a hang.
- Make `alpha` flaky (`fail_rate: 0.3`) → retries absorb it; requests still succeed.
- Restore `alpha` → traffic returns to the primary (immediately for Must Have; health-window-based recovery is Good To Have).

## Additional Checks Reviewers May Run

- **Cache isolation:** the same prompt sent with two different keys must not share a cache entry. A cross-tenant hit is a data leak and an automatic correctness failure.
- **Streaming honesty:** watching the stream, tokens should arrive progressively (the mock providers pace ~20ms per token). A "stream" that arrives all at once was buffered.
- **Budget edge:** the seeded budget-demo key (`prism-sk-budget-demo-0j1k2l`) gets one request through, then a clean `budget_exceeded` rejection — and the rejection itself is logged.
- **Secret hygiene:** provider API keys never appear in responses, logs shown in the console, or error messages.

## What to Include in Your Verification Report

- Smoke test output (all PASS; explain any WARN).
- Load test output, plus the matching usage-API numbers demonstrating reconciliation.
- Routing eval accuracy with per-case results, your classification method, and an explanation of the misses.
- Your semantic-cache threshold, embedding choice, and observed hit/miss behavior on both sample paraphrase pairs and the near-miss case (`req_cache_a3`).
- Gateway added latency (coarse measurement is fine — document how you measured).
- Known limitations (for example single-instance rate limiting).
