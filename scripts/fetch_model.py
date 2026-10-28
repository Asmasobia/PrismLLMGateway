#!/usr/bin/env python
"""Fetch or verify the vendored embedding model.

The gateway treats `BAAI/bge-small-en-v1.5` (quantized ONNX) as a **vendored
artifact**: present on disk before startup, never fetched on the request path. That
leaves reviewers needing a documented way to obtain it, which is this script.

Two modes, and the asymmetry between them is deliberate:

* `--verify` (default) reads what is already in `PRISM_MODEL_CACHE` and checks every
  file against the digests recorded below. It touches no network, so it is the mode
  that runs in this project's own verification and the mode whose output in the
  README can be trusted.
* `--fetch` downloads the pinned revision from the Hugging Face hub and then verifies
  it. **This half has never been run in this project** — the artifact was vendored out
  of band and re-downloading it was explicitly out of scope. Reviewers should expect
  first use to be its first real run, which is also why it verifies afterwards rather
  than trusting the transfer.

The digests were computed from the vendored copy on disk, not copied from a webpage,
so `--verify` is checking the bytes the measured results in `docs/DESIGN_NOTES.md`
were actually produced with. That is the property worth having: it detects a *changed*
artifact, which would silently move every cosine in this repository.

Never add `verify=False` or `HF_HUB_DISABLE_SSL_VERIFICATION` here or anywhere else.
A TLS failure means the transfer is untrustworthy; the correct response is to fix the
trust store or vendor the artifact by hand, not to stop checking.

Usage:

    python scripts/fetch_model.py              # verify what is on disk
    python scripts/fetch_model.py --fetch      # download the pinned revision, then verify
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The repository `fastembed` resolves `BAAI/bge-small-en-v1.5` to. Named explicitly
#: because the download has to ask for a concrete repo, and because a future
#: `fastembed` release is free to resolve that alias somewhere else — in which case
#: this script would still fetch the artifact the measurements were taken against.
REPO_ID = "qdrant/bge-small-en-v1.5-onnx-q"

#: Pinned, and the reason cosines are reproducible run to run. A floating `main`
#: would let a re-quantized model land silently, and the symptom would be a cache
#: that stops hitting and a router that stops agreeing with its own eval score.
REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"

#: The artifact proper: path relative to the snapshot directory → (size, sha256).
#: Only these five files are checked. The rest of the cache directory is Hugging Face
#: bookkeeping (`refs/`, `trees/`, `CACHEDIR.TAG`, `files_metadata.json`) whose
#: contents legitimately differ between a `snapshot_download` and a `fastembed` fetch,
#: so asserting on it would fail for reasons that say nothing about the model.
FILES: dict[str, tuple[int, str]] = {
    "config.json": (
        706,
        "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd",
    ),
    "model_optimized.onnx": (
        66_465_124,
        "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
    ),
    "special_tokens_map.json": (
        695,
        "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
    ),
    "tokenizer.json": (
        711_396,
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    ),
    "tokenizer_config.json": (
        1_242,
        "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",
    ),
}


def cache_dir() -> Path:
    """`PRISM_MODEL_CACHE`, from the environment or `.env`, or exit.

    The same two sources and the same precedence `prism.settings` uses, so the script
    cannot verify one directory while the gateway loads another. Never defaulted to a
    path: the location is machine-specific, which is why it lives in a gitignored
    `.env` rather than in source.
    """
    value = os.environ.get("PRISM_MODEL_CACHE")
    if not value:
        from dotenv import dotenv_values

        value = dotenv_values(REPO_ROOT / ".env").get("PRISM_MODEL_CACHE")
    if not value:
        sys.exit(
            "PRISM_MODEL_CACHE is not set. Put it in .env (see .env.example) or export "
            "it, then re-run. It must name the directory the model cache lives in."
        )
    return Path(value)


def snapshot_dir(cache: Path) -> Path:
    """Where the Hugging Face cache layout puts a pinned revision's files."""
    return cache / f"models--{REPO_ID.replace('/', '--')}" / "snapshots" / REVISION


def sha256(path: Path) -> str:
    """Digest in 1 MiB chunks: the ONNX file is 63 MiB and need not be resident."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify(cache: Path) -> int:
    """Check every recorded file. Returns a process exit code.

    Reports *all* mismatches rather than stopping at the first, because "the tokenizer
    changed too" and "only the ONNX changed" call for different responses.
    """
    snapshot = snapshot_dir(cache)
    print(f"repo     {REPO_ID}")
    print(f"revision {REVISION}")
    print(f"cache    {cache}")
    if not snapshot.is_dir():
        print(f"\nMISSING  {snapshot}", file=sys.stderr)
        print(
            "The pinned revision is not in this cache. Run with --fetch to download "
            "it, or point PRISM_MODEL_CACHE at the directory that has it.",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    for name, (expected_size, expected_digest) in sorted(FILES.items()):
        path = snapshot / name
        if not path.is_file():
            failures.append(f"{name}: absent")
            print(f"  MISSING  {name}")
            continue
        size = path.stat().st_size
        digest = sha256(path)
        if size != expected_size or digest != expected_digest:
            failures.append(f"{name}: sha256 {digest} size {size}")
            print(f"  CHANGED  {name}")
            print(f"           expected {expected_digest} ({expected_size:,} bytes)")
            print(f"           found    {digest} ({size:,} bytes)")
        else:
            print(f"  ok       {name}  ({size:,} bytes)")

    if failures:
        print(
            "\nThe artifact on disk is not the one this repository's measurements were "
            "taken against. Every cosine in docs/DESIGN_NOTES.md and the routing eval "
            "score assume the pinned bytes above.",
            file=sys.stderr,
        )
        return 1
    print("\nAll files match the pinned revision.")
    return 0


def fetch(cache: Path) -> int:
    """Download the pinned revision, then verify it. Never run in this project.

    `snapshot_download` writes the same `models--org--repo/snapshots/<rev>/` layout
    `fastembed` reads, so fetching here and loading there need no translation.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit(
            "huggingface_hub is not installed. It ships as a fastembed dependency; "
            "run `pip install -r requirements.txt` in the venv first."
        )

    # HF_HUB_OFFLINE is set unconditionally by prism.embeddings, and .env may set it
    # too. Fetching is the one operation that must not honour it, so clear it here —
    # explicitly and locally, rather than by weakening the setting anywhere else.
    os.environ.pop("HF_HUB_OFFLINE", None)

    cache.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {REPO_ID}@{REVISION[:12]} into {cache} ...")
    snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        cache_dir=str(cache),
        # Only the artifact proper. The repository also carries unquantized weights,
        # and pulling them would multiply the transfer for files nothing loads.
        allow_patterns=sorted(FILES),
    )
    print("Download complete; verifying.\n")
    return verify(cache)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="download the pinned revision before verifying (needs network)",
    )
    args = parser.parse_args()

    cache = cache_dir()
    raise SystemExit(fetch(cache) if args.fetch else verify(cache))


if __name__ == "__main__":
    main()
