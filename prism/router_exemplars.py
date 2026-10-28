"""Labelled exemplars for the difficulty router. Self-authored, on purpose.

`data/routing_eval.jsonl` is the answer key. `docs/DATA_MODEL.md:11` forbids reading
its `expected_tier` in routing logic, and `docs/DESIGN_NOTES.md` goes further and
forbids *tuning* against it: a classifier fitted to twenty known prompts measures
its author's memory, not its own generalisation. So the router's knowledge lives
here, written from first principles, and the eval is scored once as a held-out set.

**The exemplars encode task shape, not topic.** Difficulty in this system is a
property of what the caller is asking the model to *do* — recall a fact versus
construct an argument — and embeddings are dominated by subject matter, so a naive
exemplar set teaches the router that "databases are hard" and "greetings are easy".
Two rules keep that from happening:

1. **Topics appear on both sides.** Postgres, Docker, Python, Kafka, HTTP and
   incident response each have an easy exemplar *and* a hard one, so topic
   similarity cancels out and shape is what is left to discriminate on.
2. **Both trap shapes are represented as shapes.** `docs/EVALUATION_GUIDE.md:82`
   says the eval is trapped with short-but-hard and long-but-trivial prompts. Those
   are not eval quirks, they are the two ways length lies, so this set contains
   proofs and estimations that fit in one line, and pasted payloads whose actual ask
   is a lookup.

`difficulty` is a float on a two-point scale — 0.0 for "a small model answers this
correctly", 1.0 for "this needs the reasoning-grade model" — rather than a tier
name, because the *tier* is the config's vocabulary (`route_by_difficulty` maps
`simple`/`complex`, and another deployment may use four labels). Keeping the
exemplars in difficulty space means the config decides how many tiers exist and
this file never has to change with it.

`shape` is a short tag used only in `route_reason`, so an operator reading the log
sees *why* a tier was chosen without the prompt itself being copied into the audit
trail.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Exemplar:
    """One labelled example. `difficulty` is 0.0 (easy) to 1.0 (hard)."""

    text: str
    difficulty: float
    shape: str


#: Task families a small model handles: one retrieval or one mechanical
#: transformation, with no argument to construct.
EASY: tuple[Exemplar, ...] = (
    Exemplar("What is the boiling point of water at sea level?", 0.0, "fact-lookup"),
    Exemplar("Who wrote the novel Frankenstein?", 0.0, "fact-lookup"),
    Exemplar("What does the HTTP Retry-After header do?", 0.0, "definition"),
    Exemplar("What is a Kafka topic partition?", 0.0, "definition"),
    Exemplar("What does the -d flag do in docker run?", 0.0, "definition"),
    # Rule 1 in the docstring, learned the hard way: without this line the dev set's
    # "what is a database index?" routed to the smart tier, because the only
    # index-shaped exemplar was the *hard* one below ("why do indexes slow writes").
    # The topic was one-sided, so the router had learned that indexes are difficult.
    Exemplar("What does a database index do?", 0.0, "definition"),
    Exemplar("What port does PostgreSQL listen on by default?", 0.0, "fact-lookup"),
    Exemplar("How many kilometres are in 12 miles?", 0.0, "unit-conversion"),
    Exemplar("Convert 2.5 hours into minutes.", 0.0, "unit-conversion"),
    Exemplar("Translate 'thank you very much' into Italian.", 0.0, "translation"),
    Exemplar("How do you say 'where is the station' in German?", 0.0, "translation"),
    Exemplar("Is 143 a prime number?", 0.0, "arithmetic-check"),
    Exemplar("What is 15% of 240?", 0.0, "arithmetic-check"),
    Exemplar(
        "Pull the phone number out of this sentence: 'Call the depot on 0161 555 0134 "
        "before noon.'",
        0.0,
        "extraction",
    ),
    Exemplar(
        "From this line, what is the order id? 'shipped order=A-4471 carrier=DHL'",
        0.0,
        "extraction",
    ),
    Exemplar("Write a one-line Python expression that reverses a list.", 0.0, "snippet"),
    Exemplar("Give me the shell command to list files by size, largest first.", 0.0, "snippet"),
    Exemplar(
        "Suggest a short subject line for an email announcing a maintenance window.",
        0.0,
        "short-writing",
    ),
    Exemplar(
        "Turn this into a single sentence: 'The build failed. The cause was a missing "
        "environment variable. It is fixed now.'",
        0.0,
        "short-writing",
    ),
    Exemplar(
        "Fix the grammar in this sentence: 'Their going to deploy the change tomorrow.'",
        0.0,
        "copy-edit",
    ),
    Exemplar(
        "Make this sentence less formal: 'Please be advised that the office will be closed.'",
        0.0,
        "copy-edit",
    ),
    Exemplar(
        "Here are our expense totals by month: January 4,120; February 3,880; March 5,410; "
        "April 4,995; May 5,120; June 4,700; July 5,380; August 6,010; September 5,240; "
        "October 4,860; November 5,730; December 6,450. Which month had the highest total?",
        0.0,
        "payload-lookup",
    ),
    Exemplar(
        "This is our service configuration:\n"
        "server:\n  host: 0.0.0.0\n  port: 8080\n  workers: 4\n"
        "database:\n  host: db.internal\n  port: 5432\n  max_connections: 40\n  timeout_ms: 2500\n"
        "logging:\n  level: info\n  format: json\n"
        "What value is set for max_connections?",
        0.0,
        "payload-lookup",
    ),
    Exemplar(
        "Notes from standup: Priya finished the importer and will review the schema PR. "
        "Chen is blocked on credentials and will chase IT. Amara volunteered to write the "
        "release notes. Diego is on holiday until Thursday. Fatima will run the migration "
        "rehearsal on Friday afternoon. Who agreed to write the release notes?",
        0.0,
        "payload-lookup",
    ),
    Exemplar(
        "Departures: BA219 08:40, LH441 07:15, AF1180 09:05, KL1002 06:50, IB3121 10:20, "
        "SN2104 07:55, TP1234 06:35, AZ204 11:10. Which flight leaves earliest?",
        0.0,
        "payload-lookup",
    ),
    Exemplar(
        "Rewrite the paragraph below so it fits in two sentences, keeping the meaning: "
        "'We are moving the weekly sync from Tuesday to Wednesday because several people "
        "have a conflict on Tuesday afternoons. The room is the same. The agenda document "
        "is unchanged and will still be circulated the evening before.'",
        0.0,
        "payload-rewrite",
    ),
)

#: Task families that need a reasoning-grade model: an argument, a derivation, a
#: design, or a diagnosis has to be constructed and defended.
HARD: tuple[Exemplar, ...] = (
    Exemplar("Show that there is no largest prime number.", 1.0, "proof"),
    Exemplar("Prove that the sum of two odd numbers is always even.", 1.0, "proof"),
    Exemplar(
        "Prove or disprove: every graph with more edges than vertices contains a cycle.",
        1.0,
        "proof",
    ),
    Exemplar(
        "Roughly how much paint would it take to repaint every bus in a mid-sized city? "
        "Show your assumptions.",
        1.0,
        "estimation",
    ),
    Exemplar(
        "Estimate the yearly electricity cost of running our staging environment overnight, "
        "and say which assumption your answer is most sensitive to.",
        1.0,
        "estimation",
    ),
    # A Fermi question that opens with "how many". The two estimation exemplars above
    # both open with an estimate/roughly cue, so the dev set's "how many words does a
    # person speak in a lifetime" landed next to *unit conversion* — "how many X in a
    # Y" is the grammar of both, and only the multi-step derivation distinguishes
    # them. This exemplar puts that grammar on the hard side too.
    Exemplar(
        "How many litres of coffee does an office of fifty people drink in a year? "
        "Show your reasoning.",
        1.0,
        "estimation",
    ),
    Exemplar(
        "Why can adding a database index make writes slower but reads faster? Give the "
        "mechanisms.",
        1.0,
        "causal-mechanism",
    ),
    Exemplar(
        "Why might a service get slower as you add more worker threads? Give several "
        "plausible mechanisms.",
        1.0,
        "causal-mechanism",
    ),
    Exemplar(
        "Design a partitioning and archival strategy for a four-terabyte append-only "
        "Postgres table, and explain what you trade away.",
        1.0,
        "design-tradeoffs",
    ),
    Exemplar(
        "Design an idempotency scheme for a payments API that can survive client retries "
        "and duplicate webhooks. Justify the key you choose.",
        1.0,
        "design-tradeoffs",
    ),
    Exemplar(
        "Design a permission model for an app with organisations, teams and guest users, "
        "and explain how you would keep it auditable.",
        1.0,
        "design-tradeoffs",
    ),
    Exemplar(
        "Compare message queues with scheduled batch jobs for nightly report generation, "
        "and recommend one for a small team, with reasons.",
        1.0,
        "compare-recommend",
    ),
    Exemplar(
        "Should we run our own Kubernetes cluster or use a managed platform? Weigh the "
        "options for a five-person engineering team and recommend one.",
        1.0,
        "compare-recommend",
    ),
    Exemplar(
        "Write a function that finds the k most frequent elements in a stream using bounded "
        "memory, and explain why your approach is correct.",
        1.0,
        "algorithm-explain",
    ),
    Exemplar(
        "Implement a rate limiter that is fair across clients and explain its worst-case "
        "behaviour under a burst.",
        1.0,
        "algorithm-explain",
    ),
    Exemplar(
        "Our containers are killed for running out of memory, but only in production. "
        "List the likely causes in order and how you would confirm each.",
        1.0,
        "incident-diagnosis",
    ),
    Exemplar(
        "Error rates jumped after a config change that supposedly only touched timeouts. "
        "Work out what could be responsible and how you would narrow it down.",
        1.0,
        "incident-diagnosis",
    ),
    Exemplar(
        "Explain when a Python generator can hold memory longer than expected across an "
        "await, and how you would demonstrate it.",
        1.0,
        "semantics-reasoning",
    ),
    Exemplar(
        "Explain what delivery guarantees a Kafka consumer group gives while it is "
        "rebalancing, and give a concrete example of duplicate processing.",
        1.0,
        "semantics-reasoning",
    ),
    Exemplar(
        "Two services both update the same row without a transaction. Explain the "
        "interleavings that lose an update, and how you would prevent them.",
        1.0,
        "semantics-reasoning",
    ),
    Exemplar(
        "Draft a plan for splitting our shared database between two teams without downtime, "
        "including how you would roll back at each stage.",
        1.0,
        "plan-rollback",
    ),
    Exemplar(
        "Plan the rollout of a change to our authentication flow so that a failure affects "
        "as few users as possible, and say what you would measure at each step.",
        1.0,
        "plan-rollback",
    ),
    Exemplar(
        "Write a threat model for a file-upload feature that stores documents for other "
        "customers to download, and rank the risks.",
        1.0,
        "threat-model",
    ),
    Exemplar(
        "How many application servers do we need to serve two thousand requests per second "
        "with a p99 under 300 ms? Derive it and state your assumptions.",
        1.0,
        "capacity-derivation",
    ),
    Exemplar(
        "Our reporting numbers disagree with the billing numbers by a small amount every "
        "month. Explain the classes of bug that produce that symptom and how to tell them "
        "apart.",
        1.0,
        "incident-diagnosis",
    ),
    Exemplar(
        "The requirements for this feature contradict each other on what happens to "
        "in-flight orders. Explain the options, pick one, and defend it.",
        1.0,
        "judgement-under-ambiguity",
    ),
)

EXEMPLARS: tuple[Exemplar, ...] = EASY + HARD


def shapes() -> dict[str, int]:
    """Shape tag → number of exemplars, for the eval report's method section."""
    counts: dict[str, int] = {}
    for exemplar in EXEMPLARS:
        counts[exemplar.shape] = counts.get(exemplar.shape, 0) + 1
    return counts


__all__ = ["EASY", "EXEMPLARS", "HARD", "Exemplar", "shapes"]
