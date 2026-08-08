#!/usr/bin/env bash
set -euo pipefail

# Production launcher: Flask serves API + built UI from gopro_cleaner/web
# (no Vite / Node required). For local UI development use run.dev.sh.

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VENV_PY="${ROOT}/.venv/bin/python"
PORT="${GOPRO_CLEANER_PORT:-8765}"
APP_URL="http://127.0.0.1:${PORT}/review"
WEB_DIR="${ROOT}/gopro_cleaner/web"
FRONTEND_DIR="${ROOT}/gopro_cleaner/frontend"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
"${VENV_PY}" -m pip install -q --upgrade pip
"${VENV_PY}" -m pip install -q -r requirements.txt

export PYTHONPATH="${ROOT}"
export GOPRO_LITE_MODE=1

echo "Ensuring FFmpeg (system install, or download via static-ffmpeg)..."
"${VENV_PY}" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"

if [[ ! -f "${WEB_DIR}/index.html" && ! -f "${WEB_DIR}/_shell.html" ]]; then
  if [[ ! -f "${FRONTEND_DIR}/package.json" ]]; then
    echo ""
    echo "ERROR: No UI build found at gopro_cleaner/web"
    echo "Restore frontend source and run: (cd gopro_cleaner/frontend && npm install && npm run build:flask)"
    exit 1
  fi
  if ! command -v node >/dev/null 2>&1; then
    echo ""
    echo "ERROR: UI build missing and Node.js is not installed to build it."
    exit 1
  fi
  echo "Building UI for Flask (npm run build:flask)..."
  (
    cd "${FRONTEND_DIR}"
    [[ -d node_modules ]] || npm install
    npm run build:flask
  )
fi

echo "Using UI from gopro_cleaner/web"

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

echo "Starting Flask (API + UI) on port ${PORT}..."
"${VENV_PY}" -m gopro_cleaner &
FLASK_PID=$!

cleanup() {
  echo ""
  echo "Stopping server..."
  kill "${FLASK_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..40}; do
  if ! kill -0 "${FLASK_PID}" 2>/dev/null; then
    echo "Failed to start GoPro Footage Cleaner."
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "Server did not become ready on port ${PORT}."
  exit 1
fi

if command -v open >/dev/null 2>&1; then
  open "${APP_URL}" || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "${APP_URL}" || true
fi

echo ""
echo "========================================================"
echo " GoPro Footage Cleaner is running!"
echo " App:  ${APP_URL}"
echo " API:  http://127.0.0.1:${PORT}/api/health"
echo " Press Ctrl+C to stop."
echo "========================================================"
echo ""

wait "${FLASK_PID}"
