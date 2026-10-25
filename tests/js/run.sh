#!/usr/bin/env bash
#
# Run the dashboard's DOM tests against a real server.
#
#   bash tests/js/run.sh
#
# Starts uvicorn on a spare port if nothing is already listening, installs the
# jsdom dev dependency on first use, runs the suite, then tears the server down.
set -euo pipefail

PORT="${PORT:-8123}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON="${PY:-}"
if [ -z "$PYTHON" ]; then
  if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

if [ ! -d tests/js/node_modules ]; then
  echo "installing dashboard test dependencies…"
  npm --prefix tests/js install --silent
fi

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if curl -sf "http://127.0.0.1:${PORT}/livez" >/dev/null 2>&1; then
  echo "using the server already listening on :${PORT}"
else
  echo "starting the API on :${PORT}…"
  DASH_PROVIDER_MODE=mock "$PYTHON" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning &
  SERVER_PID=$!

  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${PORT}/livez" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done

  if ! curl -sf "http://127.0.0.1:${PORT}/livez" >/dev/null 2>&1; then
    echo "the API did not become healthy on :${PORT}" >&2
    exit 1
  fi
fi

DASHBOARD_URL="http://127.0.0.1:${PORT}" node tests/js/dashboard.test.mjs
