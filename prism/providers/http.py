"""The real adapter: one HTTP call to one upstream.

Three things here are load-bearing rather than incidental.

**One shared `AsyncClient` for the process, not one per request.** A fresh client
per request means a fresh TCP connection and, against a TLS provider, a fresh
handshake — tens of milliseconds added to every call, and a socket left in
`TIME_WAIT` behind it. Under the burst `scripts/load_test.py` fires, per-request
clients exhaust ephemeral ports long before they exhaust the rate limiter, and the
test then reports connection errors that look like a gateway bug. The client is
created at startup and disposed in the lifespan.

**Separate connect and read timeouts.** They fail for different reasons and want
different numbers: a provider that is *down* refuses or drops the connection and
should be abandoned in a second or two so the next provider in the chain gets a
turn, while a provider that is *thinking* legitimately holds the connection open
for much longer. A single timeout has to be set to the larger of the two, which
means a dead provider consumes the whole generation budget before failover starts.

**A read timeout is not retried against the same provider.** Splitting the timeouts
above bounds what *one* attempt costs; it does nothing about how many attempts get
made. Retrying a stalled endpoint multiplies the read budget by `max_attempts`
before the chain moves on, which measured out at 90.5 s against a 30 s timeout.
`_timeout_retry_same` is where that is decided, and it explains itself.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from prism.config import ResolvedTarget
from prism.providers.base import (
    DONE_SENTINEL,
    ProviderCallFailed,
    ProviderClient,
    ProviderStream,
    UpstreamCompletion,
    classify,
    parse_sse_data,
    read_usage,
)

CHAT_COMPLETIONS_PATH = "/chat/completions"


def _timeout_retry_same(exc: httpx.TimeoutException) -> bool:
    """Is this timeout worth another attempt against the *same* target?

    Only a connect timeout is. The intuitive rule is that every timeout is
    retryable — a timeout says nothing about whether the request was valid — but
    that rule ignores what the failed attempt already cost. A read timeout has, by
    definition, just spent the entire read budget; retrying it against the same
    endpoint spends the whole budget again, and failover does not even begin until
    `max_attempts` of them have elapsed. Measured on this build with a provider
    stalled past the 30 s read timeout and `max_attempts: 3`: the client waited
    **90.5 s** for an answer the fallback could have given in 30. That is the hang
    `docs/EVALUATION_GUIDE.md:91` asks about, and the fix belongs here — in the
    classification — rather than in `prism/dispatch.py`, which by design never
    reasons about *why* an attempt failed.

    A `ConnectTimeout` keeps its retry because it is cheap and different in kind: it
    costs `connect_timeout_seconds`, not the read budget, and a handshake that did
    not complete is the transient blip backoff exists for. A `PoolTimeout` does not,
    because this client's pool timeout is the read budget, so waiting it out again
    has the same cost as a read timeout with none of the reason to hope.

    Failover still happens on every timeout — `try_next` stays True. What changes is
    that the caller's wait is now bounded by the depth of the chain rather than by
    depth times attempts.
    """
    return isinstance(exc, httpx.ConnectTimeout)


class HttpProviderClient(ProviderClient):
    """Talks OpenAI over HTTP to whatever `base_url` the config names."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        connect_timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=connect_timeout_seconds,
                # Reading is where a slow generation shows up, so it gets the
                # generous budget; writing a small JSON body never needs one.
                read=timeout_seconds,
                write=connect_timeout_seconds,
            ),
            # Enough keep-alive connections to cover the configured chain depth
            # under the load test's concurrency without re-handshaking.
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
        )

    def _url(self, target: ResolvedTarget) -> str:
        return target.provider.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH

    def _headers(self, target: ResolvedTarget) -> dict[str, str]:
        """Build upstream headers.

        The caller's `Authorization` header is **not** forwarded. Provider
        credentials come from the gateway's own config, and the whole premise of a
        virtual key (`docs/API_CONTRACT.md:9`) is that a tenant never holds or
        sees one. Forwarding the caller's header would also make an upstream 401
        depend on the tenant's key rather than on ours, which is a confusing bug
        to chase.
        """
        return {
            "Authorization": f"Bearer {target.provider.api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> UpstreamCompletion:
        started = time.monotonic()
        try:
            response = await self._client.post(
                self._url(target), json=payload, headers=self._headers(target)
            )
        except httpx.TimeoutException as exc:
            # Always failover-worthy — a timeout says nothing about whether the
            # request was valid. Retryable against this same target only if it was
            # cheap; see `_timeout_retry_same`.
            raise ProviderCallFailed(
                f"Timed out calling upstream: {type(exc).__name__}",
                target=target,
                retry_same=_timeout_retry_same(exc),
                try_next=True,
            ) from exc
        except httpx.HTTPError as exc:
            # Connect errors, DNS failures, protocol errors, dropped connections.
            raise ProviderCallFailed(
                f"Transport error calling upstream: {type(exc).__name__}",
                target=target,
                retry_same=True,
                try_next=True,
            ) from exc

        latency_ms = round((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            retry_same, try_next = classify(response.status_code)
            raise ProviderCallFailed(
                f"Upstream returned {response.status_code}",
                target=target,
                status=response.status_code,
                retry_same=retry_same,
                try_next=try_next,
            )

        try:
            body = response.json()
        except ValueError as exc:
            # A 200 whose body is not JSON is a broken upstream, not a broken
            # request — worth another attempt and worth another provider.
            raise ProviderCallFailed(
                "Upstream returned a non-JSON body",
                target=target,
                status=response.status_code,
                retry_same=True,
                try_next=True,
            ) from exc

        if not isinstance(body, dict) or not body.get("choices"):
            raise ProviderCallFailed(
                "Upstream body has no choices",
                target=target,
                status=response.status_code,
                retry_same=True,
                try_next=True,
            )

        prompt_tokens, completion_tokens = read_usage(body)
        return UpstreamCompletion(
            target=target,
            body=body,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )

    async def open_stream(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> ProviderStream:
        """Send the request and return once the upstream has accepted it.

        `httpx.AsyncClient.stream` is an async context manager, and it is entered
        **manually** here rather than with `async with`, because the response has to
        outlive this function — the whole point is that the caller iterates it. The
        matching `__aexit__` lives in `HttpProviderStream.aclose`, which the route
        calls from a `finally`, so a client that disconnects halfway still releases
        the upstream connection instead of leaking it for the rest of the process.
        """
        # `stream: True` is set here as well as by the caller. It is a transport-level
        # invariant of this method — an upstream that answers with a single JSON body
        # would make `aiter_lines` produce one unparseable line — so it is asserted
        # where it is required rather than trusted from above.
        request = self._client.build_request(
            "POST",
            self._url(target),
            json={**payload, "stream": True},
            headers=self._headers(target),
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            # Same rule as `complete`. It matters more here, not less: a streaming
            # client is watching a blank screen for the whole wait.
            raise ProviderCallFailed(
                f"Timed out opening upstream stream: {type(exc).__name__}",
                target=target,
                retry_same=_timeout_retry_same(exc),
                try_next=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderCallFailed(
                f"Transport error opening upstream stream: {type(exc).__name__}",
                target=target,
                retry_same=True,
                try_next=True,
            ) from exc

        if response.status_code >= 400:
            # The body has not been read yet, so it must be drained before the
            # connection can go back to the pool — and it is drained and *discarded*,
            # never quoted, for the reason in `ProviderCallFailed`'s docstring.
            await response.aread()
            await response.aclose()
            retry_same, try_next = classify(response.status_code)
            raise ProviderCallFailed(
                f"Upstream returned {response.status_code}",
                target=target,
                status=response.status_code,
                retry_same=retry_same,
                try_next=try_next,
            )

        return HttpProviderStream(target=target, response=response)

    async def aclose(self) -> None:
        # Only close what we created: an injected client belongs to its owner, and
        # closing it here would break a caller that reuses it across two adapters.
        if self._owns_client:
            await self._client.aclose()


class HttpProviderStream(ProviderStream):
    """An open SSE response, read line by line and never buffered whole.

    `docs/API_CONTRACT.md:85` requires chunks to be forwarded as they arrive. That is
    not a performance preference: the demo criterion in
    `docs/IMPLEMENTATION_GUIDE.md:96` is that a streaming request *visibly* arrives
    token by token, and any accumulate-then-emit step makes a correct-looking response
    that fails the only test a human runs.
    """

    def __init__(self, *, target: ResolvedTarget, response: httpx.Response) -> None:
        self.target = target
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._response = response
        self._closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[str]:
        try:
            async for line in self._response.aiter_lines():
                data = parse_sse_data(line)
                if data is None or not data:
                    continue
                if data == DONE_SENTINEL:
                    # Swallowed: the gateway emits its own terminator, so a stream
                    # that ends without one still terminates cleanly at the client.
                    break
                self._capture_usage(data)
                yield data
        except httpx.TimeoutException as exc:
            raise ProviderCallFailed(
                f"Upstream stream timed out mid-response: {type(exc).__name__}",
                target=self.target,
                # Both false: bytes have already reached the client, and
                # docs/IMPLEMENTATION_GUIDE.md:172 forbids splicing another
                # provider's output onto a partial answer. The flags say so
                # explicitly rather than relying on the caller not to try.
                retry_same=False,
                try_next=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderCallFailed(
                f"Upstream stream failed mid-response: {type(exc).__name__}",
                target=self.target,
                retry_same=False,
                try_next=False,
            ) from exc

    def _capture_usage(self, data: str) -> None:
        """Read `usage` off a chunk if it carries one, ignoring anything unparseable.

        A chunk that does not parse is still forwarded — it is the provider's output
        and the client may understand a dialect this gateway does not. What it cannot
        do is break the accounting, so the parse failure is contained here.
        """
        try:
            chunk = json.loads(data)
        except ValueError:
            return
        prompt_tokens, completion_tokens = read_usage(chunk)
        # Last writer wins: OpenAI sends usage once, in the final chunk, but a
        # provider that sends running totals should leave the final one in place.
        if prompt_tokens or completion_tokens:
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._response.aclose()
