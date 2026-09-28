#!/usr/bin/env python
"""Score the difficulty router against a labelled dataset. One command, one number.

    python scripts/routing_eval.py                        # the held-out eval, semantic router
    python scripts/routing_eval.py --classifier length     # the published baseline
    python scripts/routing_eval.py --dataset tests/data/router_dev_set.jsonl
    python scripts/routing_eval.py --json > routing_eval_results.json

Classification only: nothing is sent upstream, no database is touched, and no
provider needs to be running (`docs/EVALUATION_GUIDE.md:67`). The script drives the
same `prism.routing.resolve` the request path drives, through the same `auto` alias,
so what it grades is the shipped router and not a copy of it.

`expected_tier` is the answer key (`docs/DATA_MODEL.md:11`), so **nothing under
`prism/` reads it** — it never reaches the classifier, only the scorer below. The one
other reader in the repository is the dev-set quality test in
`tests/test_router_semantic.py`, which grades the same way against the same key and is
also not routing logic. That is the invariant worth stating precisely: not "one file
touches this field", but "no code that decides a route can see it".

Two datasets, and the distinction is the whole method:

* `tests/data/router_dev_set.jsonl` is mine. Constants and exemplars were settled
  against it, and `--k` / `--temperature` exist for exactly that sweep.
* `data/routing_eval.jsonl` is **held out**. Per `docs/DESIGN_NOTES.md` it is scored
  to report a number, not to choose one. Any run against it that leads to a change
  in `prism/router_exemplars.py` or the constants in `prism/routing.py` has turned
  the answer key into training data, and the reported accuracy stops meaning
  anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from prism import routing  # noqa: E402
from prism.config import load_gateway_config  # noqa: E402
from prism.router_exemplars import EXEMPLARS, shapes  # noqa: E402
from prism.settings import load_settings  # noqa: E402

DEFAULT_DATASET = Path("data/routing_eval.jsonl")


def read_cases(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"Dataset not found: {path}")
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{number}: {exc}") from exc
    return cases


async def run(args: argparse.Namespace) -> dict:
    settings = load_settings()
    config = load_gateway_config(settings.gateway_config_path, settings.pricing_path)

    alias = config.alias(args.model)
    if alias is None or not alias.is_router:
        raise SystemExit(
            f"Alias {args.model!r} is not a router. The eval grades a router's tier "
            "choice, so it needs an alias with route_by_difficulty (normally 'auto')."
        )

    if args.classifier == "length":
        classifier: routing.Classifier = routing.LengthClassifier()
        method = {
            "classifier": "length",
            "threshold_words": routing.LENGTH_THRESHOLD_WORDS,
        }
    else:
        from prism.embeddings import MODEL_NAME, FastEmbedEmbedder

        embedder = FastEmbedEmbedder(settings.require_embedding_cache())
        semantic = routing.SemanticClassifier(
            embedder, neighbours=args.k, temperature=args.temperature
        )
        await semantic.prepare()
        classifier = semantic
        method = {
            "classifier": "knn-exemplars",
            "embedding_model": MODEL_NAME,
            "exemplars": len(EXEMPLARS),
            "shapes": shapes(),
            "k": args.k,
            "temperature": args.temperature,
            "ask_extraction": {
                "min_words": routing.ASK_EXTRACTION_MIN_WORDS,
                "max_ask_words": routing.MAX_ASK_WORDS,
            },
        }

    results = []
    for case in read_cases(args.dataset):
        prompt = case["prompt"]
        route = await routing.resolve(
            config, args.model, [{"role": "user", "content": prompt}], classifier
        )
        expected = case.get("expected_tier")  # answer key: scoring only
        results.append(
            {
                "id": case.get("id", "?"),
                "expected": expected,
                "actual": route.tier,
                "correct": route.tier == expected,
                "reason": route.reason,
                "note": case.get("note"),
            }
        )

    correct = sum(1 for row in results if row["correct"])
    total = len(results)
    return {
        "dataset": str(args.dataset).replace("\\", "/"),
        "held_out": Path(args.dataset) == DEFAULT_DATASET,
        "model": args.model,
        "method": method,
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "cases": [{k: v for k, v in row.items() if k != "correct"} for row in results],
        # Kept out of `cases` so the shape stays exactly what the guide asks for.
        "misses": [row["id"] for row in results if not row["correct"]],
    }


def report(summary: dict) -> None:
    """Human-readable version. The JSON is the artifact; this is for iterating."""
    print(f"dataset  {summary['dataset']}  ({'held out' if summary['held_out'] else 'dev set'})")
    method = summary["method"]
    print(f"method   {method['classifier']}", end="")
    if method["classifier"] == "knn-exemplars":
        print(
            f"  n={method['exemplars']} k={method['k']} T={method['temperature']}"
            f"  model={method['embedding_model']}"
        )
    else:
        print(f"  threshold={method['threshold_words']} words")
    print()
    for row in summary["cases"]:
        verdict = "ok  " if row["expected"] == row["actual"] else "MISS"
        print(f"{verdict} {row['id']:<12} expected={row['expected']:<6} actual={row['actual']:<6}")
        print(f"       {row['reason']}")
        if verdict == "MISS" and row.get("note"):
            print(f"       note: {row['note']}")
    print()
    print(
        f"accuracy {summary['correct']}/{summary['total']} = "
        f"{summary['accuracy'] * 100:.1f}%"
        + (f"   misses: {', '.join(summary['misses'])}" if summary["misses"] else "")
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model", default="auto", help="router alias to grade")
    parser.add_argument("--classifier", choices=("semantic", "length"), default="semantic")
    parser.add_argument("--k", type=int, default=routing.NEIGHBOURS, help="neighbours that vote")
    parser.add_argument(
        "--temperature", type=float, default=routing.SIMILARITY_TEMPERATURE,
        help="softmax temperature for neighbour weights",
    )
    parser.add_argument("--json", action="store_true", help="emit only the JSON result")
    args = parser.parse_args()

    summary = asyncio.run(run(args))
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        report(summary)


if __name__ == "__main__":
    main()
