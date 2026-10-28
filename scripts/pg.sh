#!/usr/bin/env bash
# Start/stop the local Postgres used for development.
#
# NOT part of the provided scaffold — this is mine. It exists so Postgres can run
# where Docker is unavailable: from EnterpriseDB's *binaries* zip unpacked into a
# user-owned directory, with no installer, no Windows service and no elevation.
# Reviewers with Docker should use `docker compose up -d` instead; see the README.
#
#   ./scripts/pg.sh start | stop | status | psql | logs
#
set -euo pipefail

PG_HOME="${PRISM_PG_HOME:-$LOCALAPPDATA/prism-postgres}"
PG_BIN="$PG_HOME/pgsql/bin"
PG_DATA="${PRISM_PG_DATA:-$PG_HOME/data}"
PG_LOG="$PG_HOME/server.log"
PG_PORT="${PRISM_PG_PORT:-5433}"
PG_USER="${PRISM_PG_USER:-prism}"
PG_DB="${PRISM_PG_DB:-prism}"

if [ ! -x "$PG_BIN/pg_ctl.exe" ] && [ ! -x "$PG_BIN/pg_ctl" ]; then
  echo "No Postgres binaries at $PG_BIN" >&2
  echo "See the README section 'Postgres without Docker' for the one-time setup." >&2
  exit 1
fi
PG_CTL="$PG_BIN/pg_ctl.exe"; [ -x "$PG_CTL" ] || PG_CTL="$PG_BIN/pg_ctl"
PSQL="$PG_BIN/psql.exe";     [ -x "$PSQL" ]   || PSQL="$PG_BIN/psql"

case "${1:-}" in
  start)
    # The redirects matter. Under Git Bash / MSYS, `pg_ctl start` inherits the
    # pipe and blocks forever even though the server itself has detached
    # successfully. Detaching stdio makes it return.
    "$PG_CTL" -D "$PG_DATA" -l "$PG_LOG" -o "-p $PG_PORT" -w start \
      </dev/null >/dev/null 2>&1 || true
    sleep 2
    if "$PG_CTL" -D "$PG_DATA" status >/dev/null 2>&1; then
      echo "Postgres up on port $PG_PORT (log: $PG_LOG)"
    else
      echo "Failed to start. Last log lines:" >&2
      tail -20 "$PG_LOG" >&2 || true
      exit 1
    fi
    ;;
  stop)
    "$PG_CTL" -D "$PG_DATA" -m fast -w stop </dev/null >/dev/null 2>&1 || true
    echo "Postgres stopped."
    ;;
  status)
    "$PG_CTL" -D "$PG_DATA" status || true
    ;;
  psql)
    shift || true
    PGPASSWORD="${PRISM_PG_PASSWORD:-prism}" \
      "$PSQL" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" "$@"
    ;;
  logs)
    tail -f "$PG_LOG"
    ;;
  *)
    echo "usage: $0 {start|stop|status|psql|logs}" >&2
    exit 2
    ;;
esac
