# Prism — LLM Gateway and Semantic Cache

A multi-tenant LLM gateway: one OpenAI-compatible API in front of multiple providers, with
difficulty-based routing, retries and failover, per-key rate limits and cost budgets,
token-by-token streaming, usage metering, and a semantic response cache.

`data/`, `scripts/` and `docs/` are the provided project scaffold, imported unmodified in the
first commit (`84cf8cb`). Everything else is mine.

## Provenance

Commit `84cf8cb` is the untouched scaffold import and the provenance boundary — every commit
after it is my own work. Do not amend or rewrite it.

The two provided root-level docs were relocated into `docs/` in the setup PR, as renames so the
history stays legible:

- `README.md` → `docs/PROVIDED_PACK.md`
- `PRISM_PROBLEM_STATEMENT.md` → `docs/PRISM_PROBLEM_STATEMENT.md`

Paths quoted inside `docs/PROVIDED_PACK.md` are relative to the repository root, not to `docs/`.
Its shell commands are meant to be run from the root.

## Environment

- **Python 3.12**, via a venv at `.venv/`. Create it with `py -3.12 -m venv .venv`.
- Bare `python` on this machine resolves to **3.7.9** (EOL) and `python3` does not exist in Git
  Bash. Always activate the venv, or use `py -3.12` explicitly. Never assume `python3` works.
- **Postgres, not SQLite.** SQLite serialises writers, which would hide the read-then-write rate
  limiter race that `scripts/load_test.py` exists to catch — "no over-admission" would pass for
  the wrong reason.

## Verification

The scaffold ships its own checks; prefer them over ad-hoc testing.

| Script | Checks | Needs |
|---|---|---|
| `scripts/validate_pack.py` | provided data parses and is self-consistent | nothing |
| `scripts/mock_provider.py` | zero-dep OpenAI-compatible upstream, live failure injection | nothing |
| `scripts/smoke_test.py` | API + `x-prism-*` header contract, streaming, cache | gateway on `:8080` |
| `scripts/load_test.py` | over-admission, accounting reconciliation, latency | gateway on `:8080` |

Run two mock providers (`--port 9001 --name alpha`, `--port 9002 --name beta`) as the intended
upstreams — free, offline, and deterministic for failover.

## Working agreement

- Feature work goes on a branch and lands via PR, never straight to `main`.
- This repo is personal: commits must use `Asmasobia <asmarosealia@gmail.com>`, set in
  `.git/config`. No work identity, work email, or work data belongs anywhere in this repository.
- Keep the README's **Known limitations** section current — add a line every time something is
  deferred. It is a graded deliverable and cannot be reconstructed honestly at the end.
