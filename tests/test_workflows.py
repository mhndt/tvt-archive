from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_release_actions_are_sha_pinned(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if "uses:" in line]
        self.assertTrue(lines)
        for line in lines:
            reference = line.split("@", 1)[1].split()[0]
            self.assertRegex(reference, r"^[0-9a-f]{40}$", line)
            self.assertRegex(line, r" # v\d+\.\d+\.\d+$", line)


if __name__ == "__main__":
    unittest.main()
