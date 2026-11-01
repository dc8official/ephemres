#!/usr/bin/env bash
# Throwaway TimescaleDB for the `timescale` integration tests.
#
#   tests/integration/timescale.sh start   # prints the export line for the DSN
#   tests/integration/timescale.sh stop
#
# The container is separate from any real Ephemres database, listens on 127.0.0.1 only
# and is removed when stopped.
set -euo pipefail

NAME="${EPHEMRES_IT_CONTAINER:-ephemres-it-db}"
PORT="${EPHEMRES_IT_PORT:-55432}"
IMAGE="${EPHEMRES_IT_IMAGE:-timescale/timescaledb:latest-pg15}"
DSN="postgresql://postgres@127.0.0.1:${PORT}/postgres"

case "${1:-}" in
  start)
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      docker run -d --rm --name "$NAME" \
        -e POSTGRES_HOST_AUTH_METHOD=trust \
        -p "127.0.0.1:${PORT}:5432" "$IMAGE" >/dev/null
    fi
    # The image runs a temporary server during first-time init and then restarts it, so wait
    # for the init-complete log line before trusting a successful query.
    for _ in $(seq 1 60); do
      if docker logs "$NAME" 2>&1 | grep -q 'PostgreSQL init process complete' \
        && docker exec "$NAME" psql -U postgres -c 'SELECT 1' >/dev/null 2>&1; then
        echo "export EPHEMRES_TEST_TIMESCALE_DSN=${DSN}"
        exit 0
      fi
      sleep 1
    done
    echo "TimescaleDB container '$NAME' did not become ready" >&2
    exit 1
    ;;
  stop)
    docker stop "$NAME" >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 2
    ;;
esac
