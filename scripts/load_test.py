#!/usr/bin/env python3
"""Concurrency load test for a Prism gateway. Zero dependencies (Python 3.9+ stdlib).

Fires a burst of concurrent, cache-busting chat completions and reports:

  1. Over-admission: with --rpm-limit set to the key's configured limit, the
     number of accepted (2xx) requests must not exceed the limit. More means
     the rate limiter has a read-then-write race.
  2. Accounting totals: client-side sums of requests, tokens, and
     x-prism-cost-usd headers - compare these against your usage API for the
     same key and window. They should reconcile exactly.
  3. Latency: client-observed average and p95 for the burst.

Run against the mock providers so the burst is free and deterministic:

    python3 load_test.py --url http://localhost:8080 --key prism-sk-free-7g8h9i \
        --model fast --requests 30 --concurrency 10 --rpm-limit 10

Exit code is 1 only when over-admission is detected (with --rpm-limit) or no
request succeeded at all.
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


def fire(base, key, model, salt, results, lock):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": f"({salt}) What is a message queue and when should I use one?"}],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    start = time.monotonic()
    outcome = {"status": None, "latency_ms": None, "cost": None,
               "prompt_tokens": 0, "completion_tokens": 0, "headers_ok": False}
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode(errors="replace"))
            headers = {k.lower(): v for k, v in resp.headers.items()}
            outcome["status"] = resp.status
            usage = payload.get("usage") or {}
            outcome["prompt_tokens"] = usage.get("prompt_tokens", 0)
            outcome["completion_tokens"] = usage.get("completion_tokens", 0)
            outcome["headers_ok"] = all(h in headers for h in
                                        ("x-prism-provider", "x-prism-cache", "x-prism-cost-usd"))
            try:
                outcome["cost"] = float(headers.get("x-prism-cost-usd", ""))
            except ValueError:
                outcome["cost"] = None
    except urllib.error.HTTPError as e:
        outcome["status"] = e.code
        e.read()
    except Exception as e:  # timeout, connection refused, malformed body
        outcome["status"] = f"error: {type(e).__name__}"
    outcome["latency_ms"] = round((time.monotonic() - start) * 1000)
    with lock:
        results.append(outcome)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="gateway base URL")
    parser.add_argument("--key", required=True, help="virtual API key to burst with")
    parser.add_argument("--model", default="fast", help="model or alias the key may use")
    parser.add_argument("--requests", type=int, default=30, help="total requests in the burst")
    parser.add_argument("--concurrency", type=int, default=10, help="concurrent threads")
    parser.add_argument("--rpm-limit", type=int, default=None,
                        help="the key's configured requests-per-minute limit, for the over-admission check")
    args = parser.parse_args()

    results, lock = [], threading.Lock()
    print(f"Bursting {args.requests} requests at concurrency {args.concurrency} ...")
    burst_start = time.monotonic()
    pending = list(range(args.requests))
    threads = []

    def worker():
        while True:
            with lock:
                if not pending:
                    return
                pending.pop()
            fire(args.url, args.key, args.model, uuid.uuid4().hex[:8], results, lock)

    for _ in range(min(args.concurrency, args.requests)):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    burst_seconds = time.monotonic() - burst_start

    accepted = [r for r in results if r["status"] == 200]
    rate_limited = [r for r in results if r["status"] == 429]
    other = [r for r in results if r not in accepted and r not in rate_limited]
    latencies = [r["latency_ms"] for r in accepted] or [0]
    costs = [r["cost"] for r in accepted if r["cost"] is not None]

    print(f"\nBurst finished in {burst_seconds:.1f}s")
    print(f"  accepted (200):      {len(accepted)}")
    print(f"  rate limited (429):  {len(rate_limited)}")
    if other:
        print(f"  other outcomes:      {len(other)}  {sorted({str(r['status']) for r in other})}")
    print(f"  latency avg/p95 ms:  {round(statistics.mean(latencies))} / "
          f"{round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)])}")

    missing_headers = [r for r in accepted if not r["headers_ok"]]
    if missing_headers:
        print(f"  WARNING: {len(missing_headers)} accepted responses missing x-prism-* headers")

    print("\nClient-side accounting totals - compare with your usage API for this key:")
    print(f"  accepted requests:   {len(accepted)}")
    print(f"  prompt tokens:       {sum(r['prompt_tokens'] for r in accepted)}")
    print(f"  completion tokens:   {sum(r['completion_tokens'] for r in accepted)}")
    if costs:
        print(f"  sum of cost headers: {sum(costs):.6f} USD ({len(costs)} of {len(accepted)} had a numeric header)")

    failed = False
    if not accepted:
        print("\nFAIL: no request succeeded - is the gateway up and the key valid?")
        failed = True
    if args.rpm_limit is not None:
        over = len(accepted) - args.rpm_limit
        if burst_seconds > 60:
            print(f"\nNOTE: burst took {burst_seconds:.0f}s (>1 minute); over-admission check "
                  f"is only meaningful for bursts inside one rate-limit window.")
        elif over > 0:
            print(f"\nFAIL: over-admission - {len(accepted)} accepted but the limit is "
                  f"{args.rpm_limit}. Your rate limiter has a race.")
            failed = True
        else:
            print(f"\nOver-admission check OK: {len(accepted)} accepted <= limit {args.rpm_limit}.")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
