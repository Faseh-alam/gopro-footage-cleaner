#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for gopro-footage-cleaner.
# Prepares system packages and a shared virtualenv for both Flask apps
# (gopro_cleaner and sd_offloader). Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# --- System packages ---------------------------------------------------------
# ffmpeg/ffprobe : lossless GoPro trimming (present on the default image).
# python3-tk     : native folder-picker fallback used on Linux/Windows.
# python3-venv   : needed to create the virtualenv on Debian/Ubuntu.
NEEDED=()
command -v ffmpeg >/dev/null 2>&1 || NEEDED+=(ffmpeg)
python3 -c "import tkinter" >/dev/null 2>&1 || NEEDED+=(python3-tk)
# ensurepip (not just the venv module) is required so the virtualenv gets pip.
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

echo "install.sh completed successfully."
