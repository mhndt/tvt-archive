#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


version = read("VERSION").strip()
if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
    fail(f"VERSION is not a supported semantic version: {version!r}")
if len(sys.argv) > 1 and sys.argv[1] != version:
    fail(f"tag/release version {sys.argv[1]!r} does not match VERSION {version!r}")

manifest_path = ROOT / "custom_components/tvt_archive/manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
    if not manifest.get(key):
        fail(f"manifest.json is missing {key}")
if manifest["domain"] != "tvt_archive":
    fail("manifest domain must be tvt_archive")
if manifest["version"] != version:
    fail(f"manifest version {manifest['version']} does not match {version}")
if "@mhndt" not in manifest["codeowners"]:
    fail("manifest codeowners must include @mhndt")

hacs = json.loads(read("hacs.json"))
if hacs.get("name") != "TVT Archive":
    fail("hacs.json name is incorrect")
if hacs.get("zip_release") is not True or hacs.get("filename") != "tvt_archive.zip":
    fail("hacs.json must point to the release ZIP")

required_version_references = {
    "compose/compose.yaml": f"ghcr.io/mhndt/tvt-archive:{version}",
    ".env.example": f"ghcr.io/mhndt/tvt-archive:{version}",
    "compose/build-local.yaml": f"tvt-archive:{version}-local",
    "setup.sh": f"ghcr.io/mhndt/tvt-archive:{version}",
    "host/app/bridge.py": f'TVTArchiveBridge/{version}',
    "custom_components/tvt_archive/frontend/tvt-archive-panel.js": f'const VERSION = "{version}";',
}
for path, expected in required_version_references.items():
    if expected not in read(path):
        fail(f"{path} does not contain expected version reference {expected!r}")

component_root = ROOT / "custom_components"
components = sorted(p.name for p in component_root.iterdir() if p.is_dir())
if components != ["tvt_archive"]:
    fail(f"expected one custom integration, found: {components}")
if (component_root / "tvt_archive/strings.json").exists():
    fail("custom integration release should use translations/en.json, not strings.json")
for path in (
    "custom_components/tvt_archive/translations/en.json",
    "custom_components/tvt_archive/brand/icon.png",
    "custom_components/tvt_archive/brand/logo.png",
    "brand/icon.png",
    "README.md",
    "LICENSE",
    "THIRD_PARTY.md",
):
    if not (ROOT / path).is_file():
        fail(f"required file is missing: {path}")

for path in ROOT.rglob("*"):
    if path.is_dir() and path.name == "__pycache__":
        fail(f"generated Python cache directory is present: {path.relative_to(ROOT)}")
    if path.is_file() and path.suffix in {".pyc", ".pyo"}:
        fail(f"generated Python bytecode is present: {path.relative_to(ROOT)}")

forbidden_patterns = {
    "private SMB path": r"/srv/storage/",
    "private IPv4 range": r"192\.168\.",
}
text_suffixes = {"", ".md", ".txt", ".py", ".js", ".json", ".yaml", ".yml", ".sh", ".example"}
for path in ROOT.rglob("*"):
    if path.resolve() == Path(__file__).resolve():
        continue
    if not path.is_file() or path.suffix.lower() not in text_suffixes:
        continue
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for label, pattern in forbidden_patterns.items():
        if re.search(pattern, content):
            fail(f"{label} found in {path.relative_to(ROOT)}")

print(f"Repository checks passed for TVT Archive {version}.")
