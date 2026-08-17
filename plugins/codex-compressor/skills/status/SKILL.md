---
name: status
description: codex-compressor의 설치 상태와 검증 단계를 읽기 전용으로 확인합니다.
---

# 상태 확인

```text
# macOS/Linux
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor status

# Windows PowerShell
$env:PYTHONPATH = Join-Path $env:PLUGIN_ROOT "runtime"
py -3 -m codex_compressor status
```

상태 출력은 다음 단계를 구분합니다.

- `PASS`: 해당 검사가 통과했습니다.
- `WARN`: 계속 사용할 수 있지만 버전, 인식하지 못한 버퍼 값 또는 후크 승인 확인이 필요합니다.
- `FAIL`: 현재 구성으로는 안전하게 사용할 수 없으며 수정 또는 복원이 필요합니다.
- `BLOCKED`: 필요한 Codex 기능이나 권한을 확인할 수 없어 다음 단계로 진행하지 않았습니다.
- `NOT_RUN`: 앞 단계의 실패 때문에 실행하지 않았습니다.

상태 확인은 설정을 변경하지 않습니다. 원본 파일의 내용과 사용자가 편집한 설정 보존 여부는 `doctor`에서 별도로 확인합니다.
