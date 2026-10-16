#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYCACHE="$(mktemp -d)"
trap 'rm -rf "$PYCACHE"' EXIT
export PYTHONPYCACHEPREFIX="$PYCACHE"
python3 -m py_compile "$ROOT/custom_components/tvt_archive/bridge/bridge.py" "$ROOT/custom_components/tvt_archive/bridge/native9008.py" "$ROOT/custom_components/tvt_archive/bridge/archive_capture.py"
find "$ROOT/custom_components/tvt_archive" -type f -name '*.py' -print0 | xargs -0 python3 -m py_compile
if command -v node >/dev/null 2>&1; then
  node --check "$ROOT/custom_components/tvt_archive/frontend/tvt-archive-panel.js"
fi
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
