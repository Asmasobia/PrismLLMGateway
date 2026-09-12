# AI-First Software Engineering Capstone - Case Study

## Prism: LLM Gateway and Semantic Cache

**Author:** Airtribe

## Background & Objective

Every AI product you have used this year sits behind the same invisible layer: a gateway between the application and the model providers. It is the infrastructure layer every AI company now runs — one place that handles keys, budgets, failover, caching, and cost accounting, so 40 teams do not each wire up providers their own way. Products like LiteLLM, OpenRouter, and Portkey exist because this is now core infrastructure, the way API gateways and load balancers were a decade ago.

Prism is an AI-first product where the gateway itself makes AI decisions in the hot path: it judges how hard each prompt is to route it to the right model tier, and it matches meaning — not strings — to serve repeated questions from cache. There is no chatbot to build; the challenge is streaming, metering, reliability, and multi-tenancy done properly, with machine judgment running inside the request path.

The objective is to build a production-grade LLM gateway that can:

- Route requests across multiple model providers through one unified, OpenAI-compatible API.
- Judge prompt difficulty and route automatically — cheap models for simple prompts, strong models for hard ones — and prove it against a labeled eval set.
- Fall back automatically when a provider goes down, rate-limits, or degrades.
- Stream responses token by token, end to end, without buffering.
- Meter usage per tenant — every request, every token, every dollar.
- Enforce cost budgets and rate limits per team.
- Answer repeated questions from a semantic cache instead of paying for the same tokens twice.
- Keep a request log complete enough to debug any AI traffic issue.

## Language and Stack

This project is intentionally language agnostic. You may implement it in Node.js, Java, Python, Go, Ruby, or any stack of your choice.

You are free to choose your framework, database, cache store, and embedding approach. You do not need a paid AI provider: the pack includes a zero-dependency mock provider that speaks the OpenAI wire format, supports streaming, and lets you inject failures on demand. Free-tier APIs (for example Gemini or Groq), locally hosted models (for example via Ollama), or the mock providers are all acceptable upstreams. Your choices must be documented, and the system must satisfy the functional, correctness, and verification requirements described below.

The provided Python scripts are optional local utilities. They are not part of the required implementation stack.

## Scope

Prism has a clear core scope. Build the Must Have features first. Good To Have and Stretch features should only be attempted after the core gateway works end to end.

## Recommended Demo Flow

Your final demo should be able to show this flow clearly:

1. Start both mock providers and show your gateway is healthy.
2. Send a non-streaming request with the `search` team key (`prism-sk-search-1a2b3c`) to the `fast` alias. Show the response headers: which provider served it, cache miss, and cost.
3. Send a streaming request and show tokens arriving one by one.
4. Repeat the same prompt, then a paraphrase of it (see `req_cache_a1` / `req_cache_a2` in `data/sample_requests.jsonl`). Show `x-prism-cache: hit` on both.
5. Send two requests to the `auto` alias with the research key: a short-but-hard prompt (`route_011`, a proof) and a long-but-trivial one (`route_006`, a roster lookup). Show the routing decisions: the proof lands on `smart`, the lookup on `fast` — and show your routing eval accuracy over `data/routing_eval.jsonl`.
6. Burst past the free-tier key's rate limit and show clean rejections.
7. Send two requests with the budget-demo key (`prism-sk-budget-demo-0j1k2l`, seeded with a deliberately tiny budget): the first consumes it, the second is rejected with a clear budget-exceeded error.
8. Take provider `alpha` down live (`POST /admin/config {"mode": "down"}` on the mock) and show the same request served by `beta` with `x-prism-fallback: true`.
9. Open the ops console: usage by key, recent requests, cache hit rate.
10. Run the provided smoke test and load test and show the summaries.

## Acceptable Simplifications

- The two mock providers are a complete multi-provider setup. Adding a real hosted or local provider is optional.
- Semantic matching may use an embeddings API, a local embedding model, or a clearly documented local substitute (for example normalized bag-of-words cosine similarity). Exact-match-only caching does not satisfy the requirement — the provided paraphrase pairs should hit.
- The `auto` router may use any documented method (embeddings, a small LLM judge via a free-tier or local model, or engineered heuristics). It is graded by accuracy on the provided eval set, not by technique. Note that the mock providers return canned text, so an LLM-judge classifier needs a real (free-tier or local) model.
- Budget windows can be simple calendar months with reset-on-read. Proration is not required.
- A single-instance, in-memory rate limiter is acceptable as long as it is race-safe within that instance. Distributed rate limiting is Stretch.
- Tenants can be seeded from `data/seed_keys.json`. Key-management APIs are Good To Have.
- The ops console can be a simple page with tables and numbers. UI polish is not graded.
- Added-latency measurement can be coarse (timestamps around the upstream call).

## Avoid These Mistakes

- Do not buffer the full upstream response before forwarding a streaming request. Pass-through streaming is graded.
- Do not compute cost from client-declared token counts. Use the provider's `usage` object (the mock providers return one, including on streams).
- Do not share the semantic cache across tenants by default. A cache hit that returns another team's response is a data leak. Scope the cache per key unless you document a deliberate, safe exception.
- Do not cache error responses, and think before caching time-sensitive prompts (see `req_no_cache` in the sample requests).
- Do not implement rate limiting or budget checks as read-then-write. Concurrent requests will over-admit. The load test checks for this.
- Do not silently splice two providers' outputs when a stream fails midway. Decide and document your mid-stream failure behavior.
- Do not let callers hardcode provider model names. Aliases (`fast`, `smart`) exist so routing stays a gateway decision.
- Do not let a hung provider hang your client. Every upstream call needs a timeout.
- Do not build Good To Have features before the Must Have gateway works end to end.

## Must Have

### 1. Load the Provided Data

- Load the provided model price table, seed tenants, and gateway configuration (`data/model_pricing.json`, `data/seed_keys.json`, `data/gateway_config.sample.json`).
- Use any persistent store you prefer.
- Preserve the seed key values and alias names — the demo flow and provided scripts reference them.

### 2. Unified Chat Completions API

- Expose one OpenAI-compatible `POST /v1/chat/completions` endpoint in front of at least 2 providers (the mock providers count).
- Callers use a model alias or model name; the gateway resolves it and never exposes provider credentials.
- Reject unknown models or aliases with a clear 4xx error; map upstream provider errors to clean gateway errors.
- Every response must include the header contract:

| Header | Value |
|---|---|
| `x-prism-provider` | which upstream provider/model served the request |
| `x-prism-cache` | `hit` or `miss` |
| `x-prism-fallback` | `true` only when a fallback provider served it |
| `x-prism-cost-usd` | computed cost of the request |

Streaming exception: headers are sent before the final usage is known, so `x-prism-cost-usd` is not required on streaming responses — but `x-prism-provider`, `x-prism-cache`, and `x-prism-fallback` still are, and the stream's cost must still land in the request log and usage API.

The provided smoke test depends on this contract.

### 3. Streaming Pass-Through

- Support `"stream": true` end to end: tokens flow from the provider through your gateway to the client as they arrive.
- The stream terminates correctly (`data: [DONE]`) and usage/cost is still recorded for streamed requests.

### 4. Virtual Keys, Rate Limits, and Budgets

- Authenticate every request with a virtual key from the seed data.
- Enforce per-key: a model allowlist, a requests-per-minute rate limit, and a monthly cost budget.
- Each rejection type returns a distinct, documented error (invalid key, model not allowed, rate limited, budget exhausted).

### 5. Routing, Retries, and Failover

- Resolve aliases (`fast`, `smart`) to a primary model with an ordered fallback chain.
- Retry transient upstream errors with exponential backoff.
- Fail over to the next provider when one is down or rate-limited, and record it (`x-prism-fallback`, request log).

### 6. Smart Routing: the `auto` Alias

- Callers can send `"model": "auto"` and let the gateway decide the tier: the gateway classifies each prompt's difficulty and routes simple prompts to `fast` and complex ones to `smart`.
- The classifier is your choice — an embedding-based approach, a small LLM judge, or a documented heuristic — but it must be better than guessing by length: `data/routing_eval.jsonl` deliberately contains short-but-hard prompts (proofs, systems debugging) and long-but-trivial ones (roster lookups, log extraction).
- Build a one-command routing eval that runs all cases in `data/routing_eval.jsonl` through your router and reports accuracy, with per-case expected vs actual. Include the result in your verification report.
- Log every `auto` decision: which tier was chosen and why (score, matched signal, or judge output).

### 7. Semantic Response Cache

- Cache successful responses and serve a cached response when a new prompt is semantically similar (similarity above a threshold), not just an exact match.
- The cache is scoped per key and configurable per key (on/off, threshold — see the seed data).
- Report hits via the header contract and track hit rates.

### 8. Metering, Request Logs, and Usage API

- Compute the cost of every request from the price table using provider-reported token usage.
- Store a request log entry for every request: key, requested model/alias, resolved provider and model, status, tokens, cost, cache result, fallback flag, and latency.
- Expose a usage summary API: spend and token consumption by key.
- Accounting must stay accurate under concurrent requests.

### 9. Simple Ops Console and Verification

- Build a simple frontend for the platform team: usage by key, recent requests, and cache hit rate. You may vibe-code or AI-assist the console; polish is not graded.
- Your gateway must pass `scripts/smoke_test.py`.
- Run `scripts/load_test.py` and include its report (over-admission and accounting totals) in your submission.
- Run your routing eval over `data/routing_eval.jsonl` and include the accuracy report.
- Provide automated tests for critical logic such as cost computation and the rate limiter.

## Good To Have

- Response-quality escalation: inspect the model's answer and, when the cheap model refuses or returns garbage, automatically retry the request on a stronger tier and record the escalation. (The mock providers support this: `-small` models refuse any prompt containing `[refuse]`; `-large` models answer it.)
- AI guardrails as gateway middleware: classify incoming prompts for injection/jailbreak attempts or PII, and block, redact, or flag per key configuration, with guardrail results logged.
- Degradation-based failover: track per-provider error rates and latency over a window and route around slow-but-alive providers, plus a provider-health endpoint.
- Tokens-per-minute limits in addition to requests-per-minute.
- Key management APIs: create, rotate, and disable virtual keys.
- Usage breakdowns by model and by day; simple charts in the console.
- A real embedding model for the cache with per-key tunable thresholds, TTLs, and cached responses replayed as proper SSE streams.
- Fuller observability: added-latency percentiles, per-provider latency and error dashboards, routing-decision breakdowns.

## Stretch

- Distributed rate limiting and budgets across multiple gateway instances (for example via Redis), correct under concurrency.
- Weighted or least-latency routing across healthy providers.
- Protocol translation: add a provider with a different wire format (for example the Anthropic Messages API) behind the same OpenAI-compatible front door.
- Cost anomaly detection with alert webhooks.
- Organizations: teams grouped under orgs with shared budgets and rollup reporting.

## Technical Requirements

- Expose core functionality as an HTTP API. The data plane must stay OpenAI-compatible because the provided scripts depend on it.
- Use a reliable persistent store for keys, usage records, request logs, and cache entries.
- Usage accounting and rate limiting must be correct under concurrent requests — no lost updates, no over-admission.
- Every upstream call must have a timeout; the gateway must never hang a client on a slow or dead provider.
- Keep the provider integration behind an adapter so it can be pointed at mocks in tests.
- Handle malformed upstream responses and mid-stream failures gracefully, with documented behavior.
- Keep long upstream calls from blocking unrelated requests where practical.
- Provide automated tests for critical flows (cost math, limiter, cache decision, allowlist).
- Do not depend on any specific programming language, framework, or hosted AI provider for the core design.

## Suggested API Surface

You may design your own API shape, but a complete solution should cover flows similar to these:

- `POST /v1/chat/completions`
- `GET /health`
- `GET /admin/usage?key=...&from=...&to=...`
- `GET /admin/logs?key=...&limit=...`
- `GET /admin/cache/stats`
- `GET /admin/providers/health` (Good To Have)
- `POST /admin/keys` (Good To Have)

## Suggested Milestones

You may plan your own schedule, but this staging keeps scope manageable:

1. **Core pass-through:** Load provided data, one provider, non-streaming completions, header contract in place from day one.
2. **Keys and limits:** Authentication, allowlists, rate limits, budgets, cost metering, request logs.
3. **Streaming and reliability:** SSE pass-through, timeouts, retries, failover across both mock providers.
4. **Semantic intelligence:** The semantic cache (exact match first, then similarity), then the `auto` router and its eval — they can share the same embedding machinery.
5. **Console and verification:** Ops console, smoke test green, load test report, routing eval accuracy, documentation, demo video.

The Must Have section above defines the minimum viable submission.

For a more detailed build path and FAQ, see `docs/IMPLEMENTATION_GUIDE.md`.

## Provided Starter Dataset

This capstone pack includes:

- A model price table for cost accounting.
- Four seed tenants with budgets, rate limits, allowlists, and cache settings — including a tiny-budget key for demoing budget exhaustion live.
- A sample gateway configuration: provider registry, model aliases (including `auto`), fallback chains, and retry policy.
- Annotated sample requests, including semantically similar prompt pairs for cache testing and deliberate negative cases.
- A labeled routing eval set (`data/routing_eval.jsonl`): 20 prompts tagged `fast` or `smart`, with short-but-hard and long-but-trivial traps so length heuristics fail.
- A zero-dependency mock OpenAI-compatible provider with live failure injection (down, slow, flaky, rate-limited) and a refusal trigger for escalation testing.
- A smoke test that checks the required API and header contract.
- A load test that checks rate-limit over-admission and accounting accuracy.

You may extend the dataset, but you must document any added data and how it affects verification.

## Assessment Criteria

- **Must Have workflow - 60%:** Does the gateway load the provided data, serve OpenAI-compatible completions over 2 providers with the header contract, stream without buffering, enforce keys/limits/budgets, fail over, route `auto` traffic by difficulty, cache semantically, meter accurately, expose usage, and demo through a simple console?
- **Gateway correctness under pressure - 25%:** Is accounting accurate under concurrency? Does the load test show zero over-admission? Does failover work when a provider is killed live? Does the cache hit paraphrases without leaking across tenants? Does the `auto` router beat a length-only baseline on the provided eval set? Do streams terminate cleanly on upstream failure?
- **Engineering quality, documentation, and demo - 15%:** Is the code maintainable, is setup clear, are design decisions documented, and does the demo clearly show the core workflow?
- Good To Have and Stretch work can strengthen the project, but it should not compensate for missing Must Have functionality.

## Deliverables

1. Final functional product: the gateway and a simple ops console.
2. README with setup instructions, API documentation, architecture, and design decisions (especially: routing/failover design and cache scoping).
3. Public GitHub repository link.
4. Seeded demo keys and instructions to reproduce the demo with the mock providers.
5. Verification report: smoke test output, the load test report (over-admission, accounting totals, latency), and routing eval accuracy with per-case results.
6. Explainer video demonstrating the project, including a live provider failover and a semantic cache hit on a paraphrased prompt.
