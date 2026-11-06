#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"

VERSION_LINES = {
    "VERSION": ("@VERSION@",),
    "custom_components/tvt_archive/manifest.json": ('  "version": "@VERSION@"',),
    "addon/config.yaml": ('version: "@VERSION@"',),
    "Dockerfile": ("ARG APP_VERSION=@VERSION@",),
    ".env.example": ("TVT_ARCHIVE_IMAGE=ghcr.io/mhndt/tvt-archive:@VERSION@",),
    "compose/compose.yaml": (
        "    image: ${TVT_ARCHIVE_IMAGE:-ghcr.io/mhndt/tvt-archive:@VERSION@}",
    ),
    "compose/build-local.yaml": (
        "    image: ${TVT_ARCHIVE_LOCAL_IMAGE:-tvt-archive:@VERSION@-local}",
        '        APP_VERSION: "@VERSION@"',
    ),
    "setup.sh": (
        "TVT_ARCHIVE_IMAGE=${TVT_ARCHIVE_IMAGE:-ghcr.io/mhndt/tvt-archive:@VERSION@}",
        "TVT_ARCHIVE_LOCAL_IMAGE=${TVT_ARCHIVE_LOCAL_IMAGE:-tvt-archive:@VERSION@-local}",
    ),
    "custom_components/tvt_archive/bridge/bridge.py": (
        'APP_VERSION = "@VERSION@"',
        '    server_version = "TVTArchiveBridge/@VERSION@"',
    ),
}


def version_lines(version: str) -> dict[str, tuple[str, ...]]:
    return {
        path: tuple(line.replace("@VERSION@", version) for line in lines)
        for path, lines in VERSION_LINES.items()
    }


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def set_version(new: str) -> None:
    if not re.fullmatch(VERSION_RE, new):
        fail(f"not a supported version: {new!r}")
    old_lines, new_lines = version_lines(read("VERSION").strip()), version_lines(new)
    pending = {}
    for path, lines in old_lines.items():
        content = read(path)
        for old, replacement in zip(lines, new_lines[path], strict=True):
            content, count = re.subn(rf"(?m)^{re.escape(old)}$", replacement, content)
            if count != 1:
                fail(f"expected one version line in {path}: {old!r}")
        pending[path] = content
    for path, content in pending.items():
        (ROOT / path).write_text(content, encoding="utf-8", newline="\n")


if len(sys.argv) == 3 and sys.argv[1] == "--set":
    set_version(sys.argv[2])
    sys.argv = sys.argv[:1]

version = read("VERSION").strip()
if not re.fullmatch(VERSION_RE, version):
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
