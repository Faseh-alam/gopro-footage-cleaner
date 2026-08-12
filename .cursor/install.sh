#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for gopro-footage-cleaner (redesign).
# Prepares system packages, a shared Python virtualenv for both Flask apps
# (gopro_cleaner + sd_offloader), and the React/Vite frontend dependencies.
# Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# --- System packages ---------------------------------------------------------
# ffmpeg/ffprobe : lossless GoPro trimming (present on the default image).
# python3-tk     : native folder-picker fallback used on Linux/Windows.
# python3-venv   : provides ensurepip so the virtualenv gets pip.
NEEDED=()
command -v ffmpeg >/dev/null 2>&1 || NEEDED+=(ffmpeg)
python3 -c "import tkinter" >/dev/null 2>&1 || NEEDED+=(python3-tk)
python3 -c "import ensurepip" >/dev/null 2>&1 || NEEDED+=(python3-venv)
if [ "${#NEEDED[@]}" -gt 0 ]; then
  if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${NEEDED[@]}"
  else
    echo "WARN: missing packages [${NEEDED[*]}] and apt-get/sudo unavailable; continuing." >&2
  fi
fi

# --- Python virtualenv -------------------------------------------------------
# One venv shared by both apps; their dependency sets are compatible.
# Recreate if missing or if a previous run produced a venv without pip.
if [ ! -x "${ROOT}/.venv/bin/python" ] || ! "${ROOT}/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  rm -rf "${ROOT}/.venv"
  python3 -m venv "${ROOT}/.venv"
fi
VENV_PY="${ROOT}/.venv/bin/python"

"${VENV_PY}" -m pip install --quiet --upgrade pip
"${VENV_PY}" -m pip install --quiet -r "${ROOT}/requirements.txt"
"${VENV_PY}" -m pip install --quiet -r "${ROOT}/sd_offloader/requirements.txt"

# --- Verify ffmpeg resolves --------------------------------------------------
# Uses the system binary, otherwise fetches static-ffmpeg on first run.
PYTHONPATH="${ROOT}" "${VENV_PY}" - <<'PY'
from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg

status = ensure_ffmpeg(quiet=True)
print(f"ffmpeg ok={status.get('ok')} ffmpeg={status.get('ffmpeg')} ffprobe={status.get('ffprobe')}")
raise SystemExit(0 if status.get("ok") else 1)
PY

# --- React/Vite frontend -----------------------------------------------------
# Only needed for UI hot-reload dev; the committed build in gopro_cleaner/web
# lets Flask serve the production UI without Node. Load nvm's Node if present.
# Sourcing nvm and `nvm use` can return non-zero internally, so guard them
# from errexit; keep the actual npm install under strict mode.
export NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"
if [ -s "${NVM_DIR}/nvm.sh" ]; then
  set +e
  # shellcheck disable=SC1091
  . "${NVM_DIR}/nvm.sh"
  nvm use node >/dev/null 2>&1 || nvm use --lts >/dev/null 2>&1
  set -e
fi
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
    && [ -f "${ROOT}/gopro_cleaner/frontend/package.json" ]; then
  echo "Installing frontend dependencies with npm (node $(node --version), npm $(npm --version))..."
  ( cd "${ROOT}/gopro_cleaner/frontend" && npm install --no-audit --no-fund )
else
  echo "WARN: Node/npm unavailable; skipping Vite deps. The committed gopro_cleaner/web build still serves the UI." >&2
fi

echo "install.sh completed successfully."
