"""The semantic response cache.

A cache that answers "close enough" questions is the one feature in this gateway
that can be *wrong* in a way the caller cannot detect. A rate limiter that
over-admits shows up in a load test; a cache that serves the answer to a slightly
different question returns a plausible paragraph and nobody notices. So the whole
module is arranged around making a false hit hard, in this order:

    scope ─▶ exact hash ─▶ embed ─▶ threshold ─▶ literal guard ─▶ polarity guard

**The scope comes first and is not a filter, it is part of the key.** An entry
belongs to one tenant (`docs/DATA_MODEL.md:104`), one served tier, one conversation
prefix and one set of sampling parameters. Nothing else can ever be compared
against it, which is why `docs/PRISM_PROBLEM_STATEMENT.md:66` — "a cache hit that
returns another team's response is a data leak" — is enforced by the shape of the
key rather than by remembering to write a `WHERE` clause.

**The exact path exists to avoid the semantic path.** An identical prompt is one
indexed lookup: no embedding, no scan, no thresholds, no guards. That is the common
case in practice (a retried request, a page that polls) and it is also the cheapest
thing the gateway can possibly do. It is safe to skip the guards there precisely
*because* the text is identical — a byte-identical prompt has identical literals and
identical polarity by construction, and a volatile prompt was never stored (see
`is_volatile`).

**Similarity alone is not enough, and this is measured, not defensive.**
`docs/DESIGN_NOTES.md` records two pairs from this very embedding space: a negation
pair at **0.9620** and `"100 USD"` vs `"200 USD"` at **0.9149**. The first clears the
demoed threshold of 0.92 outright and the second clears the free tier's 0.85, while
both have completely different correct answers. So two guards run after the
threshold, and both are *rejection-only* — neither can promote a candidate that
similarity did not already accept. That
property is what makes them safe to tune and safe to add to: lowering the threshold
does not weaken them, and a guard bug can cost a cache hit but cannot invent one.

**Writes are guarded too, once, by volatility.** `data/sample_requests.jsonl`
carries `req_no_cache` — "What is the current status of the payments service?" — and
`docs/PRISM_PROBLEM_STATEMENT.md:67` asks for a decision about it. The decision is
that a time-sensitive prompt is served normally and **never stored**. Note that the
similarity threshold cannot protect this case: the danger is not a near-miss, it is
the *same* question asked twice, which the exact path would answer instantly with a
stale status.

**What this module stores that the rest of the gateway refuses to.**
`prism/audit.py` deliberately keeps prompts and responses out of `request_log`. A
cache cannot: matching needs the prompt and serving needs the response. The
mitigation is that caching is **opt-in per tenant** (`cache_enabled` in
`data/seed_keys.json`), so a team that does not want its traffic retained turns it
off and gets no entries at all — which is the behaviour two of the four seeded
tenants already have.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from prism.config import GatewayConfig, ResolvedTarget
from prism.db.models import CacheEntry, Tenant, utcnow
from prism.embeddings import Embedder, cosine
from prism.errors import NotFoundError
from prism.schemas import message_text

logger = logging.getLogger("prism.cache")

#: Ceiling on how many entries one lookup compares against. Similarity is computed
#: in Python over a per-tenant scan rather than in the database — see
#: `prism/db/models.py:EMBEDDING_DIM` for why pgvector was not adopted — so the scan
#: is the one part of a lookup whose cost grows with a tenant's history. 500 vectors
#: is well under a millisecond of dot products and keeps a lookup bounded no matter
#: how long a tenant has been running. Most-recent-first, so the entries a live
#: workload is actually asking about are the ones that stay in range; the cost of
#: that choice (an old entry can become unreachable rather than expiring) is in the
#: README's Known limitations.
MAX_CANDIDATES = 500

#: Values of `x-prism-cache` and of `request_log.cache`. The column is `String(8)`.
HIT = "hit"
MISS = "miss"

# ---------------------------------------------------------------------------
# Text normalisation and the exact-match key
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse whitespace and case, for hashing only.

    Two prompts differing by a trailing newline or a capital letter are the same
    question, and treating them as different would send an identical request
    upstream and store a second copy of the same answer. Case folding is safe *for
    the hash* because the embedding model is itself case-insensitive in practice —
    but the original text is what gets stored in `prompt_text`, because that is the
    prompt that was actually asked.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


def digest(text: str) -> str:
    """SHA-256 of the normalised text: the `prompt_hash` exact-match key.

    A hash rather than the text itself as the key, because `prompt_hash` is indexed
    and a prompt can be thousands of characters. SHA-256 rather than a fast
    non-cryptographic hash for one reason that matters here: a collision would serve
    one tenant's answer to a different question, and `hash()`-family functions are
    also process-salted, which would silently invalidate every stored entry on
    restart.
    """
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def split_conversation(messages: Sequence[Mapping[str, object]]) -> tuple[list[dict], str]:
    """Everything before the final user turn, and that turn's text.

    The split is the answer to "how do you cache a multi-turn conversation?", and
    the two obvious answers are both wrong. Keying on the last user message alone
    leaks context — "and what about Postgres?" means different things after
    different histories. Embedding the whole transcript instead makes every long
    conversation similar to every other long conversation, because the accumulated
    history dominates the vector.

    So the prefix is matched **exactly** (it goes into the scope digest, below) and
    only the final question is matched **semantically**. A follow-up can hit the
    cache, but only within an identical conversation so far, which is exactly the
    condition under which the earlier answer is still the right one.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return [dict(m) for m in messages[:index]], message_text(messages[index])
    # No user turn at all. Callers treat an empty question as uncacheable.
    return [dict(m) for m in messages], ""


def scope(
    messages: Sequence[Mapping[str, object]],
    *,
    served_as: str,
    params: Mapping[str, object] | None = None,
) -> str:
    """The `cache_key`: everything except the question that changes the answer.

    Three things are folded in:

    * **`served_as`** — the tier a router chose, or the alias/model the caller
      named. A `fast` answer is not a valid `smart` answer, so they cannot share a
      namespace. Using the *tier* rather than the requested alias is what lets an
      `auto` request that routed to `fast` reuse a direct `fast` answer, which is
      the same text produced by the same model and therefore genuinely the same
      thing.
    * **the conversation prefix** — see `split_conversation`.
    * **the sampling parameters** — `temperature`, `max_tokens`, `top_p`, `tools`,
      `response_format` and anything else the caller sent. A completion generated
      under `max_tokens: 10` is not an answer to the same request with
      `max_tokens: 1000`. Folding them into the key is better than refusing to
      cache parameterised requests: every request stays cacheable, just in its own
      namespace.

    The bare `served_as` is returned unchanged for the common single-turn,
    no-parameters request, so `cache_key` stays readable in the admin console and in
    a psql session. Only when there is something more to pin down does a digest get
    appended.
    """
    prefix, _ = split_conversation(messages)
    extras = dict(params or {})
    if not prefix and not extras:
        return served_as[:64]
    # sort_keys so two logically identical bodies cannot produce two namespaces
    # because a client serialised its JSON in a different order. `default=str` so an
    # exotic-but-legal value cannot turn a cacheable request into a 500.
    material = json.dumps({"prefix": prefix, "params": extras}, sort_keys=True, default=str)
    tail = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{served_as[:47]}#{tail}"


# ---------------------------------------------------------------------------
# Guard 1: literals
# ---------------------------------------------------------------------------

#: Numbers, including grouped and decimal forms. `100`, `1,000`, `3.14`, `2024`.
_NUMBER = re.compile(r"\d[\d,_]*(?:\.\d+)?")

#: Identifier-shaped tokens: anything containing both a digit and a letter, like
#: `INV-2024-001`, `2fa`, `sha256`, `v3`. The double lookahead is what keeps this
#: from matching every ordinary word.
_IDENTIFIER = re.compile(
    r"\b(?=[A-Za-z0-9._-]*\d)(?=[A-Za-z0-9._-]*[A-Za-z])[A-Za-z0-9._-]{2,}\b"
)

#: Quoted spans of **any** length. `prism/routing.py` drops quoted spans only when
#: they are long, because there it is discarding pasted payload; here a two-word
#: quotation is often the entire distinguishing content of the prompt ("what does
#: 'connection reset' mean" vs "what does 'connection refused' mean"), so length is
#: not the criterion.
_QUOTED = re.compile(
    r"```(.*?)```|\"([^\"]+)\"|“([^”]+)”|(?<![A-Za-z])'([^']+)'(?![A-Za-z])",
    re.DOTALL,
)

#: Month names. Dates are mostly caught as numbers, but "the March invoice" and
#: "the April invoice" differ only by a word, and their answers differ completely.
_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})

#: Currency codes and symbols. `_NUMBER` already separates "100 USD" from "200 USD";
#: this separates "100 USD" from "100 EUR", where the digits are identical.
_CURRENCY = frozenset({
    "usd", "eur", "gbp", "inr", "jpy", "cad", "aud", "chf", "cny", "brl", "sgd", "zar",
})

_WORD = re.compile(r"[a-z]+")


def literals(text: str) -> frozenset[tuple[str, int]]:
    """The exact values a paraphrase is not allowed to change.

    Returned as a frozen *multiset* — (value, count) pairs — not a set. "add 2 and
    3" against "add 2 and 2" have the same set of literals and different answers;
    counting catches it.

    The extracted classes deliberately overlap: `INV-2024-001` contributes both an
    identifier and its numbers. That is harmless, because the two sides of every
    comparison are computed by this same function, so an overlap either appears in
    both or in neither.

    The failure mode is asymmetric by design. A literal this misses is a possible
    false hit; a literal it invents is a lost hit. So it errs toward extracting
    more, and `literal_match` only ever rejects.
    """
    found: Counter[str] = Counter()
    lowered = text.casefold()
    found.update(_NUMBER.findall(lowered))
    found.update(token.casefold() for token in _IDENTIFIER.findall(text))
    for groups in _QUOTED.findall(text):
        for span in groups:
            if span:
                found[normalise(span)] += 1
    for word in _WORD.findall(lowered):
        if word in _MONTHS or word in _CURRENCY:
            found[word] += 1
    return frozenset(found.items())


def literal_match(query: frozenset[tuple[str, int]], candidate: str) -> bool:
    """True when the candidate carries exactly the query's literals."""
    return query == literals(candidate)


# ---------------------------------------------------------------------------
# Guard 2: polarity
# ---------------------------------------------------------------------------

#: Negation cues, apostrophes already stripped, so `don't` arrives as `dont`.
_NEGATIONS = frozenset({
    "not", "no", "never", "none", "nor", "neither", "cannot", "cant", "without",
    "nothing", "nobody", "nowhere", "isnt", "arent", "wasnt", "werent", "dont",
    "doesnt", "didnt", "wont", "wouldnt", "couldnt", "shouldnt", "havent", "hasnt",
    "hadnt", "aint",
})

_APOSTROPHE = re.compile(r"['’]")


def negated(text: str) -> bool:
    """Whether the prompt carries a negation, as a boolean rather than a count.

    A boolean and not a count, because counting is brittle in the wrong direction:
    "cannot" and "can not" are one cue or two depending on the tokenizer, and a
    mismatch there would reject a legitimate paraphrase. Whether a negation is
    present at all is stable across those variations, and it is the property that
    matters — `docs/DESIGN_NOTES.md` measures a negation pair at 0.9620, which
    clears the demoed threshold outright, and the two members of that pair differ
    precisely in whether one word is present.
    """
    words = _WORD.findall(_APOSTROPHE.sub("", text.casefold()))
    return any(word in _NEGATIONS for word in words)


# ---------------------------------------------------------------------------
# The write-path guard: volatility
# ---------------------------------------------------------------------------

#: Words that make an answer perishable.
_VOLATILE_WORDS = frozenset({
    "current", "currently", "today", "tonight", "tomorrow", "yesterday",
    "latest", "live", "newest", "ongoing", "upcoming", "outstanding",
})

#: Phrases, for cues whose individual words are too common to blacklist. Bare "now"
#: is deliberately absent: "how do I do X now?" is a perfectly cacheable question,
#: and the words below are the forms that actually mean "as of this moment".
_VOLATILE_PHRASES = (
    "right now",
    "just now",
    "at the moment",
    "as of now",
    "as of today",
    "so far",
    "up to date",
    "this week",
    "this month",
    "this quarter",
    "this year",
    "status of",
    "how many are there now",
)


def is_volatile(text: str) -> bool:
    """Whether this prompt's answer goes stale, and so must never be stored.

    A lexical rule, in a project that argued against lexical rules for the
    difficulty router (`prism/routing.py`). The difference is real: difficulty is a
    semantic property, so keywords are the wrong instrument for it, whereas
    "current", "today" and "latest" *are* the signal here — an embedding places "what
    is the status" and "what was the status" close together, which is the opposite of
    what this needs.

    Like the read-path guards, this can only ever say no. A false positive costs one
    cache entry; a false negative serves a stale answer. The list is therefore short
    and confident rather than exhaustive, and the prompts it cannot catch (a
    time-sensitive question phrased with no time words at all) are recorded in the
    README's Known limitations.
    """
    lowered = normalise(text)
    if any(phrase in lowered for phrase in _VOLATILE_PHRASES):
        return True
    return any(word in _VOLATILE_WORDS for word in _WORD.findall(lowered))


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheHit:
    """An entry that may be served, and enough about it to log and header it."""

    entry_id: int
    body: dict
    similarity: float
    #: `exact` or `semantic`. Reported in the log line, not in the header: the
    #: header contract (`docs/API_CONTRACT.md:70`) is `hit` or `miss` and clients
    #: branch on it, so it does not grow a third value.
    kind: str
    served_provider: str | None
    served_model: str | None
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Lookup:
    """The outcome of one cache lookup."""

    hit: CacheHit | None = None
    #: Why, in a form safe to log: never any prompt text. Same rule and the same
    #: reason as `prism/routing.py`'s `route_reason` — an operator needs to know
    #: whether the threshold or a guard decided this, without the journal
    #: accumulating a second copy of every prompt.
    reason: str = ""
    #: The question's vector, when the semantic path computed one. Handed back so
    #: `store` does not have to embed the same string again. The per-request
    #: `MemoEmbedder` would make a second call free, but depending on a memo for
    #: correctness is a different thing from depending on it for speed.
    embedding: list[float] | None = None

    @property
    def state(self) -> str:
        return HIT if self.hit else MISS


def _fresh(now: dt.datetime):
    """The not-expired predicate, as a SQL expression.

    `expires_at IS NULL` means "no TTL configured", which is the default and must
    stay serveable — a bare `expires_at > now` would make every entry written
    without a TTL invisible.
    """
    return (CacheEntry.expires_at.is_(None)) | (CacheEntry.expires_at > now)


async def lookup(
    session: AsyncSession,
    tenant: Tenant,
    *,
    cache_key: str,
    question: str,
    embedder: Embedder,
    now: dt.datetime | None = None,
) -> Lookup:
    """Find a servable entry for this question, or explain why there is none."""
    now = now or utcnow()

    if not tenant.cache_enabled:
        return Lookup(reason="disabled")
    if not question.strip():
        return Lookup(reason="empty-prompt")

    threshold = tenant.cache_similarity_threshold
    if threshold is None:
        # `prism/db/models.py:151` leaves the column nullable so a misconfigured
        # tenant is visible rather than papered over by a default. Honouring that
        # here means declining to invent one: a threshold picked by the gateway
        # would be a number nobody chose, guarding a data-leak boundary.
        logger.warning(
            "tenant %s has the cache enabled with no similarity_threshold; "
            "serving every request as a miss until one is configured",
            tenant.team,
        )
        return Lookup(reason="no-threshold")
    limit = float(threshold)

    exact = (
        await session.execute(
            select(CacheEntry).where(
                CacheEntry.tenant_id == tenant.id,
                CacheEntry.cache_key == cache_key,
                CacheEntry.prompt_hash == digest(question),
                _fresh(now),
            )
        )
    ).scalar_one_or_none()
    if exact is not None:
        # No embedding was computed and none is needed: nothing downstream stores an
        # entry after a hit.
        return Lookup(hit=_hit(exact, similarity=1.0, kind="exact"), reason="exact")

    vector = await embedder.embed_one(question)
    candidates = (
        (
            await session.execute(
                select(CacheEntry)
                .where(
                    CacheEntry.tenant_id == tenant.id,
                    CacheEntry.cache_key == cache_key,
                    _fresh(now),
                )
                .order_by(CacheEntry.created_at.desc())
                .limit(MAX_CANDIDATES)
            )
        )
        .scalars()
        .all()
    )

    query_literals = literals(question)
    query_negated = negated(question)
    best: CacheEntry | None = None
    best_similarity = 0.0
    highest = 0.0  # highest similarity seen at all, guards ignored — for the reason
    rejected: Counter[str] = Counter()

    for entry in candidates:
        similarity = cosine(vector, entry.embedding)
        highest = max(highest, similarity)
        if similarity < limit:
            continue
        if not literal_match(query_literals, entry.prompt_text):
            rejected["literal"] += 1
            continue
        if negated(entry.prompt_text) != query_negated:
            rejected["polarity"] += 1
            continue
        if similarity > best_similarity:
            best, best_similarity = entry, similarity

    detail = (
        f"scanned={len(candidates)} best={highest:.4f} threshold={limit:.4f}"
        + ("".join(f" rejected={name}:{count}" for name, count in sorted(rejected.items())))
    )
    if best is not None:
        return Lookup(
            hit=_hit(best, similarity=best_similarity, kind="semantic"),
            reason=f"semantic sim={best_similarity:.4f} {detail}",
            embedding=vector,
        )
    return Lookup(reason=f"no-match {detail}", embedding=vector)


def _hit(entry: CacheEntry, *, similarity: float, kind: str) -> CacheHit:
    return CacheHit(
        entry_id=entry.id,
        body=entry.response_body,
        similarity=similarity,
        kind=kind,
        served_provider=entry.served_provider,
        served_model=entry.served_model,
        prompt_tokens=entry.prompt_tokens,
        completion_tokens=entry.completion_tokens,
    )


async def record_hit(
    session: AsyncSession, entry_id: int, *, now: dt.datetime | None = None
) -> None:
    """Count the hit, atomically.

    `hit_count = hit_count + 1` in the statement rather than read-modify-write in
    Python, for the same reason `prism/budget.py` increments spend that way: two
    concurrent hits on the same entry would otherwise both read the same value and
    one increment would vanish. Here the consequence is only a wrong number in
    `/admin/cache/stats`, but a hit rate nobody can trust is not worth reporting.
    """
    await session.execute(
        update(CacheEntry)
        .where(CacheEntry.id == entry_id)
        .values(hit_count=CacheEntry.hit_count + 1, last_hit_at=now or utcnow())
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def is_cacheable_response(body: object) -> bool:
    """Whether a response body is a successful completion worth keeping.

    `docs/PRISM_PROBLEM_STATEMENT.md:67` — "do not cache error responses". This is
    the belt to that braces: the only caller reaches this line after a 200, but a
    provider that answers 200 with an `error` object exists, and an error cached
    once is an error served forever.
    """
    if not isinstance(body, dict) or "error" in body:
        return False
    choices = body.get("choices")
    return isinstance(choices, list) and bool(choices)


async def store(
    session: AsyncSession,
    tenant: Tenant,
    *,
    cache_key: str,
    question: str,
    embedding: Sequence[float],
    body: dict,
    target: ResolvedTarget | None,
    prompt_tokens: int,
    completion_tokens: int,
    ttl_seconds: int | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Remember this answer. Returns whether an entry was written.

    Nothing here raises on a duplicate: `ON CONFLICT DO NOTHING` against
    `uq_cache_exact` handles two identical prompts arriving concurrently, which is
    a normal thing for a retrying client to do and not a reason to fail a request
    that has already been answered. The first writer wins and the second is a no-op.
    """
    if not tenant.cache_enabled:
        return False
    if not question.strip() or not is_cacheable_response(body):
        return False
    if is_volatile(question):
        # Logged, because "why did this never cache?" is otherwise unanswerable
        # from the outside — the request looks like an ordinary miss every time.
        logger.info(
            "not caching a time-sensitive prompt for tenant %s (%d words)",
            tenant.team,
            len(question.split()),
        )
        return False

    now = now or utcnow()
    expires_at = (
        now + dt.timedelta(seconds=ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
    )

    await session.execute(
        insert(CacheEntry)
        .values(
            tenant_id=tenant.id,
            cache_key=cache_key,
            prompt_hash=digest(question),
            prompt_text=question,
            embedding=list(embedding),
            response_body=body,
            served_provider=target.provider.name if target else None,
            served_model=target.model if target else None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            hit_count=0,
            created_at=now,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(constraint="uq_cache_exact")
    )
    return True


async def purge_expired(
    session: AsyncSession, tenant_id: int, *, now: dt.datetime | None = None
) -> int:
    """Delete this tenant's expired entries. Returns how many went.

    Eviction policy, stated plainly because `docs/DATA_MODEL.md:129` asks for it:
    **entries are evicted only by TTL, and only opportunistically.** There is no
    background sweeper and no size cap. Expired rows are already unservable — every
    query filters them — so this reclaims space rather than affecting correctness,
    and running it on the write path keeps it off the read path where latency
    matters. With no TTL configured the cache grows without bound per tenant, which
    is in the README's Known limitations.
    """
    result = await session.execute(
        delete(CacheEntry).where(
            CacheEntry.tenant_id == tenant_id,
            CacheEntry.expires_at.is_not(None),
            CacheEntry.expires_at <= (now or utcnow()),
        )
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Serving a hit
# ---------------------------------------------------------------------------


def served_target(
    config: GatewayConfig, hit: CacheHit, fallback: ResolvedTarget
) -> ResolvedTarget:
    """The provider/model to report for a cache hit.

    The stored origin, not the chain this request resolved to. `x-prism-provider` on
    a hit should name where the bytes actually came from — the answer really was
    produced by that provider, on an earlier request — and reporting the target that
    was *not* called would put a provider in the audit trail that served nothing.

    Falls back when the stored provider is no longer in the config, which is a real
    situation: an entry outlives a config change.
    """
    if not hit.served_provider or not hit.served_model:
        return fallback
    try:
        return ResolvedTarget(config.provider(hit.served_provider), hit.served_model)
    except NotFoundError:
        return fallback


def replay_chunks(body: Mapping[str, object]) -> Iterator[str]:
    """A cached completion, re-emitted as OpenAI streaming chunk payloads.

    A streaming request could simply be treated as uncacheable, and that would be
    less code. It would also mean the cache silently stops working for any client
    that streams — which is most of them — and the gateway's most expensive feature
    would quietly not apply to its most common request shape.

    The chunk shape mirrors `scripts/mock_provider.py:185` exactly: an opening
    `{"role": "assistant"}` delta, one delta per word, then an empty delta carrying
    `finish_reason` and `usage`. Same wire shape as a live stream, so a client cannot
    tell a replay from a first-time answer except by the header — which is the point
    of the header.

    No artificial pacing. A live stream is slow because the model is thinking; a
    replay has nothing to think about, and inserting a sleep to *look* like one would
    be dressing up a fast path as a slow one.
    """
    choices = body.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message", {}) if isinstance(first, dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    text = content if isinstance(content, str) else ""

    completion_id = str(body.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}")
    model = str(body.get("model") or "")
    created = body.get("created")
    created = int(created) if isinstance(created, int | float) else int(time.time())

    def chunk(delta: dict, *, finish_reason: str | None = None, usage: object = None) -> str:
        data: dict[str, object] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            data["usage"] = usage
        return json.dumps(data)

    yield chunk({"role": "assistant", "content": ""})
    for word in text.split(" "):
        if word:
            yield chunk({"content": word + " "})
    yield chunk({}, finish_reason="stop", usage=body.get("usage"))


__all__ = [
    "HIT",
    "MAX_CANDIDATES",
    "MISS",
    "CacheHit",
    "Lookup",
    "digest",
    "is_cacheable_response",
    "is_volatile",
    "literal_match",
    "literals",
    "lookup",
    "negated",
    "normalise",
    "purge_expired",
    "record_hit",
    "replay_chunks",
    "scope",
    "served_target",
    "split_conversation",
    "store",
]
