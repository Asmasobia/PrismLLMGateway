"""An in-process provider, for tests and for the failure modes sockets can't give you.

This is not a mock in the "assert it was called" sense. It is a second
implementation of `ProviderClient` that behaves like `scripts/mock_provider.py`:
same response shape, same whitespace-word token counting, same failure-injection
vocabulary (`ok`, `down`, `rate_limited`, plus a timeout and a malformed-request
mode the HTTP mock reaches only through `fail_rate`).

Why it lives in `prism/` and not in `tests/`: `scripts/smoke_test.py` and
`scripts/load_test.py` both need a *running* gateway, and standing one up needs
upstreams. Shipping the fake as part of the package means the gateway can be run
end to end with no ports open at all, which is how the failover demo is
reproducible on a machine where nothing else is listening.

Token counting matches the mock deliberately. Cost assertions are the point of
several tests, and a fake that counted tokens differently would let a cost bug
pass in tests and fail against the mock providers the evaluation actually uses.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from prism.config import ResolvedTarget
from prism.providers.base import (
    ProviderCallFailed,
    ProviderClient,
    ProviderStream,
    UpstreamCompletion,
    classify,
    read_usage,
)

#: Same templates as `scripts/mock_provider.py`, selected the same way (crc32 of
#: the topic, not `hash()`, which is salted per process and so is not stable
#: across runs). Identical prompts therefore produce identical replies, which is
#: what makes a cache test meaningful.
REPLIES = (
    "Here's a concise answer: {topic}. In practice, start simple, measure, then iterate.",
    "Short version: {topic}. The trade-off is latency versus cost, so profile before optimizing.",
    "Good question about {topic}. The standard approach works for most cases; edge cases "
    "need retries and idempotency.",
    "Regarding {topic}: cache what repeats, stream what is long, and meter everything you "
    "pay for.",
)

#: Statuses the fake can be told to return, keyed by the mock provider's mode name.
#: `unauthorized` has no counterpart in the mock provider and is here for one
#: scenario: 401 is the status where `retry_same` and `try_next` disagree, so it is
#: the only way to prove the two flags are independent rather than one renamed twice.
MODE_STATUS = {
    "down": 503,
    "rate_limited": 429,
    "bad_request": 400,
    "server_error": 500,
    "unauthorized": 401,
}


def count_tokens(text: str) -> int:
    """Whitespace words, minimum one. Mirrors `scripts/mock_provider.py:57`."""
    return max(1, len(text.split()))


def build_reply(provider_name: str, model: str, messages: list[dict[str, Any]]) -> str:
    """Mirrors `scripts/mock_provider.py:61`, including the `[refuse]` escalation hook."""
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "your request",
    )
    if "[refuse]" in str(last_user).lower() and model.endswith("-small"):
        return "I'm sorry, but I can't help with that."
    topic = " ".join(str(last_user).split()[:12])
    template = REPLIES[zlib.crc32(topic.encode()) % len(REPLIES)]
    reply = f"[{provider_name}:{model}] " + template.format(topic=topic)
    if model.endswith("-large"):
        reply += (
            " A more thorough treatment would cover failure modes, observability, and "
            "cost controls in depth."
        )
    return reply


@dataclass
class Behaviour:
    """How one provider should behave.

    `fail_first` exists for the one scenario the mock provider cannot express: a
    provider that fails a bounded number of times and then recovers. Without it
    there is no way to test that a *retry on the same provider* succeeds, as
    distinct from a failover — and those two paths set `x-prism-fallback`
    differently, so conflating them would hide a header bug.

    The two fields divide cleanly: `mode` says *how* the provider fails, and
    `fail_first` says *how many times*. Zero — the default — means "for as long as
    the mode is set", which is the mock provider's own behaviour.

    `die_after_chunks` is the second thing the mock provider cannot do, and the more
    important one: it kills a stream that has **already delivered output**. That is
    the case `docs/IMPLEMENTATION_GUIDE.md:172` asks every implementation to decide
    and document, and it is untestable over a socket without deliberately crashing a
    server mid-write.
    """

    mode: str = "ok"
    fail_first: int = 0
    latency_ms: int = 0
    die_after_chunks: int = 0

    _failures_served: int = field(default=0, init=False, repr=False)

    def next_status(self) -> int | None:
        """The status this call should fail with, or None to serve normally."""
        if self.mode == "ok":
            return None
        if self.fail_first and self._failures_served >= self.fail_first:
            # The bounded failures have been served; the provider has recovered.
            return None
        self._failures_served += 1
        if self.mode == "timeout":
            return 0  # sentinel: raise a transport failure rather than a status
        return MODE_STATUS.get(self.mode, 503)


class FakeProviderClient(ProviderClient):
    """A `ProviderClient` that never opens a socket."""

    def __init__(self, behaviour: dict[str, Behaviour] | None = None) -> None:
        self._behaviour: dict[str, Behaviour] = behaviour or {}
        #: Every attempt, in order, as `provider/model` labels. The failover tests
        #: assert on this: "beta served it" is weaker than "alpha was tried first,
        #: then beta", and only the sequence distinguishes them.
        self.calls: list[str] = []
        #: The payload of each attempt, in the same order as `calls`. Kept so a test
        #: can assert what actually left the gateway — that the concrete model
        #: replaced the alias, and that fields Prism does not understand survived.
        self.payloads: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    # -- scripting ---------------------------------------------------------

    def set(self, provider: str, **kwargs: Any) -> None:
        """Point-in-time failure injection, mirroring the mock's `POST /admin/config`."""
        self._behaviour[provider] = Behaviour(**kwargs)

    def reset(self) -> None:
        self._behaviour.clear()
        self.calls.clear()
        self.payloads.clear()

    def behaviour_for(self, provider: str) -> Behaviour:
        return self._behaviour.setdefault(provider, Behaviour())

    # -- the interface -----------------------------------------------------

    async def _attempt(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> Behaviour:
        """Record the attempt, apply the injected latency, and fail if scripted to.

        Shared by `complete` and `open_stream` so a provider scripted as `down` is
        down for both. A fake where `set(mode="down")` only affected one of the two
        would make the streaming failover tests pass for the wrong reason.
        """
        self.calls.append(target.label)
        self.payloads.append(payload)
        behaviour = self.behaviour_for(target.provider.name)

        if behaviour.latency_ms:
            await asyncio.sleep(behaviour.latency_ms / 1000)

        status = behaviour.next_status()
        if status == 0:
            raise ProviderCallFailed(
                "Timed out calling upstream: injected",
                target=target,
                retry_same=True,
                try_next=True,
            )
        if status is not None:
            retry_same, try_next = classify(status)
            raise ProviderCallFailed(
                f"Upstream returned {status}",
                target=target,
                status=status,
                retry_same=retry_same,
                try_next=try_next,
            )
        return behaviour

    async def complete(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> UpstreamCompletion:
        behaviour = await self._attempt(target, payload)

        messages = payload.get("messages") or []
        reply = build_reply(target.provider.name, target.model, messages)
        prompt_tokens = sum(count_tokens(str(m.get("content", ""))) for m in messages)
        completion_tokens = count_tokens(reply)
        body = {
            "id": f"chatcmpl-fake{next(self._ids):020d}",
            "object": "chat.completion",
            "created": 0,
            "model": target.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        # Read back through the same parser the HTTP adapter uses, so a change to
        # usage extraction cannot pass tests while breaking production.
        prompt_tokens, completion_tokens = read_usage(body)
        return UpstreamCompletion(
            target=target,
            body=body,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=behaviour.latency_ms,
        )

    async def open_stream(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> ProviderStream:
        behaviour = await self._attempt(target, payload)

        messages = payload.get("messages") or []
        reply = build_reply(target.provider.name, target.model, messages)
        prompt_tokens = sum(count_tokens(str(m.get("content", ""))) for m in messages)
        return FakeProviderStream(
            target=target,
            completion_id=f"chatcmpl-fake{next(self._ids):020d}",
            reply=reply,
            prompt_tokens=prompt_tokens,
            die_after_chunks=behaviour.die_after_chunks,
        )


class FakeProviderStream(ProviderStream):
    """The mock provider's chunk sequence, produced in-process.

    Same shape as `scripts/mock_provider.py:185`: an opening chunk carrying the role,
    one chunk per whitespace-separated word, then a final chunk with
    `finish_reason: "stop"` and the `usage` block. Matching that sequence matters
    because the gateway reads usage from the *last* chunk — a fake that attached usage
    to the first one would let a "we never captured usage" bug pass.
    """

    def __init__(
        self,
        *,
        target: ResolvedTarget,
        completion_id: str,
        reply: str,
        prompt_tokens: int,
        die_after_chunks: int = 0,
    ) -> None:
        self.target = target
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._completion_id = completion_id
        self._reply = reply
        self._final_prompt_tokens = prompt_tokens
        self._die_after_chunks = die_after_chunks
        #: The chunks handed to the caller, for tests that assert on pacing and shape.
        self.emitted: list[str] = []

    def _chunk(
        self,
        delta: dict[str, Any],
        *,
        finish_reason: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> str:
        data: dict[str, Any] = {
            "id": self._completion_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": self.target.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            data["usage"] = usage
        return json.dumps(data)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[str]:
        words = self._reply.split(" ")
        completion_tokens = count_tokens(self._reply)
        planned = [self._chunk({"role": "assistant", "content": ""})]
        planned += [self._chunk({"content": word + " "}) for word in words]
        planned.append(
            self._chunk(
                {},
                finish_reason="stop",
                usage={
                    "prompt_tokens": self._final_prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": self._final_prompt_tokens + completion_tokens,
                },
            )
        )

        for index, chunk in enumerate(planned):
            if self._die_after_chunks and index >= self._die_after_chunks:
                raise ProviderCallFailed(
                    "Upstream stream failed mid-response: injected",
                    target=self.target,
                    # Not retryable and not failover-worthy, for the reason in
                    # prism/providers/http.py: output has already been delivered.
                    retry_same=False,
                    try_next=False,
                )
            self.emitted.append(chunk)
            # Read usage back through the same parser production uses, so the fake
            # cannot report a number the real adapter would have missed.
            read_prompt, read_completion = read_usage(json.loads(chunk))
            if read_prompt or read_completion:
                self.prompt_tokens = read_prompt
                self.completion_tokens = read_completion
            yield chunk
