#!/usr/bin/env bash
# Demo helpers for docs/DEMO_SCRIPT.md. Source it, never execute it:
#
#     cd /path/to/PrismLLMGateway
#     source scripts/demo_env.sh
#
# Sourcing rather than pasting is deliberate. Git Bash here leaks bracketed-paste
# markers (ESC[200~ / ESC[201~) into the input as literal text, and one landing on a
# closing brace leaves the shell stuck in an unterminated function definition. A file
# has no paste step, so it cannot happen — and it survives closing the terminal.
#
# Run from the repository root: `reset` calls ./scripts/pg.sh by relative path.

if [ ! -f .env ]; then
    echo "demo_env: no .env here. cd to the repository root first." >&2
    return 1 2>/dev/null || exit 1
fi

# The venv, so `python` is 3.12 rather than the machine's 3.7.
# shellcheck disable=SC1091
source .venv/Scripts/activate

# PRISM_ADMIN_TOKEN, for $A and the console. Read exactly one value out of .env rather
# than sourcing the file.
#
# `.env` is written for python-dotenv, which takes backslashes literally. Bash does not:
# `source .env` reads PRISM_MODEL_CACHE=C:\Users\...\prism-models as escape sequences and
# exports C:Users...prism-models instead. That corrupted path is worse than an unset one,
# because pydantic-settings and tests/conftest.py both prefer the process environment over
# .env — so any gateway or pytest run started from this shell would inherit it, fail to
# load the embedding model, and lose semantic caching and `auto` routing while still
# reporting success. pytest would show a green run with 10 silent skips.
_env_value() {
    grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'"'"']//; s/["'"'"']$//'
}

PRISM_ADMIN_TOKEN="$(_env_value PRISM_ADMIN_TOKEN)"
unset -f _env_value

if [ -z "$PRISM_ADMIN_TOKEN" ]; then
    echo "demo_env: PRISM_ADMIN_TOKEN not found in .env — \$A and the console will not work." >&2
fi

# -- credentials -------------------------------------------------------------
S="Authorization: Bearer prism-sk-search-1a2b3c"        # cache on (0.92), fast only
R="Authorization: Bearer prism-sk-research-4d5e6f"       # cache off, the only key with `auto`
F="Authorization: Bearer prism-sk-free-7g8h9i"           # 10 rpm, for the burst
B="Authorization: Bearer prism-sk-budget-demo-0j1k2l"    # $0.00001 budget
A="Authorization: Bearer $PRISM_ADMIN_TOKEN"
J="Content-Type: application/json"
export S R F B A J

# -- request helpers ---------------------------------------------------------
body() { printf '{"model":"%s","messages":[{"role":"user","content":"%s"}]}' "$1" "$2"; }

# Status line and the four x-prism headers, nothing else. The workhorse.
ask() {
    curl -s -D- -o /dev/null localhost:8080/v1/chat/completions \
        -H "$1" -H "$J" -d "$(body "$2" "$3")" | grep -iE "^(HTTP/|x-prism)"
}

# Status, Retry-After, and the error body — for the 429 and 402 steps.
err() {
    curl -s -i localhost:8080/v1/chat/completions \
        -H "$1" -H "$J" -d "$(body "$2" "$3")" | grep -iE "^(HTTP/|retry-after|\{)"
}

# SSE with a millisecond timestamp per line. The timestamps are the point: a
# 24-chunk reply at ~20 ms per chunk is half a second of wall time, which is too
# fast to *see*. The gaps prove progressive arrival without slowing anything down.
stream() {
    curl -N -s localhost:8080/v1/chat/completions -H "$1" -H "$J" \
        -d "$(printf '{"model":"%s","stream":true,"messages":[{"role":"user","content":"%s"}]}' "$2" "$3")" \
        | while IFS= read -r l; do
            [ -n "$l" ] && printf '%s  %s\n' "$(date +%T.%3N)" "$l"
        done
}

# 15 sequential requests on a 10-rpm key: ten 200s, then clean 429s.
burst() {
    for i in $(seq 1 15); do
        curl -s -o /dev/null -w "%{http_code} " localhost:8080/v1/chat/completions \
            -H "$F" -H "$J" -d "$(body fast "capacity probe $i")"
    done
    echo
}

# -- failure injection on mock alpha ----------------------------------------
down() { curl -s -X POST 127.0.0.1:9001/admin/config -d '{"mode":"down"}'; echo; }
up()   { curl -s -X POST 127.0.0.1:9001/admin/config -d '{"mode":"ok"}';   echo; }
slow() { curl -s -X POST 127.0.0.1:9001/admin/config -d '{"latency_ms":3000}'; echo; }
flaky(){ curl -s -X POST 127.0.0.1:9001/admin/config -d '{"fail_rate":0.3}'; echo; }

# -- per-take reset ----------------------------------------------------------
# Run before every take. Scoped to budget-demo on purpose: the other three keys
# hold ~1,200 request_log rows that ARE the reconciliation evidence the console
# shows. Never widen the WHERE clause.
reset() {
    ./scripts/pg.sh psql -q \
        -c "delete from cache_entries;" \
        -c "delete from request_log where tenant_id = (select id from tenants where team='budget-demo');" \
        -c "update budget_periods set spent_usd = 0, request_count = 0 where tenant_id = (select id from tenants where team='budget-demo');"
    # All three fields, not just mode. The mock merges a partial POST rather than
    # replacing its config, so {"mode":"ok"} leaves a fail_rate or latency_ms from an
    # earlier drill in place — and then every request is 3 s late or randomly failing
    # while this function cheerfully reports both providers healthy.
    local healthy='{"mode":"ok","fail_rate":0.0,"latency_ms":0}'
    curl -s -X POST 127.0.0.1:9001/admin/config -d "$healthy" >/dev/null
    curl -s -X POST 127.0.0.1:9002/admin/config -d "$healthy" >/dev/null
    echo "reset: cache cleared, budget-demo zeroed, both providers fully healthy"
    echo "note:  the rate-limit window is in-memory — restart the gateway too if you ran burst"
}

# -- demo prompts ------------------------------------------------------------
P1="How do I reset my password on the dashboard?"
P2="What are the steps to reset my dashboard password?"
HARD="Prove that the square root of 2 is irrational."
EASY="Here is our on-call roster for the next two weeks: Monday - Priya, Tuesday - Chen, Wednesday - Amara, Thursday - Diego, Friday - Fatima, Saturday - Lukas, Sunday - Mei, next Monday - Tom, next Tuesday - Sara, next Wednesday - Ravi, next Thursday - Ana, next Friday - Kofi, next Saturday - Elif, next Sunday - Jonas. Who is on call this Thursday?"
export P1 P2 HARD EASY

echo "demo_env loaded. keys: S R F B A | helpers: ask err stream burst | drills: down up slow flaky | reset"
