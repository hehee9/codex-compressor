"""Codex Compressor 설치, 상태 확인, 복구를 담당합니다."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .compatibility import inspect_compatibility
from .configuration import (
    LEGACY_SCRIPT_SHA256,
    REQUEST_MARKER,
    ConfigurationConflict,
    ConfigurationError,
    inspect_token_budget,
    legacy_script_matches,
    parse_config,
    prepare_token_budget,
    remove_token_budget,
    sha256_bytes,
)


VERSION = "0.1.0"
HOOK_EVENTS = ("PreToolUse", "PreCompact", "PostCompact", "SessionStart")
CONTINUITY_RELATIVE = Path("codex_compressor") / "continuity.py"


class ManagerError(RuntimeError):
    """설치 관리 작업을 중단해야 할 때 발생합니다."""


@dataclass(frozen=True)
class FileSnapshot:
    """트랜잭션 롤백에 필요한 한 파일의 원본입니다."""

    path: Path
    existed: bool
    data: bytes


@dataclass(frozen=True)
class RuntimePlan:
    """소스에서 계산한 런타임 파일과 트랜잭션 대상을 담습니다."""

    files: dict[str, bytes]
    stale_files: tuple[str, ...]
    snapshots: tuple[FileSnapshot, ...]


def _sha256(data: bytes) -> str:
    """바이트를 대문자 SHA-256으로 해시합니다."""

    return hashlib.sha256(data).hexdigest().upper()


def _read_bytes(path: Path) -> bytes:
    """파일을 읽거나 없는 파일을 빈 상태로 표시합니다."""

    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _read_snapshot(path: Path) -> FileSnapshot:
    """경로의 존재 여부와 원본 바이트를 캡처합니다."""

    try:
        return FileSnapshot(path, True, path.read_bytes())
    except FileNotFoundError:
        return FileSnapshot(path, False, b"")


def _atomic_write(path: Path, data: bytes) -> None:
    """임시 파일을 fsync한 뒤 대상 파일로 원자적 교체합니다."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_snapshot(snapshot: FileSnapshot) -> None:
    """한 파일을 캡처한 상태로 되돌립니다."""

    if snapshot.existed:
        _atomic_write(snapshot.path, snapshot.data)
    else:
        snapshot.path.unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    """UTF-8 JSON 바이트를 일관된 형식으로 만듭니다."""

    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _load_json(path: Path, default: Any) -> Any:
    """JSON 파일을 읽고 없는 파일은 기본값으로 표시합니다."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise ManagerError(f"JSON 해석 실패: {path}: {exc}") from exc


def _text_from_bytes(data: bytes) -> str:
    """UTF-8 파일을 문자열로 해석합니다."""

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManagerError(f"UTF-8 텍스트가 아닙니다: {exc}") from exc


def _now_id() -> str:
    """백업 식별자를 만듭니다."""

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:10]}"


class Manager:
    """Codex 홈과 Compressor 런타임의 수명주기를 관리합니다."""

    def __init__(self, codex_home: str | Path | None = None, source_root: str | Path | None = None):
        configured_home = codex_home or os.environ.get("CODEX_HOME")
        self.codex_home = Path(configured_home).expanduser().resolve() if configured_home else (
            Path.home() / ".codex"
        ).resolve()
        self.source_root = (
            Path(source_root).resolve()
            if source_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.runtime_root = self.codex_home / "codex-compressor"
        self.config_path = self.codex_home / "config.toml"
        self.hooks_path = self.codex_home / "hooks.json"
        self.state_path = self.runtime_root / "state.json"

    def _state(self) -> dict[str, Any] | None:
        """설치 상태를 읽습니다."""

        value = _load_json(self.state_path, None)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ManagerError("설치 상태가 객체가 아닙니다")
        return value

    @staticmethod
    def _hook_state_table(config: dict[str, Any]) -> dict[str, Any]:
        """파싱된 설정에서 훅 신뢰 상태 테이블을 반환합니다."""

        hooks = config.get("hooks")
        if not isinstance(hooks, dict):
            return {}
        state = hooks.get("state")
        return state if isinstance(state, dict) else {}

    def _pre_install_config(self, state: dict[str, Any]) -> dict[str, Any]:
        """마지막 설치 백업에서 변경 전 설정을 읽습니다."""

        backup_id = state.get("last_backup_id")
        if not isinstance(backup_id, str) or not backup_id:
            return {}
        root = self.runtime_root / "backups" / backup_id
        manifest = _load_json(root / "manifest.json", None)
        if not isinstance(manifest, dict):
            return {}
        for target in manifest.get("targets", []):
            if not isinstance(target, dict) or target.get("path") != str(self.config_path):
                continue
            backup_file = target.get("pre_backup_file")
            if not isinstance(backup_file, str):
                return {}
            try:
                text = (root / backup_file).read_text(encoding="utf-8")
                return parse_config(text)
            except (OSError, ConfigurationError):
                return {}
        return {}

    def _trust_approval_status(self, state: dict[str, Any] | None) -> bool | str:
        """설치 후 변경된 네 훅 신뢰 해시를 확인합니다."""

        if not state or state.get("mode") != "standalone":
            return "unobserved"
        if state.get("trust_approved") is True:
            return True
        try:
            current = parse_config(self._load_target_text(self.config_path))
        except ConfigurationError:
            return "unobserved"
        current_states = self._hook_state_table(current)
        previous_states = self._hook_state_table(self._pre_install_config(state))
        event_labels = ("pre_tool_use", "pre_compact", "post_compact", "session_start")
        changed_hashes = 0
        for label in event_labels:
            key = f"{self.hooks_path}:{label}:0:0"
            current_entry = current_states.get(key)
            if not isinstance(current_entry, dict):
                return "unobserved"
            current_hash = current_entry.get("trusted_hash")
            if not isinstance(current_hash, str) or not current_hash.startswith("sha256:"):
                return "unobserved"
            previous_entry = previous_states.get(key)
            previous_hash = (
                previous_entry.get("trusted_hash")
                if isinstance(previous_entry, dict)
                else None
            )
            if current_hash != previous_hash:
                changed_hashes += 1
        return True if changed_hashes == len(event_labels) else "unobserved"

    def _write_state(self, state: dict[str, Any]) -> None:
        """설치 상태를 원자적으로 저장합니다."""

        _atomic_write(self.state_path, _json_bytes(state))

    def _backup(self, snapshots: list[FileSnapshot], operation: str, mode: str | None) -> str:
        """변경 전 원본과 트랜잭션 메타데이터를 저장합니다."""

        backup_id = _now_id()
        root = self.runtime_root / "backups" / backup_id
        files_dir = root / "files"
        files_dir.mkdir(parents=True, exist_ok=False)
        entries: list[dict[str, Any]] = []
        for index, snapshot in enumerate(snapshots):
            relative_name = f"target-{index}.bin"
            backup_path = files_dir / relative_name
            if snapshot.existed:
                backup_path.write_bytes(snapshot.data)
            entries.append(
                {
                    "path": str(snapshot.path),
                    "pre_existed": snapshot.existed,
                    "pre_sha256": _sha256(snapshot.data) if snapshot.existed else None,
                    "pre_backup_file": str(Path("files") / relative_name)
                    if snapshot.existed
                    else None,
                    "post_existed": None,
                    "post_sha256": None,
                }
            )
        manifest = {
            "format": 1,
            "id": backup_id,
            "created_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "mode": mode,
            "owner": "codex-compressor",
            "version": VERSION,
            "transaction": {"status": "prepared", "targets": entries},
            "targets": entries,
        }
        _atomic_write(root / "manifest.json", _json_bytes(manifest))
        return backup_id

    def _transaction(
        self,
        snapshots: list[FileSnapshot],
        operation: str,
        mode: str | None,
        action: Callable[[str], None],
    ) -> str:
        """백업 후 작업하고 실패 시 모든 대상 파일을 복구합니다."""

        backup_id = self._backup(snapshots, operation, mode)
        try:
            action(backup_id)
            self._set_backup_status(backup_id, "committed")
        except Exception as exc:
            try:
                self._set_backup_status(backup_id, "rolling_back")
            except OSError:
                pass
            rollback_errors: list[str] = []
            for snapshot in reversed(snapshots):
                try:
                    _restore_snapshot(snapshot)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{snapshot.path}: {rollback_exc}")
            if rollback_errors:
                raise ManagerError(
                    f"작업 실패 후 롤백도 실패했습니다: {exc}; {rollback_errors}"
                ) from exc
            try:
                self._set_backup_status(backup_id, "rolled_back")
            except OSError:
                pass
            raise
        return backup_id

    def _set_backup_status(self, backup_id: str, status: str) -> None:
        """백업 매니페스트에 트랜잭션 종료 상태를 기록합니다."""

        manifest_path = self.runtime_root / "backups" / backup_id / "manifest.json"
        manifest = _load_json(manifest_path, None)
        if not isinstance(manifest, dict):
            raise OSError(f"백업 매니페스트가 없습니다: {manifest_path}")
        transaction = manifest.setdefault("transaction", {})
        transaction["status"] = status
        transaction["updated_at"] = datetime.now(UTC).isoformat()
        if status == "committed":
            for target in manifest.get("targets", []):
                target_path = Path(str(target["path"]))
                try:
                    data = target_path.read_bytes()
                except FileNotFoundError:
                    target["post_existed"] = False
                    target["post_sha256"] = None
                else:
                    target["post_existed"] = True
                    target["post_sha256"] = _sha256(data)
        _atomic_write(manifest_path, _json_bytes(manifest))

    def _hook_entry(self) -> dict[str, Any]:
        """현재 Python과 설치 경로를 사용한 장기 작업 훅 한 개를 만듭니다."""

        executable = str(Path(sys.executable).resolve())
        continuity = str((self.runtime_root / CONTINUITY_RELATIVE).resolve())

        return {
            "type": "command",
            "command": f"{shlex.quote(executable)} {shlex.quote(continuity)}",
            "commandWindows": subprocess.list2cmdline([executable, continuity]),
            "timeout": 5,
            "statusMessage": "Preserving the rollover checkpoint",
        }

    def _new_hooks(self) -> dict[str, Any]:
        """전역 훅 설정에 추가할 네 개의 이벤트를 만듭니다."""

        return {
            "hooks": {
                event: [
                    {
                        "matcher": matcher,
                        "hooks": [self._hook_entry()],
                    }
                ]
                for event, matcher in (
                    ("PreToolUse", "^new_context$"),
                    ("PreCompact", "^(auto|manual)$"),
                    ("PostCompact", "^(auto|manual)$"),
                    ("SessionStart", "^compact$"),
                )
            }
        }

    @staticmethod
    def _hook_is_compressor(entry: Any) -> bool:
        """훅 객체가 Compressor 포터블 명령인지 확인합니다."""

        if not isinstance(entry, dict):
            return False
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and "codex-compressor" in str(hook.get("command", "")):
                return True
            if isinstance(hook, dict) and "codex-compressor" in str(
                hook.get("commandWindows", "")
            ):
                return True
        return False

    @staticmethod
    def _hooks_are_empty(data: dict[str, Any]) -> bool:
        """관리 훅을 제거한 뒤 사용자 훅이 남지 않았는지 확인합니다."""

        return set(data) <= {"hooks"} and all(
            isinstance(entries, list) and not entries
            for entries in data.get("hooks", {}).values()
        )

    @staticmethod
    def _remove_exact_owned_hooks(
        hooks_data: dict[str, Any], installed: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """설치 당시 값과 같은 훅만 제거하고 편집 충돌을 보고합니다."""

        result = json.loads(json.dumps(hooks_data))
        conflicts: list[str] = []
        for event, expected in installed.items():
            hooks = result.get("hooks")
            if not isinstance(hooks, dict):
                if hooks is None:
                    continue
                conflicts.append(event)
                continue
            if event not in hooks:
                continue
            entries = hooks[event]
            if not isinstance(entries, list):
                conflicts.append(event)
                continue
            kept: list[Any] = []
            removed = False
            for entry in entries:
                if entry == expected:
                    removed = True
                elif Manager._hook_is_compressor(entry):
                    conflicts.append(event)
                    kept.append(entry)
                else:
                    kept.append(entry)
            hooks[event] = kept
            if not removed and event not in conflicts:
                # 관리 상태가 있는데 훅이 사라진 것은 이미 정리된 정상 상태입니다.
                pass
        return result, sorted(set(conflicts))

    def _legacy_installation(self, hooks_data: dict[str, Any]) -> bool:
        """알려진 기존 설치의 스크립트와 네 이벤트를 확인합니다."""

        hooks = hooks_data.get("hooks") if isinstance(hooks_data, dict) else None
        if not isinstance(hooks, dict):
            return False
        matched_events: set[str] = set()
        for event in HOOK_EVENTS:
            for entry in hooks.get(event, []):
                if not isinstance(entry, dict):
                    continue
                for hook in entry.get("hooks", []):
                    if not isinstance(hook, dict):
                        continue
                    command = f"{hook.get('command', '')}\n{hook.get('commandWindows', '')}"
                    if "long-task-continuity.py" in command:
                        matched_events.add(event)
                        break
        hooks_match = matched_events == set(HOOK_EVENTS)
        script_path = self.codex_home / "hooks" / "long-task-continuity.py"
        try:
            script = script_path.read_bytes()
        except FileNotFoundError:
            if hooks_match:
                raise ManagerError("레거시 훅 네 개가 있지만 스크립트가 없습니다")
            return False
        if not legacy_script_matches(script):
            if hooks_match or REQUEST_MARKER.encode("utf-8") in script:
                actual = sha256_bytes(script)
                raise ManagerError(
                    "레거시 설치가 사용자 수정 또는 모호합니다 "
                    f"(script sha256={actual}, expected={LEGACY_SCRIPT_SHA256})"
                )
            return False
        return hooks_match

    def _load_target_text(self, path: Path) -> str:
        """텍스트 설정을 읽되 없는 파일은 빈 파일로 취급합니다."""

        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _runtime_plan(self, old_state: dict[str, Any] | None) -> RuntimePlan:
        """소스 패키지를 읽어 런타임 변경 계획과 원본 대상을 계산합니다."""

        source = self.source_root / "src" / "codex_compressor"
        if not source.is_dir():
            source = Path(__file__).resolve().parent
        if not source.is_dir():
            raise ManagerError(f"소스 패키지를 찾을 수 없습니다: {source}")
        files: dict[str, bytes] = {}
        for source_file in source.rglob("*"):
            if not source_file.is_file() or source_file.name.endswith(".pyc"):
                continue
            relative = source_file.relative_to(source)
            files[str(Path("codex_compressor") / relative)] = source_file.read_bytes()
        old_files = {
            str(relative)
            for relative in (old_state or {}).get("runtime_files", [])
            if isinstance(relative, str)
        }
        stale_files = tuple(sorted(old_files - set(files)))
        target_paths = {
            self.runtime_root / relative for relative in old_files | set(files)
        }
        target_paths.add(self.state_path)
        snapshots = tuple(
            _read_snapshot(path) for path in sorted(target_paths, key=str)
        )
        return RuntimePlan(files, stale_files, snapshots)

    def _runtime_conflicts(self, state: dict[str, Any]) -> list[str]:
        """설치 기록과 달라진 런타임 파일을 찾아 반환합니다."""

        conflicts: list[str] = []
        for relative in state.get("runtime_files", []):
            path = self.runtime_root / relative
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            expected = state.get("runtime_hashes", {}).get(relative)
            if expected and _sha256(data) != expected:
                conflicts.append(str(path))
        return conflicts

    def install(self, mode: str, *, replace_token_budget: bool = False) -> dict[str, Any]:
        """standalone 또는 plugin 모드로 안전하게 설치합니다."""

        if mode not in {"standalone", "plugin"}:
            raise ManagerError("mode는 standalone 또는 plugin이어야 합니다")
        old_state = self._state()
        trust_approved = self._trust_approval_status(old_state)
        config_snapshot = _read_snapshot(self.config_path)
        hooks_snapshot = _read_snapshot(self.hooks_path)
        config_text = _text_from_bytes(config_snapshot.data) if config_snapshot.existed else ""
        hooks_data = (
            _load_json(self.hooks_path, {}) if hooks_snapshot.existed else {}
        )
        if not isinstance(hooks_data, dict):
            raise ManagerError("hooks.json 최상위 값은 객체여야 합니다")
        if "hooks" in hooks_data and not isinstance(hooks_data["hooks"], dict):
            raise ManagerError("hooks.json의 hooks는 객체여야 합니다")

        previous_token = old_state.get("token_budget") if isinstance(old_state, dict) else None
        previous_installed = (
            previous_token.get("installed")
            if isinstance(previous_token, dict)
            else None
        )
        previous_original = (
            previous_token.get("original")
            if isinstance(previous_token, dict)
            else None
        )
        previous_legacy = (
            bool(previous_token.get("legacy_migrated"))
            if isinstance(previous_token, dict)
            else False
        )
        legacy = False
        desired: dict[str, Any] | None = None
        if isinstance(previous_installed, dict) and all(
            key in previous_installed
            for key in (
                "enabled",
                "auto_compact_fallback_prompt",
                "auto_compact_fallback_buffer_tokens",
            )
        ):
            desired = dict(previous_installed)
            legacy = previous_legacy
        else:
            legacy = self._legacy_installation(hooks_data)
            if legacy:
                current = inspect_token_budget(config_text)
                if not all(
                    key in current
                    for key in (
                        "enabled",
                        "auto_compact_fallback_prompt",
                        "auto_compact_fallback_buffer_tokens",
                    )
                ):
                    raise ManagerError("레거시 훅은 확인됐지만 토큰 예산 필드가 불완전합니다")
                desired = {
                    "enabled": current["enabled"],
                    "auto_compact_fallback_prompt": current["auto_compact_fallback_prompt"],
                    "auto_compact_fallback_buffer_tokens": current[
                        "auto_compact_fallback_buffer_tokens"
                    ],
                }
                if desired["enabled"] is not True or desired[
                    "auto_compact_fallback_buffer_tokens"
                ] != 155200:
                    raise ManagerError("레거시 설치의 토큰 예산 값이 알려진 값과 다릅니다")

        try:
            token_edit = prepare_token_budget(
                config_text,
                replace=replace_token_budget,
                desired=desired,
            )
        except (ConfigurationError, ConfigurationConflict) as exc:
            raise ManagerError(str(exc)) from exc
        if previous_installed is not None and isinstance(previous_original, dict):
            token_edit = replace(token_edit, original=previous_original)
        elif legacy:
            # 레거시 값은 이번 설치에서 명시적으로 Compressor 소유로
            # 채택하므로, 제거 시 원래부터 없었던 관리 필드로 취급합니다.
            token_edit = replace(
                token_edit,
                original={key: {"present": False} for key in token_edit.values},
            )

        new_hooks = json.loads(json.dumps(hooks_data))
        old_hook_conflicts: list[str] = []
        if old_state and isinstance(old_state.get("hooks"), dict):
            new_hooks, old_hook_conflicts = self._remove_exact_owned_hooks(
                new_hooks, old_state["hooks"]
            )
        if old_hook_conflicts:
            raise ManagerError("사용자 수정 훅 충돌: " + ", ".join(old_hook_conflicts))
        # 알려진 레거시 훅은 네 이벤트의 기존 항목만 교체합니다.
        if legacy:
            for event in HOOK_EVENTS:
                entries = new_hooks.setdefault("hooks", {}).setdefault(event, [])
                new_hooks["hooks"][event] = [
                    entry
                    for entry in entries
                    if not any(
                        isinstance(hook, dict)
                        and "long-task-continuity.py"
                        in f"{hook.get('command', '')}\n{hook.get('commandWindows', '')}"
                        for hook in (entry.get("hooks", []) if isinstance(entry, dict) else [])
                    )
                ]
        if mode == "standalone":
            new_hooks.setdefault("hooks", {})
            entries = new_hooks["hooks"]
            fresh = self._new_hooks()["hooks"]
            for event in HOOK_EVENTS:
                entries.setdefault(event, [])
                entries[event].extend(fresh[event])

        new_config_bytes = token_edit.text.encode("utf-8")
        new_hooks_bytes = b"" if mode == "plugin" and not hooks_snapshot.existed else _json_bytes(new_hooks)
        config_changed = new_config_bytes != config_snapshot.data
        hooks_changed = new_hooks_bytes != hooks_snapshot.data
        runtime_plan = self._runtime_plan(old_state)
        runtime_hashes = {relative: _sha256(data) for relative, data in runtime_plan.files.items()}
        candidate_state = {
            "format": 1,
            "owner": "codex-compressor",
            "version": VERSION,
            "mode": mode,
            "codex_home": str(self.codex_home),
            "config_path": str(self.config_path),
            "hooks_path": str(self.hooks_path),
            "managed_files_original": (
                old_state.get("managed_files_original")
                if isinstance(old_state, dict)
                and isinstance(old_state.get("managed_files_original"), dict)
                else {
                    "config_existed": config_snapshot.existed,
                    "hooks_existed": hooks_snapshot.existed,
                }
            ),
            "installed": dict(token_edit.values),
            "token_budget": {
                "installed": dict(token_edit.values),
                "original": (
                    previous_original
                    if isinstance(previous_original, dict)
                    else token_edit.original
                ),
                "legacy_migrated": legacy,
            },
            "hooks": (
                {
                    event: entries[0]
                    for event, entries in self._new_hooks().get("hooks", {}).items()
                }
                if mode == "standalone"
                else {}
            ),
            "runtime_files": sorted(runtime_plan.files),
            "runtime_hashes": runtime_hashes,
            "trust_approved": trust_approved is True,
            "last_backup_id": None,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        stable_keys = tuple(key for key in candidate_state if key not in {"last_backup_id", "updated_at"})
        state_changed = old_state is None or any(
            old_state.get(key) != candidate_state[key] for key in stable_keys
        )
        snapshot_by_path = {snapshot.path: snapshot for snapshot in runtime_plan.snapshots}
        runtime_changed = any(
            not snapshot_by_path[self.runtime_root / relative].existed
            or snapshot_by_path[self.runtime_root / relative].data != data
            for relative, data in runtime_plan.files.items()
        ) or any(
            snapshot_by_path[self.runtime_root / relative].existed
            for relative in runtime_plan.stale_files
        )
        operation_needed = config_changed or hooks_changed or runtime_changed or state_changed
        state = dict(candidate_state)
        if not state_changed and isinstance(old_state, dict):
            state = old_state
        backup_id: str | None = None

        def mutate(transaction_id: str) -> None:
            if config_changed:
                parse_config(_text_from_bytes(new_config_bytes))
                _atomic_write(self.config_path, new_config_bytes)
            if hooks_changed:
                json.loads(_text_from_bytes(new_hooks_bytes))
                _atomic_write(self.hooks_path, new_hooks_bytes)
            for relative in runtime_plan.stale_files:
                (self.runtime_root / relative).unlink(missing_ok=True)
            for relative, data in runtime_plan.files.items():
                _atomic_write(self.runtime_root / relative, data)
            if state_changed or operation_needed:
                state["last_backup_id"] = transaction_id
                state["updated_at"] = datetime.now(UTC).isoformat()
                self._write_state(state)

        if operation_needed:
            snapshots = [config_snapshot, hooks_snapshot, *runtime_plan.snapshots]
            unique_snapshots = list({snapshot.path: snapshot for snapshot in snapshots}.values())
            backup_id = self._transaction(unique_snapshots, "install", mode, mutate)
        return {
            "ok": True,
            "mode": mode,
            "backup_id": backup_id,
            "legacy_migrated": legacy,
            "changed": {
                "config": config_changed,
                "hooks": hooks_changed,
                "runtime": runtime_changed,
                "state": state_changed,
            },
        }

    def uninstall(self, *, purge_state: bool = False) -> dict[str, Any]:
        """관리 필드와 훅을 안전하게 정리합니다."""

        state = self._state()
        if state is None:
            return {"ok": True, "installed": False, "conflicts": []}
        config_snapshot = _read_snapshot(self.config_path)
        hooks_snapshot = _read_snapshot(self.hooks_path)
        config_text = _text_from_bytes(config_snapshot.data) if config_snapshot.existed else ""
        hooks_data = _load_json(self.hooks_path, {}) if hooks_snapshot.existed else {}
        if not isinstance(hooks_data, dict):
            raise ManagerError("hooks.json 최상위 값은 객체여야 합니다")
        if "hooks" in hooks_data and not isinstance(hooks_data["hooks"], dict):
            raise ManagerError("hooks.json의 hooks는 객체여야 합니다")
        ownership = state.get("token_budget", state)
        original_files = state.get("managed_files_original", {})
        original_config_existed = (
            bool(original_files.get("config_existed", True))
            if isinstance(original_files, dict)
            else True
        )
        original_hooks_existed = (
            bool(original_files.get("hooks_existed", True))
            if isinstance(original_files, dict)
            else True
        )
        token_edit = remove_token_budget(config_text, ownership)
        new_hooks = hooks_data
        hook_conflicts: list[str] = []
        if isinstance(state.get("hooks"), dict) and state["hooks"]:
            new_hooks, hook_conflicts = self._remove_exact_owned_hooks(
                hooks_data, state["hooks"]
            )
        new_config = token_edit.text.encode("utf-8")
        new_hooks_bytes = _json_bytes(new_hooks) if hooks_snapshot.existed else b""
        config_changed = new_config != config_snapshot.data
        hooks_changed = new_hooks_bytes != hooks_snapshot.data
        remove_config_file = (
            config_changed and not original_config_existed and not new_config.strip()
        )
        remove_hooks_file = (
            hooks_changed and not original_hooks_existed and self._hooks_are_empty(new_hooks)
        )
        conflicts = list(token_edit.conflicts) + [f"hook:{item}" for item in hook_conflicts]
        runtime_conflicts = self._runtime_conflicts(state)
        conflicts.extend(runtime_conflicts)
        if conflicts:
            return {
                "ok": False,
                "installed": True,
                "backup_id": None,
                "conflicts": conflicts,
                "purged_state": False,
            }

        runtime_snapshots = tuple(
            _read_snapshot(self.runtime_root / relative)
            for relative in state.get("runtime_files", [])
            if isinstance(relative, str)
        )
        snapshots = [
            config_snapshot,
            hooks_snapshot,
            *runtime_snapshots,
            _read_snapshot(self.state_path),
        ]
        unique_snapshots = list({snapshot.path: snapshot for snapshot in snapshots}.values())
        backup_id: str | None = None

        def mutate(transaction_id: str) -> None:
            if config_changed:
                parse_config(_text_from_bytes(new_config))
                if remove_config_file:
                    self.config_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.config_path, new_config)
            if hooks_changed:
                json.loads(_text_from_bytes(new_hooks_bytes))
                if remove_hooks_file:
                    self.hooks_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.hooks_path, new_hooks_bytes)
            for relative in state.get("runtime_files", []):
                if isinstance(relative, str):
                    (self.runtime_root / relative).unlink(missing_ok=True)
            self.state_path.unlink(missing_ok=True)

        operation_needed = (
            any(snapshot.existed for snapshot in runtime_snapshots)
            or config_changed
            or hooks_changed
            or self.state_path.exists()
        )
        if operation_needed:
            backup_id = self._transaction(
                unique_snapshots, "uninstall", state.get("mode"), mutate
            )
        normal_cleanup = True
        if purge_state:
            continuity = self.codex_home / "continuity"
            if continuity.exists():
                shutil.rmtree(continuity)
        return {
            "ok": normal_cleanup,
            "installed": True,
            "backup_id": backup_id,
            "conflicts": conflicts,
            "purged_state": bool(purge_state and normal_cleanup),
        }

    def restore(self, backup_id: str, *, force: bool = False) -> dict[str, Any]:
        """백업을 현재 파일이 바뀌지 않았을 때만 복원합니다."""

        if not backup_id or Path(backup_id).name != backup_id or backup_id in {".", ".."}:
            raise ManagerError("백업 식별자가 올바르지 않습니다")
        root = self.runtime_root / "backups" / backup_id
        manifest_path = root / "manifest.json"
        manifest = _load_json(manifest_path, None)
        if not isinstance(manifest, dict):
            raise ManagerError(f"백업을 찾을 수 없습니다: {backup_id}")
        targets = manifest.get("targets", [])
        if not isinstance(targets, list):
            raise ManagerError("백업 대상 메타데이터가 올바르지 않습니다")
        if manifest.get("transaction", {}).get("status") != "committed":
            raise ManagerError("커밋된 백업만 복원할 수 있습니다")
        changes: list[tuple[Path, bytes | None]] = []
        changed_current: list[str] = []
        for target in targets:
            path = Path(str(target.get("path", "")))
            expected_exists = bool(target.get("post_existed"))
            current_exists = path.exists()
            current_data = path.read_bytes() if current_exists else b""
            expected_hash = target.get("post_sha256")
            current_hash = _sha256(current_data) if current_exists else None
            if current_exists != expected_exists or current_hash != expected_hash:
                changed_current.append(str(path))
            backup_file = target.get("pre_backup_file")
            original = (root / backup_file).read_bytes() if backup_file else None
            changes.append((path, original))
        if changed_current and not force:
            raise ManagerError(
                "현재 파일이 백업 이후 변경되어 복원을 거부했습니다: "
                + ", ".join(changed_current)
            )
        for path, original in changes:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, original)
        return {"ok": True, "backup_id": backup_id, "forced": force, "restored": len(changes)}

    def _status(self, *, live_probe: bool = False) -> dict[str, Any]:
        """공통 상태와 단계별 관찰 결과를 구성합니다."""

        state = self._state()
        result = inspect_compatibility(
            self.codex_home,
            live_probe=live_probe,
            run_cli=True,
        )
        installed = bool(state and state.get("owner") == "codex-compressor")
        configured = installed and bool(result["stages"]["configured"])
        result["stages"]["configured"] = configured
        trust_approved = self._trust_approval_status(state)
        result["stages"]["trust_approved"] = trust_approved
        result.update(
            {
                "installed": installed,
                "version": state.get("version") if state else None,
                "mode": state.get("mode") if state else None,
                "codex_home": str(self.codex_home),
                "config_values": result["token_budget"],
                "configured": configured,
                "trust_approved": trust_approved,
            }
        )
        return result

    def status(self) -> dict[str, Any]:
        """설치 상태를 확인합니다."""

        return self._status(live_probe=False)

    def doctor(self, *, live_probe: bool = False) -> dict[str, Any]:
        """설정 파싱과 CLI 기능을 안전하게 진단합니다."""

        result = self._status(live_probe=live_probe)
        result["probe_is_diagnostic_only"] = True
        return result
