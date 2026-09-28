"""Turning what the caller asked for into an ordered list of things to try.

One function answers the question for all three kinds of request, and that is the
design:

* a **concrete model** (`alpha-small`) — a chain of one;
* a **chain alias** (`fast`) — the primary followed by its fallbacks, in order;
* a **router alias** (`auto`) — classify the prompt, pick a tier, then resolve
  *that tier through the same code path*.

The third case is why resolution returns a chain rather than a target. If `auto`
had its own resolution path it would need its own retry and failover logic, and
the two would drift — the classic version of that bug is `auto` silently losing
failover because the person who added the router only wired up the primary. Here
`auto` inherits everything `fast` has, because after classification it *is* `fast`.

**There are two classifiers, and the weak one is not dead code.**
`LengthClassifier` is the published baseline — `docs/IMPLEMENTATION_GUIDE.md` sets
the bar at beating a length-only heuristic (about 60% on `data/routing_eval.jsonl`)
— and it is what runs when no embedder is supplied, which keeps `auto` working in
the no-model test lane and gives the eval harness a number to improve *on* rather
than a hypothetical one. `SemanticClassifier` is the deliverable: k-nearest
neighbours over the self-authored exemplars in `prism/router_exemplars.py`, in the
same embedding space as the semantic cache.

**Length lies in both directions, so the router does not look at length.** It looks
at what the caller is asking for, which means stripping pasted payload first — see
`extract_ask`. A four-hundred-word log excerpt followed by "which line is the first
error?" is a `fast` request, and a one-line "prove there is no largest prime" is a
`smart` one.

`data/routing_eval.jsonl` carries `expected_tier` labels. Those are graded against
and are deliberately not read here — `docs/DATA_MODEL.md:11` says so, and a
classifier that consulted them would score 100% while classifying nothing. Neither
are they tuned against: `docs/DESIGN_NOTES.md` requires the exemplars and the
constants below to be settled on a self-authored dev set
(`tests/data/router_dev_set.jsonl`) with the eval scored once, held out.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from prism.config import GatewayConfig, ModelAlias, ResolvedTarget
from prism.embeddings import Embedder, cosine
from prism.errors import NotFoundError
from prism.router_exemplars import EXEMPLARS, Exemplar
from prism.schemas import message_text

logger = logging.getLogger("prism.routing")

#: Difficulty labels from easiest to hardest. The gateway does not invent labels —
#: a router's config decides which exist (`route_by_difficulty`) — but it does need
#: to know their *order* to map a difficulty score onto whichever labels a given
#: deployment configured. Unknown labels sort last, so a config using its own
#: vocabulary still routes rather than crashing.
DIFFICULTY_ORDER = ("trivial", "simple", "easy", "medium", "hard", "complex", "expert")

#: Word count above which the baseline calls a prompt hard. Chosen by inspecting
#: the *prompts* in `data/routing_eval.jsonl`, not its labels: the short factual
#: questions there run to a dozen words and the multi-part design questions run to
#: forty-plus, so the boundary sits between them. It is a weak signal on purpose —
#: see the module docstring.
LENGTH_THRESHOLD_WORDS = 24

#: How many router hops to follow before giving up. Config validation
#: (`prism/config.py:231`) already rejects a router whose target is another router,
#: so one hop is all a valid config can need; this is the belt to that braces, and
#: it turns a hypothetical infinite loop into a startup-grade error instead of a
#: hung request.
MAX_ROUTER_HOPS = 4

#: How many nearest exemplars vote. Settled on `tests/data/router_dev_set.jsonl`.
NEIGHBOURS = 5

#: Softmax temperature for the neighbour weights. Cosines between related prompts
#: in this space sit in a narrow band (roughly 0.55–0.95), so unweighted votes are
#: nearly uniform and the nearest exemplar counts for no more than the fifth. A
#: temperature of this size turns a 0.10 similarity gap into a ~7x weight ratio,
#: which is the difference between "the neighbours agree" and "one neighbour is
#: obviously right".
SIMILARITY_TEMPERATURE = 0.05

#: Prompts at or below this length are classified whole. Ask extraction exists to
#: remove pasted payload, and a prompt this short has none — running it anyway
#: risks deleting the ask itself, which is the one thing that must survive.
ASK_EXTRACTION_MIN_WORDS = 30

#: Upper bound on what gets embedded. bge-small truncates at 512 tokens anyway, so
#: the choice is *which* words survive rather than whether to truncate; head and
#: tail are kept because an instruction sits at one end or the other and payload
#: sits in the middle. `docs/EVALUATION_GUIDE.md` explicitly permits truncating
#: before classification.
MAX_ASK_WORDS = 60

#: A quoted span this long is pasted material, not a phrase being asked about.
QUOTE_MIN_WORDS = 8

#: Quoted spans, including fenced blocks. The lookarounds on the single-quote form
#: keep apostrophes ("last week's totals") from opening a quotation.
_QUOTED = re.compile(
    r"```.*?```|\"\"\".*?\"\"\"|'''.*?'''|\"[^\"]+\"|“[^”]+”"
    r"|(?<![A-Za-z])'[^']+'(?![A-Za-z])",
    re.DOTALL,
)

#: Sentence-ish boundaries: terminal punctuation followed by space, or a newline.
#: Requiring the space is what keeps `handlers.py", line 88` and
#: `asyncpg.exceptions.PoolTimeout` in one piece, which matters because pasted
#: tracebacks are exactly the payload this is trying to drop.
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

#: A request cue: the verb of an imperative, at a position where a clause can
#: start. Anchoring to the start of the sentence alone is not enough — "Given a
#: provider that starts failing, **work out** what this does to tail latency" is
#: the ask of its sentence, and it begins after a comma.
_ASK_CUE = re.compile(
    r"(?:^|[,;:]\s*|\b(?:and|then|also|please|so)\s+)"
    r"(?:can you|could you|would you|i need|i want|help me|walk me through|"
    r"analyse|analyze|calculate|compare|compute|convert|derive|describe|design|diagnose|"
    r"draft|estimate|explain|extract|figure out|find|fix|give|implement|justify|list|make|"
    r"outline|plan|prove|pull|rank|recommend|rewrite|say|show|sketch|suggest|summarise|"
    r"summarize|tell|translate|turn|weigh|work out|write)\b",
    re.IGNORECASE,
)

#: An interrogative opening, for questions typed without a question mark.
_QUESTION_START = re.compile(
    r"^\W*(?:what|which|who|whom|whose|when|where|why|how|is|are|was|were|do|does|did|"
    r"can|could|should|would|will|has|have|if)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Ask:
    """The part of a prompt that says what is wanted, plus what it cost to find.

    `text` is what gets embedded. The counts and `dropped` tags exist for
    `route_reason`: they let an operator see that a 300-word request was classified
    on 11 words, without the prompt itself being copied into the audit trail.
    """

    text: str
    #: Words in the original prompt.
    full_words: int
    #: Words actually classified.
    words: int
    #: What the extractor removed, e.g. `("quote", "payload", "clipped")`.
    dropped: tuple[str, ...] = ()


def _clip(words: list[str], limit: int) -> tuple[str, bool]:
    if len(words) <= limit:
        return " ".join(words), False
    head = limit // 2
    tail = limit - head
    return " ".join([*words[:head], "...", *words[-tail:]]), True


def extract_ask(prompt: str) -> Ask:
    """Reduce a prompt to the request inside it.

    Difficulty is a property of the ask, not of the message. Two of the eval's trap
    shapes exist because of that gap (`docs/EVALUATION_GUIDE.md:82`): a wall of
    pasted log followed by "what is the first ERROR timestamp?" is trivial, and
    embedding the wall drowns the question in server-log vocabulary.

    So: drop long quoted spans, then keep only the sentences that carry a question
    or a request cue. If nothing survives — a prompt that is all payload, or phrased
    in a way the cues miss — fall back to the whole prompt, clipped. **Falling back
    is not a failure mode to be avoided by making the cue list aggressive**; the
    cost of dropping a real ask (a hard prompt silently routed to the small model)
    is much higher than the cost of classifying some payload along with it.

    The same function runs over the exemplars at startup, so both sides of every
    comparison have been through the same front end.
    """
    words = prompt.split()
    full_words = len(words)
    if full_words <= ASK_EXTRACTION_MIN_WORDS:
        return Ask(text=prompt.strip(), full_words=full_words, words=full_words)

    dropped: list[str] = []

    def _drop_quote(match: re.Match[str]) -> str:
        return " " if len(match.group(0).split()) >= QUOTE_MIN_WORDS else match.group(0)

    unquoted = _QUOTED.sub(_drop_quote, prompt)
    if unquoted != prompt:
        dropped.append("quote")

    sentences = [part.strip() for part in _SENTENCE.split(unquoted) if part.strip()]
    kept = [
        sentence
        for sentence in sentences
        if "?" in sentence or _ASK_CUE.search(sentence) or _QUESTION_START.match(sentence)
    ]
    if kept and len(" ".join(kept).split()) >= 3:
        if len(kept) < len(sentences):
            dropped.append("payload")
        candidate = " ".join(kept).split()
    else:
        dropped.append("no-cue")
        candidate = unquoted.split()

    text, clipped = _clip(candidate, MAX_ASK_WORDS)
    if clipped:
        dropped.append("clipped")
    return Ask(text=text, full_words=full_words, words=len(text.split()), dropped=tuple(dropped))


@runtime_checkable
class Classifier(Protocol):
    """What `resolve` needs from a difficulty classifier.

    Async because the real one embeds, and embedding runs in a worker thread. The
    baseline is async too even though it does no I/O: a Protocol that both
    implementations satisfy is worth more than saving an `await` on the cheap path.
    """

    async def classify(self, prompt: str, labels: list[str]) -> tuple[str, str]:
        """Return `(difficulty_label, reason)`. `labels` is the config's vocabulary."""
        ...

    def bind(self, embedder: Embedder) -> Classifier:
        """A view of this classifier that embeds through `embedder`.

        The classifier is app-scoped — its exemplar vectors are computed once — but
        the *memo* for a single request is request-scoped
        (`prism/embeddings.py:MemoEmbedder`). Binding is how those meet: the router
        and the cache then share one memo, so a short prompt through `auto` with
        caching on is embedded once rather than twice. Cheap by construction: the
        bound object shares the prepared vectors rather than copying or recomputing.
        """
        ...


@dataclass(frozen=True)
class Route:
    """The resolved plan for one request."""

    requested: str
    #: Ordered attempts: primary first. Never empty.
    chain: tuple[ResolvedTarget, ...]
    #: The tier a router selected (`fast`, `smart`), or None for a direct request.
    #: Named for what the routing eval grades (`expected_tier`).
    tier: str | None = None
    #: Human-readable decision trail for `request_log.route_reason`. Null for a
    #: direct request, because "the caller asked for it" is not a decision.
    reason: str | None = None

    @property
    def primary(self) -> ResolvedTarget:
        return self.chain[0]


def last_user_text(messages: list[dict] | None) -> str:
    """The text the classifier looks at.

    The *last* user message, not the whole conversation: in a multi-turn chat the
    accumulated history dominates any length signal, so a trivial follow-up in a
    long thread would be classified as hard purely because the thread is long.

    Flattening multimodal content is delegated to `schemas.message_text`, which the
    cache calls on the same message. The router and the cache must agree on what a
    prompt says, and two copies of that logic is how they would stop agreeing.
    """
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return message_text(message)
    return ""


def _ordered(labels: list[str]) -> list[str]:
    """Sort difficulty labels easiest-first, unknown vocabulary last."""
    return sorted(
        labels,
        key=lambda label: (
            DIFFICULTY_ORDER.index(label) if label in DIFFICULTY_ORDER else len(DIFFICULTY_ORDER),
            label,
        ),
    )


def classify_difficulty(prompt: str, labels: list[str]) -> tuple[str, str]:
    """Baseline difficulty classifier. Returns `(label, reason)`.

    Length only, and the reason string says so, because a `route_reason` that
    claims more insight than the code has is worse than no reason at all — it is
    the line someone will quote back during a failure investigation.
    """
    available = _ordered(labels)
    words = len(prompt.split())
    if words > LENGTH_THRESHOLD_WORDS:
        label = available[-1]
        verdict = f"{words} words > {LENGTH_THRESHOLD_WORDS}"
    else:
        label = available[0]
        verdict = f"{words} words <= {LENGTH_THRESHOLD_WORDS}"
    return label, f"heuristic=length ({verdict})"


def _label_for_score(score: float, labels: list[str]) -> tuple[str, float]:
    """Map a difficulty score in [0, 1] onto a config's label vocabulary.

    The exemplars are labelled with a float, not a tier, so that this mapping is the
    only place that knows how many tiers a deployment has: two labels split at 0.5,
    three at 0.33 and 0.67, and a config with its own vocabulary still routes.
    Returns the distance to the nearest boundary as well, which is the honest
    measure of how close the decision was — and it goes in `route_reason`, because
    "0.51" and "0.98" deserve different amounts of trust during an investigation.
    """
    available = _ordered(labels)
    count = len(available)
    index = min(count - 1, max(0, int(score * count)))
    edges = [position / count for position in range(1, count)]
    margin = min(abs(score - edge) for edge in edges) if edges else 1.0
    return available[index], margin


class LengthClassifier:
    """The published baseline: word count against a threshold.

    Kept as a real implementation rather than deleted once the semantic router
    worked, for three reasons. It is the number the deliverable is measured against
    (`docs/IMPLEMENTATION_GUIDE.md`); it is what `auto` uses when no embedder is
    configured, so the no-model test lane still exercises the router end to end; and
    it is the fallback if loading the artifact ever becomes optional.
    """

    async def classify(self, prompt: str, labels: list[str]) -> tuple[str, str]:
        return classify_difficulty(prompt, labels)

    def bind(self, embedder: Embedder) -> Classifier:
        """Itself. Counting words needs no embedder, so there is nothing to bind."""
        return self


class SemanticClassifier:
    """k-nearest neighbours over the labelled exemplars, in the cache's own space.

    kNN rather than a trained classifier or an LLM call. An LLM call would mean a
    round trip *before* the round trip, on the request path, to decide where the
    request goes — the routing decision would then cost more than the routing saves,
    and it would need its own retry and failover logic. A trained model would mean
    shipping weights fitted to twenty-odd examples. kNN has no training step, its
    knowledge is a readable file (`prism/router_exemplars.py`), and its mistakes are
    explainable: `route_reason` names the shapes that voted.

    Exemplar vectors are computed once, at startup, and held in memory. Fifty
    384-dimensional vectors is 150 KB, and doing it per request would be fifty
    embeddings of unchanging text per call.
    """

    def __init__(
        self,
        embedder: Embedder,
        exemplars: tuple[Exemplar, ...] = EXEMPLARS,
        *,
        neighbours: int = NEIGHBOURS,
        temperature: float = SIMILARITY_TEMPERATURE,
    ) -> None:
        self._embedder = embedder
        self._exemplars = tuple(exemplars)
        self._neighbours = neighbours
        self._temperature = temperature
        self._vectors: list[list[float]] | None = None
        self._lock = asyncio.Lock()

    async def prepare(self) -> None:
        """Embed the exemplars. Called from the lifespan; safe to call repeatedly.

        The double check around the lock is not superstition: without it, two
        requests arriving before startup finished would both pass the outer test and
        embed fifty prompts each.
        """
        if self._vectors is not None:
            return
        async with self._lock:
            if self._vectors is not None:
                return
            # The exemplars go through `extract_ask` too. Four of them carry pasted
            # payload on purpose, and comparing an extracted ask against an
            # unextracted exemplar would compare two different kinds of text.
            asks = [extract_ask(exemplar.text).text for exemplar in self._exemplars]
            self._vectors = await self._embedder.embed(asks)
            logger.info(
                "router exemplars ready: n=%d dim=%d k=%d",
                len(self._vectors),
                self._embedder.dimension,
                self._neighbours,
            )

    def bind(self, embedder: Embedder) -> SemanticClassifier:
        """A twin that embeds through `embedder` and shares this one's vectors.

        If the exemplars have not been embedded yet, the twin's `prepare` will do it
        through the *bound* embedder and the original stays empty — harmless (the
        vectors are identical either way; the memo simply holds them for one
        request), and avoided in practice because the lifespan prepares at startup.
        """
        twin = SemanticClassifier(
            embedder,
            self._exemplars,
            neighbours=self._neighbours,
            temperature=self._temperature,
        )
        twin._vectors = self._vectors
        return twin

    async def classify(self, prompt: str, labels: list[str]) -> tuple[str, str]:
        await self.prepare()
        if not self._vectors:
            # An empty exemplar set is a configuration mistake, not a reason to
            # refuse the request; degrade to the baseline and say so in the reason.
            label, how = classify_difficulty(prompt, labels)
            return label, f"{how} (no exemplars)"

        ask = extract_ask(prompt)
        vector = await self._embedder.embed_one(ask.text)
        ranked = sorted(
            zip(
                (cosine(vector, known) for known in self._vectors),
                self._exemplars,
                strict=True,
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )[: self._neighbours]

        best = ranked[0][0]
        # Weights are exp((sim - best) / T), i.e. softmax shifted so the largest
        # exponent is zero. Mathematically identical to softmax, and it cannot
        # overflow for a small temperature.
        weights = [math.exp((similarity - best) / self._temperature) for similarity, _ in ranked]
        total = sum(weights)
        weighted = zip(weights, ranked, strict=True)
        score = sum(weight * exemplar.difficulty for weight, (_, exemplar) in weighted) / total
        label, margin = _label_for_score(score, labels)

        near = ",".join(exemplar.shape for _, exemplar in ranked[:3])
        reason = (
            f"knn=exemplars(k={len(ranked)},n={len(self._exemplars)}) "
            f"score={score:.2f} margin={margin:.2f} top={best:.2f} near={near} "
            f"ask={ask.words}/{ask.full_words}w"
        )
        if ask.dropped:
            reason += f" dropped={'+'.join(ask.dropped)}"
        # Deliberately no prompt text: `route_reason` is written to `request_log`,
        # and an audit trail that quotes prompts is a second copy of the data the
        # gateway promised not to keep. Shapes come from a file in this repository.
        return label, reason


def _chain_targets(config: GatewayConfig, alias: ModelAlias) -> tuple[ResolvedTarget, ...]:
    return tuple(
        ResolvedTarget(provider=config.provider_for_model(model), model=model)
        for model in alias.chain
    )


async def resolve(
    config: GatewayConfig,
    requested_model: str,
    messages: list[dict] | None = None,
    classifier: Classifier | None = None,
) -> Route:
    """Resolve a requested model or alias into an ordered chain of attempts.

    Async only because classification may embed. The two non-router branches below
    do no I/O at all and return without awaiting anything, so a `fast` or
    `alpha-small` request pays a coroutine frame and nothing else — the alternative
    (a sync `resolve` plus an async `resolve_routed`) splits the one code path that
    exists to *not* be split.

    `classifier` defaults to the length baseline rather than to the semantic one so
    that this module has no mandatory dependency on a 65 MB artifact: tests and
    scripts that do not care about routing quality call it with one argument.

    Raises `NotFoundError` for anything unknown. In the request path the allowlist
    check has already rejected unknown names with the same 404
    (`prism/auth.py:96`), so this is the defence for every *other* caller —
    the admin console, a test, the routing eval — rather than dead code.
    """
    alias = config.alias(requested_model)

    if alias is None:
        # A concrete model name. `provider_for_model` and `price` both raise
        # NotFoundError, so an unpriced or unowned model cannot reach an upstream.
        config.price(requested_model)
        target = ResolvedTarget(
            provider=config.provider_for_model(requested_model), model=requested_model
        )
        return Route(requested=requested_model, chain=(target,))

    if not alias.is_router:
        return Route(requested=requested_model, chain=_chain_targets(config, alias))

    # A router. Classify once, then follow the tier it names.
    prompt = last_user_text(messages)
    engine = classifier or LengthClassifier()
    label, how = await engine.classify(prompt, list(alias.route_by_difficulty))
    tier = alias.route_by_difficulty[label]

    hops = 0
    current = config.alias(tier)
    trail = [tier]
    while current is not None and current.is_router:
        hops += 1
        if hops >= MAX_ROUTER_HOPS:
            # Unreachable through a validated config; see MAX_ROUTER_HOPS.
            raise NotFoundError(
                f"Alias {requested_model!r} routes through more than {MAX_ROUTER_HOPS} "
                "levels of router."
            )
        nested_label, _ = await engine.classify(prompt, list(current.route_by_difficulty))
        tier = current.route_by_difficulty[nested_label]
        trail.append(tier)
        current = config.alias(tier)

    if current is None:
        raise NotFoundError(
            f"Alias {requested_model!r} routes difficulty {label!r} to {tier!r}, "
            "which is not a configured alias."
        )

    return Route(
        requested=requested_model,
        chain=_chain_targets(config, current),
        tier=tier,
        reason=f"{requested_model}: difficulty={label} ({how}) -> {' -> '.join(trail)}",
    )


__all__ = [
    "DIFFICULTY_ORDER",
    "LENGTH_THRESHOLD_WORDS",
    "MAX_ASK_WORDS",
    "NEIGHBOURS",
    "SIMILARITY_TEMPERATURE",
    "Ask",
    "Classifier",
    "LengthClassifier",
    "Route",
    "SemanticClassifier",
    "classify_difficulty",
    "extract_ask",
    "last_user_text",
    "resolve",
]
