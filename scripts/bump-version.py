#!/usr/bin/env python3
"""Update application versions without touching IP addresses or dependencies."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from version_refs import version_lines

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="new version without the v prefix, e.g. 1.1.3")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", args.version):
        parser.error("expected a version such as 1.1.3 or 1.1.3-beta.1")

    # Check every existing reference and the route probe before changing any file.
    subprocess.run([sys.executable, str(ROOT / "scripts/check-version.py")], check=True)
    old_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    old_references = version_lines(old_version)
    new_references = version_lines(args.version)
    pending = {}
    for path, old_lines in old_references.items():
        content = (ROOT / path).read_text(encoding="utf-8")
        for old, new in zip(old_lines, new_references[path], strict=True):
            content, count = re.subn(r"(?m)^" + re.escape(old) + r"$", new, content)
            if count != 1:
                raise SystemExit(f"ERROR: expected exactly one version line in {path}: {old!r}")
        pending[path] = content

    for path, content in pending.items():
        (ROOT / path).write_text(content, encoding="utf-8", newline="\n")
    subprocess.run([sys.executable, str(ROOT / "scripts/check-version.py")], check=True)
    print(
        f"Updated application version {old_version} -> {args.version}. Review git diff before tagging."
    )


if __name__ == "__main__":
    main()
