---
name: uninstall
description: 백업을 보존하면서 codex-compressor를 제거하거나 복원합니다.
---

# 제거와 복원

```text
# macOS/Linux
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor uninstall
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor restore --backup <backup-id>

# Windows PowerShell
$env:PYTHONPATH = Join-Path $env:PLUGIN_ROOT "runtime"
py -3 -m codex_compressor uninstall
py -3 -m codex_compressor restore --backup <backup-id>
```

제거는 새로 추가한 설정과 플러그인 연결을 정리하되, 원본 설정과 사용자 편집 내용이 들어 있는 백업을 삭제하지 않습니다. `--purge-state`를 사용해도 백업은 별도로 보존됩니다. 복원이 필요한 경우 상태를 먼저 확인하고 `restore --backup <backup-id>`로 백업에서 복원합니다.

플러그인 후크를 사용했다면 다음 순서로 처리합니다.

1. 설치된 플러그인 런타임으로 `uninstall`을 실행해 설정을 정리합니다.
2. Codex의 `/hooks`에서 codex-compressor 후크 신뢰를 해제합니다.
3. `codex plugin remove codex-compressor@codex-compressor-local`로 플러그인을 제거합니다.
4. 백업을 확인한 뒤 필요할 때만 `restore` 절차를 실행합니다.

자동 복원은 원본 파일이 사용자의 제거 이후 변경된 경우 실행하지 않습니다. 그런 경우 백업과 현재 파일의 차이를 확인하고 사용자가 복원을 결정해야 합니다.
