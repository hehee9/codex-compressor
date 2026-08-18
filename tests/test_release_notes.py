from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "tools" / "release_notes.py"


class ReleaseNotesTests(unittest.TestCase):
    def _run(self, changelog: str, version: str = "1.0.1"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        changelog_path = root / "CHANGELOG.md"
        output_path = root / "RELEASE_NOTES.md"
        changelog_path.write_text(changelog, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                version,
                str(output_path),
                "--changelog",
                str(changelog_path),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, output_path

    def test_extracts_only_requested_version_body(self) -> None:
        result, output_path = self._run(
            "# CHANGE LOG\n\n"
            "## v1.0.1 - 2026.08.18\n"
            "### Fixed\n"
            "- 현재 수정\n\n"
            "## v1.0.0 - 2026.08.17\n"
            "### Added\n"
            "- 이전 기능\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            output_path.read_text(encoding="utf-8"),
            "### Fixed\n- 현재 수정\n",
        )

    def test_rejects_missing_version(self) -> None:
        result, output_path = self._run(
            "# CHANGE LOG\n\n## v1.0.0 - 2026.08.17\n- 첫 릴리스\n"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("v1.0.1 항목이 없습니다", result.stderr)
        self.assertFalse(output_path.exists())

    def test_rejects_empty_version_body(self) -> None:
        result, output_path = self._run(
            "# CHANGE LOG\n\n"
            "## v1.0.1 - 2026.08.18\n\n"
            "## v1.0.0 - 2026.08.17\n"
            "- 첫 릴리스\n"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("v1.0.1 항목이 비어 있습니다", result.stderr)
        self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
