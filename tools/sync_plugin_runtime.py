#!/usr/bin/env python3
"""소스 코어를 플러그인 런타임에 결정적으로 동기화합니다."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "codex_compressor"
PLUGIN_RUNTIME_ROOT = (
    REPOSITORY_ROOT / "plugins" / "codex-compressor" / "runtime" / "codex_compressor"
)


def _collect_files(root: Path) -> dict[Path, Path]:
    """캐시·바이트코드를 제외한 파일을 상대 경로로 수집합니다."""
    return {
        path.relative_to(root): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _find_differences(source: Path, destination: Path) -> list[Path]:
    """소스와 대상 사이의 추가·삭제·변경 파일을 반환합니다."""
    source_files = _collect_files(source)
    destination_files = _collect_files(destination) if destination.is_dir() else {}
    differences: list[Path] = []
    for relative_path in sorted(set(source_files) | set(destination_files)):
        source_file = source_files.get(relative_path)
        destination_file = destination_files.get(relative_path)
        if source_file is None or destination_file is None:
            differences.append(relative_path)
        elif not filecmp.cmp(source_file, destination_file, shallow=False):
            differences.append(relative_path)
    return differences


def sync(source: Path = SOURCE_ROOT, destination: Path = PLUGIN_RUNTIME_ROOT) -> list[Path]:
    """소스 코어를 대상에 복사하고 동기화된 변경 목록을 반환합니다."""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"소스 패키지를 찾을 수 없습니다: {source}")

    destination.mkdir(parents=True, exist_ok=True)
    for cache in sorted(
        (path for path in destination.rglob("__pycache__") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        shutil.rmtree(cache)
    for bytecode in destination.rglob("*.pyc"):
        if bytecode.is_file():
            bytecode.unlink()
    source_files = _collect_files(source)
    destination_files = _collect_files(destination)
    changes = _find_differences(source, destination)

    for relative_path in sorted(set(destination_files) - set(source_files), reverse=True):
        destination_files[relative_path].unlink()
    for relative_path, source_file in sorted(source_files.items()):
        destination_file = destination / relative_path
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        if not destination_file.is_file() or not filecmp.cmp(
            source_file, destination_file, shallow=False
        ):
            shutil.copyfile(source_file, destination_file)

    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    return changes


def check(source: Path = SOURCE_ROOT, destination: Path = PLUGIN_RUNTIME_ROOT) -> list[Path]:
    """소스와 대상이 같은지 확인하고 차이 경로를 반환합니다."""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"소스 패키지를 찾을 수 없습니다: {source}")
    return _find_differences(source, destination)


def main(argv: list[str] | None = None) -> int:
    """동기화 또는 동기화 상태 확인 명령을 처리합니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="대상을 수정하지 않고 소스와의 차이만 확인합니다.",
    )
    args = parser.parse_args(argv)
    try:
        differences = check() if args.check else sync()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2

    if differences:
        print("동기화 차이:")
        for path in differences:
            print(f"- {path.as_posix()}")
    if args.check and differences:
        return 1
    print("플러그인 런타임 동기화 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



