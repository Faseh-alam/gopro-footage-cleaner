#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi
if [[ $# -eq 0 ]]; then
  echo "Voiceover USB packer"
  echo "  plan        split footage across 10 sticks"
  echo "  show        see assignment"
  echo "  fill        copy onto ALL plugged USBs in parallel"
  echo "  update-app  replace VoiceoverStation only; keep footage"
  echo ""
  echo "Example:  ./start-pack-usbs.sh update-app"
  exit 1
fi
"$PY" -m pack_voiceover_usbs "$@"
