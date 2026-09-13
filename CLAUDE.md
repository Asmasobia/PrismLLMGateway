# Prism — LLM Gateway and Semantic Cache

A multi-tenant LLM gateway: one OpenAI-compatible API in front of multiple providers, with
difficulty-based routing, retries and failover, per-key rate limits and cost budgets,
token-by-token streaming, usage metering, and a semantic response cache.

The provided project scaffold is **exactly the files in commit `84cf8cb`** — all of `data/`, plus
four scripts and five docs. Everything else in the tree is mine, including later additions to
`scripts/` and `docs/`. Do not judge authorship by directory; judge it by that commit.

Provided, and not to be edited: `data/*`, `scripts/{validate_pack,mock_provider,smoke_test,load_test}.py`,
`docs/{API_CONTRACT,DATA_MODEL,EVALUATION_GUIDE,IMPLEMENTATION_GUIDE,PROVIDED_PACK,PRISM_PROBLEM_STATEMENT}.md`.

Mine, added since: `docs/DESIGN_NOTES.md`, `scripts/pg.sh`, and everything at the repository root
apart from the two relocated docs.

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
- **Postgres, not SQLite.** SQLite serialises writers, so concurrent `UPDATE ... SET spent =
  spent + ?` would appear correct without ever being atomic — budget accounting would reconcile
  for the wrong reason, and `docs/DATA_MODEL.md:86-91` requires the increment itself to be atomic.
  Note this is about **budget accounting, not the rate limiter**: `docs/DATA_MODEL.md:118` permits
  rate-limit state to live in memory provided it is race-safe, so the over-admission the load test
  hunts for is an in-process concurrency bug, not a database one.
- Postgres listens on port **5433**. Two supported paths: `docker compose up -d`, or
  `scripts/pg.sh`, which drives EDB's **binaries zip** unpacked under
  `%LOCALAPPDATA%\prism-postgres` and needs no installer, no Windows service and no elevation.
  See the README for start/stop.

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

- **All work happens on `feedback`.** That is the working branch and the branch under evaluation;
  commit each slice directly onto it, one commit per logical unit. Do not open per-slice branches.
- **Never merge to `main` before the evaluation is complete.** `main` stays at the scaffold import
  (`84cf8cb`) until then, so the reviewer sees the whole build as a single reviewable diff against
  the untouched starting point. The `feedback` → `main` merge is the last action of the project.
- Because there is no per-slice PR gate, the review checkpoint is manual: **stop after each slice
  and let Asma read it before starting the next.** If reading lags, slow down rather than skipping
  the review — a repo that can't be defended on camera defeats the point.
- This repo is personal: commits must use `Asmasobia <asmarosealia@gmail.com>`, set in
  `.git/config`. No work identity, work email, or work data belongs anywhere in this repository.
- Keep the README's **Known limitations** section current — add a line every time something is
  deferred. It is a graded deliverable and cannot be reconstructed honestly at the end.
