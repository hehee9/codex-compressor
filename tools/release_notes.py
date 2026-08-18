#!/usr/bin/env python3
"""Extract one version section from CHANGELOG.md for a GitHub release."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


VERSION_HEADING = re.compile(r"^##\s+v(?P<version>\S+)\s+-\s+.+$", re.MULTILINE)
SECTION_HEADING = re.compile(r"^##\s+", re.MULTILINE)


def extract_release_notes(changelog: str, version: str) -> str:
    """지정한 버전의 변경 로그 본문을 반환합니다."""
    matches = [
        match
        for match in VERSION_HEADING.finditer(changelog)
        if match.group("version") == version
    ]
    if not matches:
        raise ValueError(f"변경 로그에 v{version} 항목이 없습니다.")
    if len(matches) > 1:
        raise ValueError(f"변경 로그에 v{version} 항목이 중복되어 있습니다.")

    start = matches[0].end()
    next_heading = SECTION_HEADING.search(changelog, start)
    end = next_heading.start() if next_heading else len(changelog)
    notes = changelog[start:end].strip()
    if not notes:
        raise ValueError(f"변경 로그의 v{version} 항목이 비어 있습니다.")
    return notes


def main(argv: list[str] | None = None) -> int:
    """변경 로그에서 릴리스 본문을 추출해 파일로 저장합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Release version without the v prefix")
    parser.add_argument("output", type=Path, help="Output Markdown file")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="Changelog path (default: CHANGELOG.md)",
    )
    args = parser.parse_args(argv)

    try:
        changelog = args.changelog.read_text(encoding="utf-8")
        notes = extract_release_notes(changelog, args.version)
    except (OSError, UnicodeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
