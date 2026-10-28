"""The ops console: one server-rendered page over the same queries as `/admin/*`.

`docs/PRISM_PROBLEM_STATEMENT.md:170` asks for "a simple page" showing usage by key,
recent requests and cache hit rate. This is that page, and the emphasis is on
*simple* in a specific sense: it is one HTTP request, one database round of queries,
no client-side JavaScript, and no asset it cannot serve itself.

**Why no JavaScript and no CDN.** A console that fetches `/admin/usage` from the
browser needs the admin token in client-side code, which means the token is in the
page source, in `fetch` history and in the devtools network log — for a page whose
whole purpose is to display secrets-adjacent data. Rendering server-side keeps the
credential in the request that asked for it. A CDN stylesheet would additionally
make an offline machine render an unstyled page and a firewalled one hang, so the CSS
is inline and there are no external references of any kind.

**Why it calls `prism/usage.py` and not its own HTTP client.** The console is a
second consumer of the same aggregation, and `prism/api/admin.py:3` was written that
way on purpose. Going out over HTTP to reach code in the same process would add a
second place for the admin token to live, turn one page load into six authenticated
requests, and make the console's numbers capable of disagreeing with the API's for
timing reasons alone. The rule those two modules share: aggregation lives in
`usage.py`, and a route only renders.

**Why it is not in `admin.py`.** Same data, different contract. `/admin/*` is a JSON
API with stable field names that a client parses; this is HTML for a human, free to
round, re-order and drop columns. Keeping them in one file would eventually mean one
of those two promises constraining the other.

**Rendering by hand rather than with a template engine.** Jinja2 is not a dependency
of this project and adding one for a single page is a poor trade, so the helpers below
build the HTML directly. That makes escaping a correctness requirement rather than a
framework default: every value that reaches the page goes through `_esc`, including
team names, which come from the database and are therefore attacker-influenced in any
deployment where tenants are self-registered. `docs/DATA_MODEL.md:44` also applies
here — the console renders key *prefixes*, never key material.
"""

from __future__ import annotations

import datetime as dt
import html
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from prism import usage
from prism.auth import require_console
from prism.db.models import RequestLog
from prism.deps import ConfigDep, SessionDep
from prism.money import format_usd, quantize_usd

router = APIRouter(
    tags=["console"],
    dependencies=[Depends(require_console)],
)

#: Rows in the "recent requests" table. Small on purpose: the console has no
#: pagination, and a page that renders 500 rows is a page nobody scrolls to the end
#: of. `/admin/logs?limit=` is the answer for anyone who wants more.
RECENT_LIMIT = 25

#: Deliberately not `usage.DEFAULT_LOG_LIMIT`. That constant is the JSON API's
#: default page size; coupling the two would mean changing the API's paging to
#: change the length of an HTML table.
assert RECENT_LIMIT <= usage.MAX_LOG_LIMIT

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem 1.75rem 3rem;
  font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
  background: #fbfbfd; color: #16181d;
}
h1 { font-size: 1.15rem; margin: 0 0 .15rem; letter-spacing: -.01em; }
h2 { font-size: .82rem; text-transform: uppercase; letter-spacing: .07em;
     color: #5b6270; margin: 2rem 0 .6rem; font-weight: 600; }
.sub { color: #6b7280; font-size: .82rem; margin: 0 0 .25rem; }
table { border-collapse: collapse; width: 100%; background: #fff;
        border: 1px solid #e3e5ea; border-radius: 7px; overflow: hidden; }
th, td { padding: .42rem .6rem; text-align: left; border-bottom: 1px solid #eef0f3;
         white-space: nowrap; }
th { background: #f5f6f8; font-weight: 600; font-size: .76rem;
     text-transform: uppercase; letter-spacing: .04em; color: #5b6270; }
tr:last-child td { border-bottom: none; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
td.mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
          font-size: .8rem; color: #454b57; }
.cards { display: flex; flex-wrap: wrap; gap: .7rem; margin: .9rem 0 0; }
.card { background: #fff; border: 1px solid #e3e5ea; border-radius: 7px;
        padding: .6rem .85rem; min-width: 8.5rem; }
.card .k { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
           color: #6b7280; }
.card .v { font-size: 1.3rem; font-variant-numeric: tabular-nums;
           letter-spacing: -.02em; margin-top: .1rem; }
.pill { display: inline-block; padding: .04rem .42rem; border-radius: 999px;
        font-size: .74rem; font-weight: 600; border: 1px solid transparent; }
.ok   { background: #e7f6ec; color: #14663a; border-color: #c3e6cf; }
.warn { background: #fdf3d8; color: #7a5307; border-color: #f2e0a8; }
.bad  { background: #fdeaea; color: #8a1c1c; border-color: #f4c6c6; }
.mute { background: #f1f2f5; color: #626873; border-color: #e0e2e8; }
.empty { color: #6b7280; font-style: italic; padding: .7rem .1rem; }
footer { margin-top: 2.5rem; color: #8b909b; font-size: .76rem; }
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #e6e8ec; }
  table, .card { background: #1c1f25; border-color: #2b2f37; }
  th { background: #22262d; color: #9aa1ae; }
  th, td { border-bottom-color: #262a31; }
  td.mono { color: #aab1bd; }
  .sub, .card .k, footer, .empty { color: #8b919d; }
  .ok { background: #14301f; color: #7fd1a0; border-color: #225034; }
  .warn { background: #332a10; color: #e0be6a; border-color: #4d3f17; }
  .bad { background: #35191a; color: #e59a9a; border-color: #4e2224; }
  .mute { background: #23262c; color: #9aa1ae; border-color: #2e323a; }
}
"""


def _esc(value: object) -> str:
    """Escape anything for HTML text, rendering None as an em dash.

    `None` is common and meaningful here — a rejected request has no provider, a
    provider with no traffic has no p95 — and printing the string "None" in a table
    reads as a bug in the gateway rather than as an absence.
    """
    return "&mdash;" if value is None else html.escape(str(value), quote=True)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _usd(amount: Decimal) -> str:
    """Money for a human: fixed places, no scientific notation.

    Not `format_usd`, whose ten decimal places are right for a per-request header a
    spreadsheet will parse and wrong for a column an operator scans. Per-request
    costs in the log table *do* use `format_usd`, so they can be diffed against the
    `x-prism-cost-usd` header the client saw.
    """
    return f"${quantize_usd(amount):.6f}"


def _threshold(value: Decimal | None) -> str:
    """A similarity threshold, at the precision it was configured with.

    The column stores `numeric(4, 2)`, so a raw render prints `0.8500` — four decimal
    places implying a precision the seed file never expressed, next to a hit rate that
    is shown to one. Trailing zeros dropped, not rounded away: a threshold is a
    configured value and the page should show the configured value.
    """
    return "&mdash;" if value is None else f"{value.normalize():f}"


def _card(label: str, value: str) -> str:
    return f'<div class="card"><div class="k">{_esc(label)}</div><div class="v">{value}</div></div>'


def _table(headers: list[tuple[str, str]], rows: list[list[str]], *, empty: str) -> str:
    """A table, or a sentence explaining why there is nothing to show.

    An empty `<table>` with headers and no body looks like a failed query. Saying
    "no traffic yet" is the difference between a console an operator trusts and one
    they refresh hoping for different output.
    """
    if not rows:
        return f'<p class="empty">{_esc(empty)}</p>'
    head = "".join(f'<th class="{cls}">{_esc(text)}</th>' for text, cls in headers)
    # strict=True on purpose: a row that has grown a cell the header list does not know
    # about should raise here, not silently render one column short.
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{cls}">{cell}</td>'
            for cell, (_, cls) in zip(row, headers, strict=True)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


#: `request_log.status` to a pill class. Anything unrecognised falls back to neutral
#: rather than to an alarming colour: a status this map has not learned about yet is
#: an out-of-date console, not a failing request.
_STATUS_CLASS = {status: "ok" for status in usage.SERVED_STATUSES} | (
    {status: "warn" for status in usage.REJECTED_STATUSES}
    | {status: "bad" for status in usage.FAILED_STATUSES}
)


def _status_pill(status: str) -> str:
    return f'<span class="pill {_STATUS_CLASS.get(status, "mute")}">{_esc(status)}</span>'


def _health_pill(row: usage.ProviderHealth) -> str:
    cls = {"healthy": "ok", "degraded": "bad", "unknown": "mute"}[row.status]
    return f'<span class="pill {cls}">{_esc(row.status)}</span>'


def _budget_pill(state: usage.KeyState) -> str:
    """How close this key is to its ceiling.

    The threshold is 80%, which is a display choice and nothing else — the gateway
    enforces at 100% in `prism/budget.py` and this pill has no effect on admission.
    """
    if state.monthly_budget_usd <= 0:
        return '<span class="pill mute">no budget</span>'
    used = float(state.spent_usd / state.monthly_budget_usd)
    cls = "bad" if used >= 1 else "warn" if used >= 0.8 else "ok"
    return f'<span class="pill {cls}">{_pct(used)}</span>'


def _bool_pill(value: bool, *, true: str, false: str) -> str:
    cls, text = ("ok", true) if value else ("mute", false)
    return f'<span class="pill {cls}">{_esc(text)}</span>'


def _usage_table(rows: list[usage.Usage]) -> str:
    return _table(
        [
            ("Team", ""),
            ("Key", "mono"),
            ("Requests", "n"),
            ("Served", "n"),
            ("Rejected", "n"),
            ("Failed", "n"),
            ("Cache hits", "n"),
            ("Prompt", "n"),
            ("Completion", "n"),
            ("Cost", "n"),
        ],
        [
            [
                _esc(row.team),
                _esc(row.key),
                f"{row.requests:,}",
                f"{row.served:,}",
                f"{row.rejected:,}",
                f"{row.failed:,}",
                f"{row.cache_hits:,}",
                f"{row.prompt_tokens:,}",
                f"{row.completion_tokens:,}",
                _usd(row.cost_usd),
            ]
            for row in rows
        ],
        empty="No requests in this window.",
    )


def _keys_table(states: list[usage.KeyState]) -> str:
    return _table(
        [
            ("Team", ""),
            ("Key", "mono"),
            ("Status", ""),
            ("Budget", "n"),
            ("Spent", "n"),
            ("Used", ""),
            ("Reconciles", ""),
            ("Rpm", "n"),
            ("Tpm", "n"),
            ("Cache", ""),
            ("Models", "mono"),
        ],
        [
            [
                _esc(state.team),
                _esc(state.key),
                _esc(state.status),
                _usd(state.monthly_budget_usd),
                _usd(state.spent_usd),
                _budget_pill(state),
                # Two independent paths to this month's spend disagreeing is the one
                # thing on this page that means "stop and investigate", so it gets a
                # column of its own rather than being left to whoever reads the JSON.
                _bool_pill(state.reconciles, true="yes", false="NO"),
                f"{state.requests_per_minute:,}",
                f"{state.tokens_per_minute:,}",
                _bool_pill(state.cache_enabled, true="on", false="off")
                + (
                    f" {_threshold(state.similarity_threshold)}"
                    if state.cache_enabled and state.similarity_threshold is not None
                    else ""
                ),
                _esc(", ".join(state.model_allowlist)),
            ]
            for state in states
        ],
        empty="No keys are seeded. Run scripts/init_db.py.",
    )


def _cache_table(rows: list[usage.CacheSavings]) -> str:
    return _table(
        [
            ("Team", ""),
            ("Enabled", ""),
            ("Threshold", "n"),
            ("Lookups", "n"),
            ("Hits", "n"),
            ("Hit rate", "n"),
            ("Entries", "n"),
            ("Tokens saved", "n"),
            ("Cost saved", "n"),
        ],
        [
            [
                _esc(row.team),
                _bool_pill(row.cache_enabled, true="on", false="off"),
                _threshold(row.similarity_threshold),
                f"{row.lookups:,}",
                f"{row.hits:,}",
                _pct(row.hit_rate),
                f"{row.entries:,}",
                f"{row.tokens_saved:,}",
                _usd(row.cost_saved_usd),
            ]
            for row in rows
        ],
        empty="No key has consulted the cache in this window.",
    )


def _health_table(rows: list[usage.ProviderHealth]) -> str:
    return _table(
        [
            ("Provider", ""),
            ("Status", ""),
            ("Requests", "n"),
            ("Errors", "n"),
            ("Error rate", "n"),
            ("Retries", "n"),
            ("Fallbacks", "n"),
            ("Avg ms", "n"),
            ("p95 ms", "n"),
            ("Last seen (UTC)", "mono"),
        ],
        [
            [
                _esc(row.provider),
                _health_pill(row),
                f"{row.requests:,}",
                f"{row.errors:,}",
                _pct(row.error_rate),
                f"{row.retries:,}",
                f"{row.fallbacks:,}",
                _esc(row.avg_latency_ms),
                _esc(row.p95_latency_ms),
                _esc(
                    row.last_seen.astimezone(dt.UTC).strftime("%H:%M:%S")
                    if row.last_seen
                    else None
                ),
            ]
            for row in rows
        ],
        empty="No provider has been called yet.",
    )


def _log_table(rows: list[RequestLog]) -> str:
    return _table(
        [
            # Labelled, and carrying the date. `created_at` is stored tz-aware in UTC,
            # so a bare `%H:%M:%S` is correct and still misleading: an operator reading
            # it in any other zone sees a number several hours off their own clock and
            # concludes the gateway's timestamps are broken. The date is there because
            # the window can span a month, and a day-old row that renders as a time
            # alone looks like it just happened.
            ("Time (UTC)", "mono"),
            ("Team", ""),
            ("Requested", ""),
            ("Served by", ""),
            ("Status", ""),
            ("HTTP", "n"),
            ("Cache", ""),
            ("Fallback", ""),
            ("Retries", "n"),
            ("Tokens", "n"),
            ("Cost", "n"),
            ("Latency", "n"),
        ],
        [
            [
                _esc(row.created_at.astimezone(dt.UTC).strftime("%m-%d %H:%M:%S")),
                _esc(row.team),
                _esc(row.requested_model),
                # A cache hit has no resolved provider — the answer came from the
                # database. `prism/api/chat.py` still names the *originating* provider
                # in the header; the log row is honest that nothing was called.
                _esc(
                    f"{row.resolved_provider}/{row.resolved_model}"
                    if row.resolved_provider
                    else None
                ),
                _status_pill(row.status),
                _esc(row.http_status),
                _esc(row.cache),
                "yes" if row.fallback else "&mdash;",
                _esc(row.retries),
                f"{row.prompt_tokens + row.completion_tokens:,}",
                f"${format_usd(row.cost_usd)}",
                f"{row.latency_ms:,} ms" if row.latency_ms is not None else "&mdash;",
            ]
            for row in rows
        ],
        empty="No requests logged yet. Send one to /v1/chat/completions.",
    )


@router.get("/console", response_class=HTMLResponse)
async def console(
    session: SessionDep,
    config: ConfigDep,
    from_: Annotated[
        dt.date | None,
        Query(alias="from", description="First UTC day to include. Defaults to the 1st."),
    ] = None,
    to: Annotated[
        dt.date | None,
        Query(description="Last UTC day to include, inclusive. Defaults to today."),
    ] = None,
) -> HTMLResponse:
    """The whole console, in one response.

    The window defaults to the current budget period, the same as `/admin/usage`, so
    the totals here can be compared against a tenant's monthly budget without
    arithmetic. Provider health is the exception and is *not* windowed by those
    dates: it reports the configured `degradation.window_seconds`, because "is alpha
    healthy" asked about a month-long window is a question with no useful answer.
    """
    window = usage.resolve_window(from_, to)
    rows = await usage.usage_by_key(session, window=window, tenant_id=None)
    total = usage.combine(rows)
    caches = await usage.cache_savings(session, config, window=window, tenant_id=None)
    cache_total = usage.combine_cache_savings(caches)
    health = await usage.provider_health(session, config)
    states = await usage.key_states(session, tenant_id=None)
    logs = await usage.recent_logs(session, limit=RECENT_LIMIT, tenant_id=None)

    unreconciled = [state for state in states if not state.reconciles]
    banner = (
        ""
        if not unreconciled
        else '<p class="sub"><span class="pill bad">reconciliation</span> '
        + _esc(
            f"{len(unreconciled)} key(s) whose recorded spend disagrees with the sum of "
            "their logged request costs: "
            + ", ".join(state.team for state in unreconciled)
        )
        + "</p>"
    )

    return HTMLResponse(
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Prism ops console</title>"
        f"<style>{STYLE}</style>"
        "</head><body>"
        "<h1>Prism ops console</h1>"
        f'<p class="sub">{_esc(window.from_date)} to {_esc(window.to_date)} (UTC, inclusive) '
        f"&middot; {_esc(len(config.provider_names))} providers &middot; "
        f"{_esc(len(config.alias_names))} aliases</p>"
        f"{banner}"
        '<div class="cards">'
        f'{_card("Requests", f"{total.requests:,}")}'
        f'{_card("Served", f"{total.served:,}")}'
        f'{_card("Rejected", f"{total.rejected:,}")}'
        f'{_card("Failed", f"{total.failed:,}")}'
        f'{_card("Cache hit rate", _pct(cache_total.hit_rate))}'
        f'{_card("Tokens", f"{total.total_tokens:,}")}'
        f'{_card("Cost", _usd(total.cost_usd))}'
        f'{_card("Saved by cache", _usd(cache_total.cost_saved_usd))}'
        "</div>"
        f"<h2>Usage by key</h2>{_usage_table(rows)}"
        f"<h2>Keys, budgets and limits</h2>{_keys_table(states)}"
        f"<h2>Semantic cache</h2>{_cache_table(caches)}"
        f"<h2>Provider health &middot; last {_esc(config.degradation.window_seconds)}s"
        f"</h2>{_health_table(health)}"
        f"<h2>Recent requests &middot; newest {RECENT_LIMIT}</h2>{_log_table(logs)}"
        "<footer>Read-only. Observed, not enforced: provider health has no circuit "
        "breaker behind it. Refresh to update &mdash; this page does not poll."
        "</footer>"
        "</body></html>"
    )
