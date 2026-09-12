#!/usr/bin/env python3
"""Mock OpenAI-compatible LLM provider for testing Prism gateways.

Zero dependencies (Python 3.9+ stdlib only). Run two on different ports to
simulate a multi-provider setup:

    python3 mock_provider.py --port 9001 --name alpha
    python3 mock_provider.py --port 9002 --name beta

Endpoints:
    POST /v1/chat/completions   OpenAI-compatible; supports "stream": true
    GET  /health                {"status": "ok", "name": ...}
    GET  /admin/config          current failure-injection config
    POST /admin/config          set failure injection, e.g.
                                {"mode": "ok"}            healthy (default)
                                {"mode": "down"}          every request -> 503
                                {"mode": "rate_limited"}  every request -> 429
                                {"fail_rate": 0.5}        50% of requests -> 500
                                {"latency_ms": 3000}      delay before responding

Models served: <name>-small and <name>-large (e.g. alpha-small). Any other
model returns a 404 error like a real provider would.

Auth: any non-empty "Authorization: Bearer ..." header is accepted, unless
--api-key is given, in which case it must match.

Token accounting: tokens are approximated as whitespace-separated words. Both
streaming and non-streaming responses include a "usage" object (streaming
sends it in the final chunk before [DONE]).

Refusal trigger: a <name>-small model refuses any prompt containing [refuse]
("I'm sorry, but I can't help with that."); the -large models answer it.
Useful for testing response-quality escalation (detect the refusal, retry on
a stronger model).
"""

import argparse
import json
import random
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = {"mode": "ok", "fail_rate": 0.0, "latency_ms": 0}
CONFIG_LOCK = threading.Lock()

REPLIES = [
    "Here's a concise answer: {topic}. In practice, start simple, measure, then iterate.",
    "Short version: {topic}. The trade-off is latency versus cost, so profile before optimizing.",
    "Good question about {topic}. The standard approach works for most cases; edge cases need retries and idempotency.",
    "Regarding {topic}: cache what repeats, stream what is long, and meter everything you pay for.",
]


def count_tokens(text):
    return max(1, len(text.split()))


def build_reply(provider_name, model, messages):
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "your request",
    )
    # Escalation hook: small models refuse [refuse]-marked prompts, large ones answer
    if "[refuse]" in str(last_user).lower() and model.endswith("-small"):
        return "I'm sorry, but I can't help with that."
    topic = " ".join(str(last_user).split()[:12])
    # Deterministic per prompt (crc32, not hash(): hash() is salted per process)
    template = REPLIES[zlib.crc32(topic.encode()) % len(REPLIES)]
    reply = f"[{provider_name}:{model}] " + template.format(topic=topic)
    if model.endswith("-large"):
        reply += " A more thorough treatment would cover failure modes, observability, and cost controls in depth."
    return reply


class Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.server.provider_name}] {self.address_string()} {fmt % args}")

    def _send_json(self, status, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message, err_type="invalid_request_error", headers=None):
        self._send_json(status, {"error": {"message": message, "type": err_type}}, headers)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "name": self.server.provider_name})
        elif self.path == "/admin/config":
            with CONFIG_LOCK:
                self._send_json(200, dict(CONFIG))
        else:
            self._error(404, f"Unknown path {self.path}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self._error(400, "Request body is not valid JSON")

        if self.path == "/admin/config":
            with CONFIG_LOCK:
                for key in ("mode", "fail_rate", "latency_ms"):
                    if key in payload:
                        CONFIG[key] = payload[key]
                return self._send_json(200, dict(CONFIG))

        if self.path != "/v1/chat/completions":
            return self._error(404, f"Unknown path {self.path}")

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            return self._error(401, "Missing bearer token", "authentication_error")
        if self.server.api_key and auth != f"Bearer {self.server.api_key}":
            return self._error(401, "Invalid API key", "authentication_error")

        with CONFIG_LOCK:
            cfg = dict(CONFIG)
        if cfg["latency_ms"]:
            time.sleep(cfg["latency_ms"] / 1000)
        if cfg["mode"] == "down":
            return self._error(503, "Provider is down (injected)", "server_error")
        if cfg["mode"] == "rate_limited":
            return self._error(429, "Rate limit exceeded (injected)", "rate_limit_error",
                               headers={"Retry-After": "5"})
        if cfg["fail_rate"] and random.random() < float(cfg["fail_rate"]):
            return self._error(500, "Internal error (injected)", "server_error")

        name = self.server.provider_name
        model = payload.get("model", "")
        if model not in (f"{name}-small", f"{name}-large"):
            return self._error(404, f"Model '{model}' does not exist", "not_found_error")

        messages = payload.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return self._error(400, "'messages' must be a non-empty list")

        reply = build_reply(name, model, messages)
        prompt_tokens = sum(count_tokens(str(m.get("content", ""))) for m in messages)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": count_tokens(reply),
            "total_tokens": prompt_tokens + count_tokens(reply),
        }
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if payload.get("stream"):
            return self._stream(completion_id, created, model, reply, usage)

        self._send_json(200, {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": usage,
        })

    def _stream(self, completion_id, created, model, reply, usage):
        # HTTP/1.0 + connection close, so no chunked encoding needed
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta, finish_reason=None, with_usage=False):
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if with_usage:
                data["usage"] = usage
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        try:
            chunk({"role": "assistant", "content": ""})
            for word in reply.split(" "):
                chunk({"content": word + " "})
                time.sleep(0.02)  # visible token-by-token pacing
            chunk({}, finish_reason="stop", with_usage=True)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-stream; not an error worth a traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--name", default="alpha", help="provider name; serves <name>-small and <name>-large")
    parser.add_argument("--api-key", default=None, help="if set, require this exact bearer token")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.provider_name = args.name
    server.api_key = args.api_key
    print(f"Mock provider '{args.name}' on http://localhost:{args.port}")
    print(f"  models: {args.name}-small, {args.name}-large")
    print(f"  failure injection: POST /admin/config  e.g. {{\"mode\": \"down\"}}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
