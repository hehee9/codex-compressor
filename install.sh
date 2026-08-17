#!/usr/bin/env sh
# Codex Compressor 소스 CLI를 실행하는 Unix 설치 도우미입니다.
set -eu

SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python 3.11 이상을 찾지 못했습니다." >&2
    exit 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
    PYTHONPATH="$SOURCE_ROOT/src:$PYTHONPATH"
else
    PYTHONPATH="$SOURCE_ROOT/src"
fi
export PYTHONPATH
exec "$PYTHON" -m codex_compressor "$@"

