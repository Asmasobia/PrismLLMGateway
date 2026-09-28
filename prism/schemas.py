"""Wire shapes for the data plane.

The governing constraint is `docs/API_CONTRACT.md:5`: `POST /v1/chat/completions`
must stay OpenAI-compatible. That makes strict validation the wrong instinct here.
Callers send `temperature`, `max_tokens`, `top_p`, `tools`, `response_format`,
`seed`, and whatever the provider added last month; a schema that rejected unknown
fields would break every one of those, and a schema that silently dropped them
would be worse — the caller's `temperature=0` would vanish and the gateway would
look non-deterministic for no visible reason.

So `extra="allow"` throughout, and the payload sent upstream is the caller's own
object with exactly one field rewritten: `model`, from the alias they asked for to
the concrete model that will serve it. Validation is limited to the two fields the
gateway itself has to understand — `model`, because routing and the allowlist need
it, and `messages`, because the classifier and the cache key are computed from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def message_text(message: Mapping[str, Any]) -> str:
    """The text of one message, whatever shape its `content` arrived in.

    Multimodal content is a list of typed parts rather than a string, so the text
    parts are joined and image/audio parts ignored — a router and a cache key can
    only work with words. `None` content (an assistant turn carrying only tool
    calls) flattens to the empty string.

    This lives in the wire-shape module because two callers need it and neither
    should own it: `prism/routing.py` picks the last user turn to classify, and
    `prism/cache.py` picks the same turn to embed plus every earlier turn to key on.
    A second copy of this flattening in either place is a way for the router and the
    cache to slowly disagree about what a prompt says.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


class Message(BaseModel):
    """One chat message.

    `content` is `Any`, not `str`: the OpenAI schema allows a list of typed parts
    for multimodal input, and `None` for an assistant turn that only carries tool
    calls. Narrowing it to `str` here would reject valid requests the upstream
    would have accepted.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    """The subset of the OpenAI request body Prism reads. Everything else passes through."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    #: At least one message. An empty list would reach the provider as a 400 after
    #: a round trip, a retry, and a failover to a second provider that says the same
    #: thing — so it is refused here, for free.
    messages: list[Message] = Field(min_length=1)
    stream: bool = False

    def message_dicts(self) -> list[dict[str, Any]]:
        """Messages as plain dicts, for the classifier and the cache key."""
        return [message.model_dump(exclude_unset=True) for message in self.messages]

    def shaping_params(self) -> dict[str, Any]:
        """Everything the caller sent that changes the *shape* of the answer.

        That is: the whole body minus `model` (which the cache key carries
        separately, as the tier that served it), `messages` (the question itself),
        and `stream` (a transport choice — the same completion streamed and unstreamed
        is the same completion, which is what makes replaying a cached answer as SSE
        legitimate).

        What is left is `temperature`, `max_tokens`, `top_p`, `tools`,
        `response_format`, `seed`, and any field a provider added last month. The
        cache folds them into its key rather than refusing to cache when they are
        present: a `max_tokens: 10` answer is not a valid answer to the same question
        asked with no limit, but it is a perfectly good answer to that question asked
        the same way again.

        `exclude_unset=True` for the reason `upstream_payload` gives: a field the
        caller never mentioned must not appear here, or two identical requests would
        land in different namespaces depending on which defaults pydantic filled in.
        """
        payload = self.model_dump(exclude_unset=True)
        for field in ("model", "messages", "stream"):
            payload.pop(field, None)
        return payload

    def upstream_payload(
        self, resolved_model: str, *, stream: bool | None = None
    ) -> dict[str, Any]:
        """The body to send upstream: the caller's own, with `model` resolved.

        `exclude_unset=True` matters. Dumping the whole model would add
        `stream: false` to a request that never mentioned streaming and
        `content: null` to messages that never carried it — harmless against the
        provided mocks, and exactly the kind of injected default that makes a real
        provider behave differently through the gateway than it does directly.
        """
        payload = self.model_dump(exclude_unset=True)
        payload["model"] = resolved_model
        if stream is None:
            payload.pop("stream", None)
        else:
            payload["stream"] = stream
        return payload


__all__ = ["ChatCompletionRequest", "Message", "message_text"]
