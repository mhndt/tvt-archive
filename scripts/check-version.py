#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from version_refs import version_lines

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

for path, expected_lines in version_lines(version).items():
    lines = read(path).splitlines()
    for expected in expected_lines:
        if lines.count(expected) != 1:
            fail(f"{path} must contain exactly one version line {expected!r}")

# A broad version replacement once changed this IPv4 address along with the app.
if re.findall(r"\bip -4 route get (\S+)", read("setup.sh")) != ["1.1.1.1"]:
    fail("setup.sh route probe must stay 1.1.1.1; it is not an application version")

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
):
    if not (ROOT / path).is_file():
        fail(f"required file is missing: {path}")

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
