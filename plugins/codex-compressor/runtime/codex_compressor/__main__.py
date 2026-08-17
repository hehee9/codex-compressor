"""`python -m codex_compressor`와 설치 런타임 직접 실행 진입점입니다."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from .cli import main
except ImportError:
    # 설치 런타임의 __main__.py를 파일 경로로 직접 실행하는 경우입니다.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from codex_compressor.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
