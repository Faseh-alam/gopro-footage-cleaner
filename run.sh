#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VENV_PY="${ROOT}/.venv/bin/python"
PORT="${GOPRO_CLEANER_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing dependencies..."
"${VENV_PY}" -m pip install -q --upgrade pip
"${VENV_PY}" -m pip install -q -r requirements.txt

export PYTHONPATH="${ROOT}"

echo "Ensuring FFmpeg (system install, or download via static-ffmpeg)..."
"${VENV_PY}" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"

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

echo "Starting GoPro Footage Cleaner on port ${PORT}..."
"${VENV_PY}" -m gopro_cleaner &
APP_PID=$!

cleanup() {
  kill "${APP_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..30}; do
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
echo ""
echo "GoPro Footage Cleaner is running at ${URL}/review"
echo "Press Ctrl+C to stop."
echo ""

wait "${APP_PID}"
