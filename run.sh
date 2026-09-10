#!/usr/bin/env bash
set -euo pipefail

# Production launcher: Flask serves API + built UI from gopro_cleaner/web
# (no Vite / Node required). For local UI development use run.dev.sh.
#
# When this folder lives on a USB (FAT/exFAT), Python venvs break because
# those filesystems cannot store symlinks. We then create the venv on the
# Mac/Linux home disk instead and still run the app from the USB path.

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${GOPRO_CLEANER_PORT:-8765}"
START_PATH="${GOPRO_START_PATH:-/review}"
APP_NAME="${GOPRO_APP_NAME:-GoPro Footage Cleaner}"
APP_URL="http://127.0.0.1:${PORT}${START_PATH}"
WEB_DIR="${ROOT}/gopro_cleaner/web"
FRONTEND_DIR="${ROOT}/gopro_cleaner/frontend"

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "ERROR: Python 3 is not installed (or not on PATH)."
  echo "Intel Mac: install from https://www.python.org/downloads/macos/"
  echo "Then re-run this launcher."
  echo ""
  read -r -p "Press Enter to close…" _
  exit 1
fi

needs_local_venv() {
  # USB mounts on macOS
  case "${ROOT}" in
    /Volumes/*) return 0 ;;
  esac
  # Symlink probe — FAT/MSDOS/exFAT usually fail this
  local probe="${ROOT}/.wc_symlink_probe_$$"
  if ln -s /tmp "${probe}" 2>/dev/null; then
    rm -f "${probe}"
    return 1
  fi
  return 0
}

pick_venv_dir() {
  if needs_local_venv; then
    local hash
    hash="$(printf '%s' "${ROOT}" | /usr/bin/shasum -a 256 2>/dev/null | awk '{print substr($1,1,12)}')"
    if [[ -z "${hash}" ]]; then
      hash="$(printf '%s' "${ROOT}" | cksum | awk '{print $1}')"
    fi
    if [[ "$(uname -s)" == "Darwin" ]]; then
      echo "${HOME}/Library/Application Support/WorldContext/VoiceoverStation/venv-${hash}"
    else
      echo "${HOME}/.cache/worldcontext/voiceover-station/venv-${hash}"
    fi
  else
    echo "${ROOT}/.venv"
  fi
}

VENV_DIR="$(pick_venv_dir)"
VENV_PY="${VENV_DIR}/bin/python"

venv_ok() {
  [[ -x "${VENV_PY}" ]] || return 1
  "${VENV_PY}" -V >/dev/null 2>&1
}

if ! venv_ok; then
  echo "Creating virtual environment..."
  echo "  (location: ${VENV_DIR})"
  # Remove broken/partial venv (common after a failed create on USB FAT).
  rm -rf "${VENV_DIR}"
  # Also clear a broken on-USB .venv left by older launchers.
  if [[ "${VENV_DIR}" != "${ROOT}/.venv" && -e "${ROOT}/.venv" ]]; then
    rm -rf "${ROOT}/.venv" 2>/dev/null || true
  fi
  mkdir -p "$(dirname "${VENV_DIR}")"
  # --copies avoids symlink requirements if we ever land on awkward FS.
  if ! python3 -m venv --copies "${VENV_DIR}"; then
    python3 -m venv "${VENV_DIR}"
  fi
fi

if ! venv_ok; then
  echo ""
  echo "ERROR: Could not create a working Python environment at:"
  echo "  ${VENV_DIR}"
  echo "Install Python 3.10+ from https://www.python.org/downloads/macos/ and retry."
  echo ""
  read -r -p "Press Enter to close…" _
  exit 1
fi

echo "Installing Python dependencies..."
# Prefer binary wheels so students never compile cryptography / OpenSSL.
"${VENV_PY}" -m pip install -q --upgrade pip
"${VENV_PY}" -m pip install -q --only-binary=:all: -r "${ROOT}/requirements.txt" \
  || "${VENV_PY}" -m pip install -q -r "${ROOT}/requirements.txt"

export PYTHONPATH="${ROOT}"
export GOPRO_LITE_MODE=1

echo "Ensuring FFmpeg (system install, or download via static-ffmpeg)..."
"${VENV_PY}" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"

if [[ ! -f "${WEB_DIR}/index.html" && ! -f "${WEB_DIR}/_shell.html" ]]; then
  if [[ ! -f "${FRONTEND_DIR}/package.json" ]]; then
    echo ""
    echo "ERROR: No UI build found at gopro_cleaner/web"
    echo "Restore frontend source and run: (cd gopro_cleaner/frontend && npm install && npm run build:flask)"
    read -r -p "Press Enter to close…" _
    exit 1
  fi
  if ! command -v node >/dev/null 2>&1; then
    echo ""
    echo "ERROR: UI build missing and Node.js is not installed to build it."
    read -r -p "Press Enter to close…" _
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
    read -r -p "Press Enter to close…" _
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "Server did not become ready on port ${PORT}."
  read -r -p "Press Enter to close…" _
  exit 1
fi

if [[ -z "${GOPRO_NO_BROWSER:-}" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "${APP_URL}" || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${APP_URL}" || true
  fi
fi

echo ""
echo "========================================================"
echo " ${APP_NAME} is running!"
echo " App:  ${APP_URL}"
echo " API:  http://127.0.0.1:${PORT}/api/health"
echo " Press Ctrl+C to stop."
echo "========================================================"
echo ""

wait "${FLASK_PID}"
