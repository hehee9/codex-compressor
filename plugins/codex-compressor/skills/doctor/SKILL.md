---
name: doctor
description: Codex 버전, 설정 권한, 후크 기능과 버퍼 설정을 진단합니다.
---

# 진단

```text
# macOS/Linux
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor doctor

# Windows PowerShell
$env:PYTHONPATH = Join-Path $env:PLUGIN_ROOT "runtime"
py -3 -m codex_compressor doctor
```

진단은 다음을 확인합니다.

1. Python이 3.11 이상인지 확인합니다.
2. 현재 플랫폼과 Codex 버전이 지원 범위인지 확인하고, 알 수 없는 버전은 경고로 표시합니다.
3. Codex 설정 파일을 읽고 쓸 권한이 있는지 확인합니다.
4. 후크를 사용하는 모드라면 후크 기능과 `/hooks` 신뢰 승인이 가능한지 확인합니다.
5. 새 설치 기본값 `16384`, 인식 가능한 기존 값 `155200`, 기타 값에 대한 마이그레이션 확인을 구분합니다.

진단에서 `WARN`이 나오면 이유를 확인한 뒤 진행합니다. `FAIL`이나 `BLOCKED`를 무시하고 설정 파일을 직접 덮어쓰지 않습니다.
