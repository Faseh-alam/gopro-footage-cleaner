#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${SD_OFFLOADER_PORT:-8877}"
VENV_PY=".venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing dependencies..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$VENV_PY" -m pip install -r requirements.txt

export PYTHONPATH="$(pwd)"

if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "${PIDS:-}" ]]; then
    kill $PIDS 2>/dev/null || true
  fi
fi

echo "Starting SD Card Offloader on port $PORT..."
(sleep 2 && open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true) &
"$VENV_PY" -m offloader
