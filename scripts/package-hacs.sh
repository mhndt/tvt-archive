#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
find . -type d -name __pycache__ -prune -exec rm -rf {} +
find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
python3 scripts/check-version.py
rm -rf dist
dir="custom_components/tvt_archive"
mkdir -p dist
python3 - "$ROOT" <<'PY'
from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path

root = Path(sys.argv[1])
source = root / "custom_components" / "tvt_archive"
output = root / "dist" / "tvt_archive.zip"
fixed_time = (2026, 1, 1, 0, 0, 0)
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(root).as_posix()
        info = zipfile.ZipInfo(relative, fixed_time)
        mode = 0o755 if os.access(path, os.X_OK) else 0o644
        info.external_attr = mode << 16
        archive.writestr(info, path.read_bytes())

digest = hashlib.sha256(output.read_bytes()).hexdigest()
(root / "dist" / "SHA256SUMS.txt").write_text(
    f"{digest}  {output.name}\n", encoding="utf-8"
)
print(output)
PY
