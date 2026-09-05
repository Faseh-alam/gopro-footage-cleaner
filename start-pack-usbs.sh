#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi
echo "Voiceover USB packer"
echo "  1) plan       — split footage across 10 sticks"
echo "  2) show       — see assignment"
echo "  3) fill       — copy onto ALL plugged USBs in parallel"
echo "  4) update-app — replace VoiceoverStation only (keep footage)"
echo ""
"$PY" -m pack_voiceover_usbs "$@"
