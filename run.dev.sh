#!/usr/bin/env bash
set -euo pipefail

# Development launcher: Vite hot-reload UI + Flask API.
# For production/colleague workflow use run.sh instead.

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VENV_PY="${ROOT}/.venv/bin/python"
PORT="${GOPRO_CLEANER_PORT:-8765}"
FLASK_URL="http://127.0.0.1:${PORT}"
FRONTEND_DIR="${ROOT}/gopro_cleaner/frontend"

if [[ ! -f "${FRONTEND_DIR}/package.json" ]]; then
  echo ""
  echo "ERROR: Frontend source not found at: ${FRONTEND_DIR}"
  echo "For production (no Node) use ./run.sh instead."
  exit 1
fi

if [[ ! -x "${VENV_PY}" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
"${VENV_PY}" -m pip install -q --upgrade pip
"${VENV_PY}" -m pip install -q -r requirements.txt

export PYTHONPATH="${ROOT}"
export GOPRO_LITE_MODE=1

echo "Ensuring FFmpeg..."
"${VENV_PY}" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"

stop_existing() {
  local pids
  pids="$(lsof -ti :"${PORT}" 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "Stopping existing Flask server on port ${PORT}..."
    kill ${pids} 2>/dev/null || true
    sleep 1
  fi
}

stop_existing

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js is not found. Install Node.js (brew install node) and re-run."
  exit 1
fi

if [[ ! -d "${FRONTEND_DIR}/node_modules" ]]; then
  echo "Installing Vite dependencies (npm install)..."
  (cd "${FRONTEND_DIR}" && npm install)
else
  echo "node_modules already exists. Skipping npm install..."
fi

echo ""
echo "Starting React (Vite) Frontend..."
(cd "${FRONTEND_DIR}" && npm run dev) &
VITE_PID=$!

echo "Starting Flask Backend on port ${PORT}..."
"${VENV_PY}" -m gopro_cleaner &
FLASK_PID=$!

cleanup() {
  echo ""
  echo "Stopping servers..."
  kill "${VITE_PID}" 2>/dev/null || true
  kill "${FLASK_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..30}; do
  if ! kill -0 "${FLASK_PID}" 2>/dev/null; then
    echo "Failed to start GoPro Footage Cleaner (Flask)."
    exit 1
  fi
  if curl -fsS "${FLASK_URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "${FLASK_URL}/api/health" >/dev/null 2>&1; then
  echo "Flask Server did not become ready on port ${PORT}."
  exit 1
fi

echo "Waiting for Vite to become ready (auto-detect port)..."
VITE_LINE="$("${VENV_PY}" "${ROOT}/scripts/wait_open_vite.py")"
echo "${VITE_LINE}"
VITE_URL="$(printf '%s\n' "${VITE_LINE}" | sed -n 's/.*opening //p')"
if [[ -z "${VITE_URL}" ]]; then
  VITE_URL="(auto-detected)"
fi

echo ""
echo "========================================================"
echo " GoPro Footage Cleaner (dev) is running!"
echo " Frontend (Vite):  ${VITE_URL}"
echo " Backend (Flask):  ${FLASK_URL}"
echo " Press Ctrl+C to stop both servers."
echo "========================================================"
echo ""

wait "${FLASK_PID}" "${VITE_PID}"
