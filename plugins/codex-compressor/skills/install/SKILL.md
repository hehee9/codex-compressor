---
name: install
description: codex-compressor를 확인하고 안전하게 설치합니다.
---

# 설치

플러그인은 소스 저장소가 삭제된 뒤에도 동작해야 하므로, 모든 명령은 설치된 플러그인의
`runtime`을 사용합니다. `${PLUGIN_ROOT}`는 Codex가 이 플러그인에 제공하는 설치 경로입니다.

codex-compressor는 두 가지 중 **한 가지 모드만** 선택해 사용합니다.

- **설정 관리 모드**: 플러그인 없이 저장소 루트의 `install.ps1` 또는 `install.sh`를 실행합니다.
- **플러그인 후크 모드**: 아래의 로컬 마켓플레이스에서 플러그인을 설치한 뒤 설정 관리 설치 단계를 한 번 실행합니다.

두 모드를 동시에 활성화하면 설정을 서로 덮어쓸 수 있으므로 함께 사용하지 않습니다. 새 설치의 버퍼 기본값은 `16384`입니다. 기존 설정에서 도구가 인식하는 `155200` 버퍼를 발견하면 그 값을 유지하고, 인식하지 못한 값은 임의로 바꾸지 않고 사용자에게 확인을 요청합니다.

## 설정 관리 모드

```text
# macOS/Linux
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor install --mode standalone
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor status
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor doctor

# Windows PowerShell
$env:PYTHONPATH = Join-Path $env:PLUGIN_ROOT "runtime"
py -3 -m codex_compressor install --mode standalone
py -3 -m codex_compressor status
py -3 -m codex_compressor doctor
```

소스 저장소에서 처음 설치하는 경우에는 저장소 루트의 설치 도우미를 사용합니다.

```text
# macOS/Linux
./install.sh install --mode standalone

# Windows PowerShell
.\install.ps1 install --mode standalone
```

설치 전에 원본 설정을 백업하고, 원본 및 사용자가 편집한 모든 키를 보존하는지 확인합니다.

## 플러그인 후크 모드

소스 저장소에서 처음 설치할 때는 루트의 설치 도우미가 `src` 레이아웃을 준비합니다.

```text
# macOS/Linux
./install.sh install --mode plugin

# Windows PowerShell
.\install.ps1 install --mode plugin
```

그 다음 저장소 루트에서 마켓플레이스를 등록합니다.

```text
codex plugin marketplace add .
codex plugin marketplace list
codex plugin add codex-compressor@codex-compressor-local
```

플러그인 설치만으로 설정 관리 단계가 자동 실행되지는 않습니다. 설정 관리 단계가 필요하면 설치된 런타임으로 다음을 실행합니다.

```text
# macOS/Linux
PYTHONPATH="${PLUGIN_ROOT}/runtime" python3 -m codex_compressor install --mode plugin

# Windows PowerShell
$env:PYTHONPATH = Join-Path $env:PLUGIN_ROOT "runtime"
py -3 -m codex_compressor install --mode plugin
```

Codex에서 `/hooks`를 열어 후크 신뢰 승인을 완료한 뒤 새 세션에서 `status`와 `doctor`를 실행합니다.
