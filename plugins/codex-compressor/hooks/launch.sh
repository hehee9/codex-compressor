#!/usr/bin/env sh
# POSIX 셸에서 플러그인 후크를 실행합니다.
set -eu
exec python3 "${PLUGIN_ROOT}/hooks/launch.py" "$@"

