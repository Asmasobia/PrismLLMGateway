"""The provider boundary: one interface, two implementations.

`docs/IMPLEMENTATION_GUIDE.md` requires provider integration to sit behind an
adapter so tests can point at the mock providers *or* at an in-process fake. The
reason to want that is not testability in the abstract — it is that every
interesting failure in this system is an upstream failure (a 503, a 429, a
timeout, a provider that dies mid-stream), and those are the failures you cannot
provoke reliably over a real socket. Behind this interface a test asks for them
directly.

**The error taxonomy is the point of this module.** A single `ProviderCallFailed`
with a message would force the retry logic to re-parse strings to decide what to
do next, so the two decisions the dispatcher has to make are answered here, by
the code that actually saw the response:

* `retry_same` — is this worth trying again *on the same provider*? Only for
  transient conditions: 5xx, 429, 408, and transport-level errors.
* `try_next` — is this worth trying *the next provider in the chain*? Almost
  always, with one exception that matters: a request the upstream rejected as
  malformed (400) will be rejected identically by every other provider, so
  failing it over burns latency and money to reach the same answer.

Those are genuinely independent. A 401 from an upstream means *our* stored
credential for that provider is wrong: retrying it is pointless, and failing over
to a provider with a working key is exactly right. A 429 is the opposite —
retrying after a backoff is reasonable, and so is moving on.

**Streaming is split into "open" and "iterate", and that split is a policy
decision, not an implementation detail.** `open_stream` performs the request and
returns only once the upstream has accepted it — status line and headers received,
no bytes forwarded to the client yet. So a failure raised by `open_stream` is
indistinguishable from a non-streaming failure and can be retried or failed over
freely. A failure raised while *iterating* cannot: the client already holds part of
an answer, and `docs/IMPLEMENTATION_GUIDE.md:172` forbids restarting on another
provider and splicing the outputs. Making that the difference between two methods
means the rule is enforced by the shape of the interface rather than by a comment
somebody has to remember.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from prism.config import ResolvedTarget


class ProviderCallFailed(Exception):
    """One upstream attempt failed.

    Never client-visible: `prism/errors.py` converts an exhausted chain into a
    502 with a generic message. This exception may carry an upstream body, and an
    upstream body may echo the request — or, on a misconfigured provider, the
    credential we sent it. `docs/DATA_MODEL.md:44` forbids either reaching a
    client, so nothing here is ever rendered into a response.
    """

    def __init__(
        self,
        message: str,
        *,
        target: ResolvedTarget | None = None,
        status: int | None = None,
        retry_same: bool = True,
        try_next: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.target = target
        self.status = status
        self.retry_same = retry_same
        self.try_next = try_next

    def __str__(self) -> str:
        where = f" [{self.target.label}]" if self.target else ""
        code = f" status={self.status}" if self.status is not None else ""
        return f"{self.message}{where}{code}"


def classify(status: int) -> tuple[bool, bool]:
    """Map an upstream HTTP status to `(retry_same, try_next)`.

    Kept as a function, and tested directly, because this table *is* the failover
    policy — an `if status >= 500` sprinkled through the dispatcher would make the
    policy an emergent property of the code rather than a stated one.
    """
    if status in (408, 429) or status >= 500:
        return True, True
    if status == 400:
        # The caller's own request is wrong. Every provider will agree.
        return False, False
    # 401/403/404 and friends: this provider cannot serve it, another may.
    return False, True


@dataclass(frozen=True)
class UpstreamCompletion:
    """A successful non-streaming completion.

    `body` is the upstream response **verbatim**. It is not rebuilt from parsed
    fields, because the data plane has to stay OpenAI-compatible
    (`docs/API_CONTRACT.md:5`) and a client may read fields this gateway has
    never heard of — `system_fingerprint`, `logprobs`, a provider extension.
    Reconstructing the body would quietly drop them. Prism reads what it needs
    for accounting and forwards the rest untouched.
    """

    target: ResolvedTarget
    body: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


def read_usage(body: Any) -> tuple[int, int]:
    """Pull `(prompt_tokens, completion_tokens)` out of an OpenAI-shaped body.

    Missing or malformed usage yields `(0, 0)` rather than an exception. The
    trade-off is deliberate and recorded in the README's Known limitations: a
    provider that omits `usage` becomes un-billable rather than un-servable. The
    alternative — estimating tokens ourselves — would put a number into the
    accounting that no provider ever agreed to, which is worse than a visible
    zero when the whole point of the metering is that it reconciles.
    """
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return 0, 0

    def _count(key: str) -> int:
        value = usage.get(key, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    return _count("prompt_tokens"), _count("completion_tokens")


#: The SSE sentinel that ends an OpenAI-compatible stream. Recognised on the way in
#: and re-emitted on the way out, rather than forwarded, because the gateway decides
#: when *its* stream is over — an upstream that dies without sending it must not
#: leave the client's stream unterminated.
DONE_SENTINEL = "[DONE]"


def parse_sse_data(line: str) -> str | None:
    """Extract the payload of one `data:` line, or None if the line is not one.

    SSE permits comment lines (`:` prefixed), blank separator lines, and other
    fields (`event:`, `id:`, `retry:`). None of those carry chunks, and treating a
    blank line as an empty chunk would emit `data: \\n\\n` at the client for every
    separator the upstream sent — doubling the traffic and confusing parsers that
    count events.
    """
    if not line.startswith("data:"):
        return None
    return line[len("data:") :].strip()


class ProviderStream(abc.ABC):
    """An open upstream stream, plus the usage it reports at the end.

    Iterating yields the **raw JSON text** of each chunk, not a parsed object. The
    reason is the same one that makes `UpstreamCompletion.body` verbatim: a chunk may
    carry fields Prism has never heard of, and a parse-then-re-serialise round trip
    would silently drop them, reorder keys, and change how floats render. Forwarding
    the text the upstream sent is the only way to be certain the client receives what
    the provider produced.

    `prompt_tokens` and `completion_tokens` are zero until the stream has been read to
    the end, because that is when the upstream reports them
    (`scripts/mock_provider.py:203` attaches `usage` to the final chunk). Reading them
    early is not an error — it is simply zero — so the caller must only trust them
    after exhaustion. `docs/API_CONTRACT.md:88` requires the final cost to reach the
    request log, which is why they are attributes on a live object rather than a
    return value the generator protocol would throw away.
    """

    target: ResolvedTarget
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @abc.abstractmethod
    def __aiter__(self) -> AsyncIterator[str]:
        """Yield each chunk's raw JSON text, in order, without the `data:` prefix."""

    # Deliberately concrete and empty, not abstract: a stream backed by an in-process
    # list has nothing to release, and forcing it to write `pass` would be ceremony.
    async def aclose(self) -> None:  # noqa: B027
        """Release the connection. Safe to call more than once, and after exhaustion."""


class ProviderClient(abc.ABC):
    """What the gateway needs from an upstream, and nothing more.

    Note what is *absent*: retries, backoff, failover, chain resolution. An
    adapter performs exactly one attempt against exactly one target and reports
    what happened. Retry policy lives in the dispatcher, one level up, so it is
    written once and applies identically to a real provider and to a fake.
    """

    @abc.abstractmethod
    async def complete(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> UpstreamCompletion:
        """Perform one non-streaming completion, or raise `ProviderCallFailed`."""

    @abc.abstractmethod
    async def open_stream(
        self, target: ResolvedTarget, payload: dict[str, Any]
    ) -> ProviderStream:
        """Start one streaming completion, or raise `ProviderCallFailed`.

        Returns once the upstream has accepted the request and nothing has been
        forwarded to the client yet, so a failure here is still safe to retry or fail
        over. See the module docstring for why that boundary is a separate method.
        """

    # Empty by design, as above: `FakeProviderClient` holds no transport.
    async def aclose(self) -> None:  # noqa: B027
        """Release transport resources. Safe to call more than once."""


__all__ = [
    "DONE_SENTINEL",
    "ProviderCallFailed",
    "ProviderClient",
    "ProviderStream",
    "UpstreamCompletion",
    "classify",
    "parse_sse_data",
    "read_usage",
]
