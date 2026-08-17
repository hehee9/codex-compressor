#!/usr/bin/env python3
"""플러그인 런타임에 동기화된 같은 소스 코어를 실행합니다."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    """PLUGIN_ROOT 아래의 동기화된 continuity 코어를 실행합니다."""
    if sys.version_info < (3, 11):
        raise SystemExit(
            "codex-compressor 플러그인 후크에는 Python 3.11 이상이 필요합니다. "
            f"현재 버전: {sys.version.split()[0]}"
        )

    plugin_root = Path(os.environ["PLUGIN_ROOT"]).expanduser().resolve()
    runtime_root = plugin_root / "runtime"
    entrypoint = runtime_root / "codex_compressor" / "continuity.py"
    if not entrypoint.is_file():
        raise SystemExit(
            "codex-compressor 플러그인 런타임이 없습니다. "
            "설치 전에 tools/sync_plugin_runtime.py를 실행하세요."
        )

    sys.path.insert(0, str(runtime_root))
    sys.argv[0] = str(entrypoint)
    runpy.run_path(str(entrypoint), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
