#!/usr/bin/env bash
set -euo pipefail

# One-click Voiceover Station for narrators (Mac / Linux).
# Double-click in Finder (or run: ./start-voiceover.sh)
# Opens http://127.0.0.1:8765/voiceover — no coding required.

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export GOPRO_START_PATH="${GOPRO_START_PATH:-/voiceover}"
export GOPRO_APP_NAME="${GOPRO_APP_NAME:-Voiceover Station}"

# Reuse the production launcher; it installs deps, FFmpeg, and starts Flask.
exec bash "${ROOT}/run.sh"
