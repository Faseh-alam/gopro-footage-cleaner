#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ==========================================
# 1. FLASK BACKEND SETUP
# ==========================================
VENV_PY="${ROOT}/.venv/bin/python"
PORT="${GOPRO_CLEANER_PORT:-8765}"
FLASK_URL="http://127.0.0.1:${PORT}"

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

# ==========================================
# 2. NODE.JS & VITE FRONTEND SETUP
# ==========================================
echo "Checking if Node.js is installed..."
if ! command -v node >/dev/null 2>&1; then
    echo ""
    echo "ERROR: Node.js is not found."
    echo "Please install Node.js via Homebrew (brew install node) or from https://nodejs.org/"
    echo "After installing, re-run this script."
    exit 1
fi

FRONTEND_DIR="${ROOT}/gopro_cleaner/frontend"

if [[ ! -d "${FRONTEND_DIR}" ]]; then
    echo ""
    echo "ERROR: Could not find frontend folder at: ${FRONTEND_DIR}"
    exit 1
fi

if [[ ! -d "${FRONTEND_DIR}/node_modules" ]]; then
    echo "Installing Vite dependencies (npm install)..."
    # Run npm install inside the frontend directory
    (cd "${FRONTEND_DIR}" && npm install)
else
    echo "node_modules already exists. Skipping npm install..."
fi

# ==========================================
# 3. STARTING BOTH SERVERS & BROWSER
# ==========================================
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

# Wait for Flask backend to be ready
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
# Opens the browser and prints: "Vite ready on port N; opening http://localhost:N/review"
VITE_LINE="$("${VENV_PY}" "${ROOT}/scripts/wait_open_vite.py")"
echo "${VITE_LINE}"
VITE_URL="$(printf '%s\n' "${VITE_LINE}" | sed -n 's/.*opening //p')"
if [[ -z "${VITE_URL}" ]]; then
  VITE_URL="(auto-detected)"
fi

echo ""
echo "========================================================"
echo " GoPro Footage Cleaner is running!"
echo " Frontend (Vite):  ${VITE_URL}"
echo " Backend (Flask):  ${FLASK_URL}"
echo " Press Ctrl+C to stop both servers."
echo "========================================================"
echo ""

# Wait for both background processes so the script stays alive
wait "${FLASK_PID}" "${VITE_PID}"
