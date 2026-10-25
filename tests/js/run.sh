#!/usr/bin/env bash
#
# Run the dashboard's DOM tests against a real server.
#
#   bash tests/js/run.sh              # picks .venv/bin/python, else python3
#   PY=/usr/bin/python3.12 bash tests/js/run.sh
#   PORT=9000 bash tests/js/run.sh
#
# Starts uvicorn on a spare port if nothing is already listening, installs the
# jsdom dev dependency on first use, runs the suite, then tears the server down.
set -euo pipefail

PORT="${PORT:-8123}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# ── Pick an interpreter that can actually import the app ────────────────────
# A relative path (or an interpreter missing fastapi) previously produced the
# useless message "the API did not become healthy". Fail here, explicitly.
resolve_python() {
  local candidate
  for candidate in "${PY:-}" ".venv/bin/python" "python3"; do
    [ -z "$candidate" ] && continue
    if ! command -v "$candidate" >/dev/null 2>&1 && [ ! -x "$candidate" ]; then
      continue
    fi
    if "$candidate" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
      # Absolute path: the server is launched after a `cd`, so a relative path
      # to a venv would silently stop resolving.
      "$candidate" -c "import sys; print(sys.executable)"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(resolve_python)"; then
  cat >&2 <<'MSG'
No Python interpreter with fastapi and uvicorn was found.

  python3 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt

or point the script at one that already has them:

  PY=/path/to/python bash tests/js/run.sh
MSG
  exit 1
fi

echo "python: $PYTHON"

# ── Node dependencies (gitignored, so install on first run) ────────────────
if [ ! -d tests/js/node_modules ]; then
  echo "installing dashboard test dependencies…"
  npm --prefix tests/js install --silent
fi

# ── Server lifetime ────────────────────────────────────────────────────────
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

BASE_URL="http://${DASHBOARD_HOST}:${PORT}"

if curl -sf "${BASE_URL}/livez" >/dev/null 2>&1; then
  echo "using the server already listening on :${PORT}"
else
  echo "starting the API on :${PORT}…"
  # WARNING keeps the run's own output readable: the app logs one JSON line per
  # request otherwise, which buries the test report in CI.
  DASH_PROVIDER_MODE=mock DASH_LOG_LEVEL=WARNING "$PYTHON" -m uvicorn app.main:app \
    --host "$DASHBOARD_HOST" --port "$PORT" --log-level warning &
  SERVER_PID=$!

  for _ in $(seq 1 60); do
    curl -sf "${BASE_URL}/livez" >/dev/null 2>&1 && break
    # Fail fast if uvicorn died (port in use, import error) instead of waiting
    # out the full 30 seconds.
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "the API process exited during startup" >&2
      exit 1
    fi
    sleep 0.5
  done

  if ! curl -sf "${BASE_URL}/livez" >/dev/null 2>&1; then
    echo "the API did not become healthy on :${PORT}" >&2
    exit 1
  fi
fi

DASHBOARD_URL="$BASE_URL" node tests/js/dashboard.test.mjs
