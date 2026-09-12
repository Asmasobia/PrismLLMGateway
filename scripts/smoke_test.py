#!/usr/bin/env python3
"""Smoke test for a Prism gateway. Zero dependencies (Python 3.9+ stdlib).

Checks the response header contract and core behaviors:
  1. non-streaming completion works and carries x-prism-* headers
  2. streaming returns SSE token chunks ending in [DONE]
  3. requests without a valid key are rejected
  4. unknown models return a clear 4xx error
  5. an identical repeated prompt is a cache hit
  6. paraphrased prompts hit the cache (two pairs; each is threshold-dependent
     and soft on its own, but at least one pair must hit - exact-match-only
     caching fails)
  7. an unrelated prompt is a cache miss

Usage:
    python3 smoke_test.py --url http://localhost:8080 --key <virtual-key> [--model fast]

Passing this is the baseline, not the goal: it does not test budgets,
rate limits, failover, or accounting accuracy. Those are demo/report items.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

RESULTS = []


def record(name, ok, detail="", soft=False):
    tag = "PASS" if ok else ("WARN" if soft else "FAIL")
    RESULTS.append((tag, name, detail))
    print(f"  {tag:4}  {name}" + (f"  ({detail})" if detail else ""))


def post_chat(base, key, body, stream=False):
    """Returns (status, headers, parsed_json_or_raw_text)."""
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode(errors="replace")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            if stream:
                return resp.status, headers, raw
            return resp.status, headers, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return e.code, {k.lower(): v for k, v in e.headers.items()}, parsed


def simple_body(model, prompt, stream=False):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if stream:
        body["stream"] = True
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="gateway base URL, e.g. http://localhost:8080")
    parser.add_argument("--key", required=True, help="a valid virtual API key")
    parser.add_argument("--model", default="fast", help="a model or alias the key is allowed to use (default: fast)")
    args = parser.parse_args()

    salt = uuid.uuid4().hex[:8]  # keeps this run's prompts out of any earlier cache

    print("\n[1] Non-streaming completion + header contract")
    status, headers, body = post_chat(args.url, args.key,
                                      simple_body(args.model, f"({salt}) What is a load balancer?"))
    record("returns 200", status == 200, f"got {status}")
    ok_shape = isinstance(body, dict) and body.get("choices") and body.get("usage")
    record("body has choices and usage", bool(ok_shape))
    for h in ("x-prism-provider", "x-prism-cache", "x-prism-cost-usd"):
        record(f"header {h} present", h in headers, headers.get(h, "missing"))
    record("first request is a cache miss", headers.get("x-prism-cache") == "miss",
           headers.get("x-prism-cache", "missing"))

    print("\n[2] Streaming")
    status, headers, raw = post_chat(args.url, args.key,
                                     simple_body(args.model, f"({salt}) Explain SSE briefly.", stream=True),
                                     stream=True)
    record("returns 200", status == 200, f"got {status}")
    record("content-type is text/event-stream",
           "text/event-stream" in headers.get("content-type", ""), headers.get("content-type", ""))
    lines = [l for l in raw.splitlines() if l.startswith("data: ")]
    record("received multiple SSE chunks", len(lines) >= 3, f"{len(lines)} data lines")
    record("stream ends with [DONE]", bool(lines) and lines[-1] == "data: [DONE]")

    print("\n[3] Auth")
    status, _, _ = post_chat(args.url, "definitely-not-a-real-key",
                             simple_body(args.model, "hello"))
    record("invalid key rejected with 401/403", status in (401, 403), f"got {status}")

    print("\n[4] Unknown model")
    status, _, body = post_chat(args.url, args.key, simple_body("no-such-model-xyz", "hello"))
    record("unknown model rejected with 4xx", 400 <= status < 500, f"got {status}")
    record("error body explains the problem",
           isinstance(body, dict) and "error" in body, str(body)[:80])

    print("\n[5] Semantic cache: exact repeat")
    prompt = f"({salt}) How do I reset my password on the dashboard?"
    post_chat(args.url, args.key, simple_body(args.model, prompt))
    status, headers, _ = post_chat(args.url, args.key, simple_body(args.model, prompt))
    record("identical repeat is a cache hit", headers.get("x-prism-cache") == "hit",
           headers.get("x-prism-cache", "missing"))

    print("\n[6] Semantic cache: paraphrases (each pair soft; at least one pair must hit)")
    paraphrase_a = f"({salt}) What are the steps to reset my dashboard password?"
    status, headers, _ = post_chat(args.url, args.key, simple_body(args.model, paraphrase_a))
    hit_a = headers.get("x-prism-cache") == "hit"
    record("pair A paraphrase is a cache hit", hit_a,
           headers.get("x-prism-cache", "missing"), soft=True)
    prompt_b = f"({salt}) What is the refund policy for annual plans?"
    post_chat(args.url, args.key, simple_body(args.model, prompt_b))
    paraphrase_b = f"({salt}) If I bought an annual plan, can I get my money back?"
    status, headers, _ = post_chat(args.url, args.key, simple_body(args.model, paraphrase_b))
    hit_b = headers.get("x-prism-cache") == "hit"
    record("pair B paraphrase is a cache hit", hit_b,
           headers.get("x-prism-cache", "missing"), soft=True)
    record("matching is semantic, not exact-only (at least one pair hit)", hit_a or hit_b)

    print("\n[7] Semantic cache: unrelated prompt")
    unrelated = f"({salt}) Compare TCP and UDP for game servers."
    status, headers, _ = post_chat(args.url, args.key, simple_body(args.model, unrelated))
    record("unrelated prompt is a cache miss", headers.get("x-prism-cache") == "miss",
           headers.get("x-prism-cache", "missing"))

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print(f"\n{'=' * 50}")
    print(f"  {len(RESULTS) - len(fails) - len(warns)} passed, {len(warns)} warnings, {len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
