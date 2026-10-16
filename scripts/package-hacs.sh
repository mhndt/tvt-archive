#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
find . -type d -name __pycache__ -prune -exec rm -rf {} +
find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
python3 scripts/check-version.py
HLS_JS_VERSION=1.6.16
HLS_JS_SHA512=552211a4b7d1c250007462f8c224ee731d927118a9a3479dd4505ab56932b7cdf68c2e0245e2acb606bac888585701aef4b3818ee5fdc3399131e41d049b7310
HLS_JS=custom_components/tvt_archive/frontend/hls.min.js
if [ ! -s "$HLS_JS" ]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "https://registry.npmjs.org/hls.js/-/hls.js-${HLS_JS_VERSION}.tgz" -o "$tmp/hls.tgz"
  echo "${HLS_JS_SHA512}  $tmp/hls.tgz" | sha512sum -c - >/dev/null
  tar -xzf "$tmp/hls.tgz" -C "$tmp" package/dist/hls.min.js
  install -m 0644 "$tmp/package/dist/hls.min.js" "$HLS_JS"
fi
rm -rf dist
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
        relative = path.relative_to(source).as_posix()
        info = zipfile.ZipInfo(relative, fixed_time)
        mode = 0o755 if os.access(path, os.X_OK) else 0o644
        info.external_attr = mode << 16
        archive.writestr(info, path.read_bytes())

with zipfile.ZipFile(output, "r") as archive:
    names = set(archive.namelist())
if "manifest.json" not in names: raise SystemExit("HACS package is missing manifest.json at the ZIP root")
if any(name.startswith("custom_components/") for name in names): raise SystemExit("HACS package incorrectly contains a custom_components/ prefix")
if "frontend/hls.min.js" not in names: raise SystemExit("HACS package is missing frontend/hls.min.js")
if "brand/icon.png" not in names or "brand/logo.png" not in names: raise SystemExit("HACS package is missing local integration brand assets")

digest = hashlib.sha256(output.read_bytes()).hexdigest()
(root / "dist" / "SHA256SUMS.txt").write_text(
    f"{digest}  {output.name}\n", encoding="utf-8"
)
print(output)
PY
