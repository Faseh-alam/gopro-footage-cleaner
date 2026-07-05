#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

export PYTHONPATH="$ROOT"
PORT="${GOPRO_CLEANER_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

stop_existing() {
  local pids
  pids="$(lsof -ti :"${PORT}" 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "Stopping existing server on port ${PORT}..."
    kill ${pids} 2>/dev/null || true
    sleep 1
  fi
}

stop_existing

python -m gopro_cleaner &
APP_PID=$!

cleanup() {
  kill "${APP_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..20}; do
  if ! kill -0 "${APP_PID}" 2>/dev/null; then
    echo "Failed to start GoPro Footage Cleaner."
    exit 1
  fi
  if curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
  echo "Server did not become ready on port ${PORT}."
  exit 1
fi

open "${URL}/review" 2>/dev/null || true
echo "GoPro Footage Cleaner is running at ${URL}"
echo "Press Ctrl+C to stop."

wait "${APP_PID}"
