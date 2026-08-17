"""같은 세션의 체크포인트를 컨텍스트 교체 뒤에 복원하는 훅 런타임."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REQUEST_MARKER = "LONG_TASK_CONTINUITY_SUMMARY_REQUEST_V1"
RETRY_MARKER = "LONG_TASK_CONTINUITY_RETRY_V1"
MAX_ADDITIONAL_CONTEXT_BYTES = 10_000
# 외부에서 사용하던 이름은 유지하되, 제한은 이제 추가 문맥의 바이트 수로 계산한다.
MAX_SUMMARY_CHARS = MAX_ADDITIONAL_CONTEXT_BYTES
STATE_VERSION = 2
COMPACT_TRIGGERS = frozenset({"auto", "manual"})
OBSERVATION_VERSION = 2
TRANSCRIPT_CHUNK_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
TRANSCRIPT_POLL_SECONDS = 0.05


class _TranscriptError(ValueError):
    """체크포인트 탐색에 사용할 transcript를 읽거나 해석하지 못한 경우."""


class _IncompleteTranscript(Exception):
    """마지막 레코드가 아직 물리적 LF로 끝나지 않은 경우."""


def _codex_home() -> Path:
    """연속성 상태의 기준 디렉터리를 반환한다."""
    override = os.environ.get("LONG_TASK_CONTINUITY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _safe_session_id(value: Any) -> str:
    """세션 디렉터리 이름으로 사용할 안전한 식별자를 만든다."""
    session_id = str(value or "").strip()
    if not session_id:
        raise ValueError("hook input is missing session_id")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    if safe in {"", ".", ".."}:
        raise ValueError("hook input has an invalid session_id")
    return safe


def _turn_id(event: dict[str, Any]) -> str:
    """훅 이벤트에서 공백이 제거된 turn 식별자를 반환한다."""
    return str(event.get("turn_id") or "").strip()


def _session_dir(event: dict[str, Any]) -> Path:
    """이벤트의 세션 상태 디렉터리를 반환한다."""
    return _codex_home() / "continuity" / _safe_session_id(event.get("session_id"))


def _read_json(path: Path, default: Any) -> Any:
    """선택적 내부 JSON 상태를 읽고 없거나 손상되면 기본값을 반환한다."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, UnicodeError):
        return default


def _atomic_write_text(path: Path, text: str) -> None:
    """임시 파일과 교체로 텍스트를 원자적으로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any) -> None:
    """JSON 상태를 원자적으로 저장한다."""
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _observation_key(event: dict[str, Any]) -> str | None:
    """관찰할 지원 훅과 세부 조건을 정확히 식별한다."""
    event_name = event.get("hook_event_name")
    if event_name == "PreToolUse" and event.get("tool_name") == "new_context":
        return "PreToolUse(new_context)"
    if event_name in {"PreCompact", "PostCompact"}:
        trigger = event.get("trigger")
        if trigger in COMPACT_TRIGGERS:
            return f"{event_name}({trigger})"
    if event_name == "SessionStart" and event.get("source") == "compact":
        return "SessionStart(compact)"
    return None


def _observation_surface(event: dict[str, Any]) -> str | None:
    """이벤트 또는 transcript 세션 메타데이터에서 실행 표면을 확인한다."""
    for field in ("surface", "runtime_surface", "execution_surface"):
        value = event.get(field)
        if not isinstance(value, str):
            continue
        normalized = (
            value.strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
        )
        if normalized == "cli":
            return "cli"
        if normalized in {"desktop", "app_server", "desktop_app_server"}:
            return "desktop_app_server"

    path_value = event.get("transcript_path")
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    try:
        with Path(path_value).open("rb") as stream:
            line = stream.readline()
        first_record = json.loads(line.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(first_record, dict) or first_record.get("type") != "session_meta":
        return None
    payload = first_record.get("payload")
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if source == "cli":
        return "cli"
    if source in {"app_server", "desktop", "vscode"}:
        return "desktop_app_server"
    return None


def _record_observation(event: dict[str, Any], key: str) -> None:
    """지원 훅 이벤트를 세션별 JSON에 원자적으로 누적한다."""
    session_dir = _session_dir(event)
    observation_path = session_dir / "observations.json"
    observations = _read_json(observation_path, {})
    if not isinstance(observations, dict):
        observations = {}
    old_version = observations.get("version")
    events = observations.get("events")
    if not isinstance(events, dict):
        events = {}
    observed_at = datetime.now(UTC).isoformat()
    item = events.get(key)
    if not isinstance(item, dict):
        item = {
            "count": 0,
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
        }
    previous_count = item.get("count")
    item["count"] = (
        previous_count
        if isinstance(previous_count, int) and previous_count >= 0
        else 0
    ) + 1
    if not isinstance(item.get("first_seen_at"), str):
        item["first_seen_at"] = observed_at
    item["last_seen_at"] = observed_at
    surface = _observation_surface(event)
    if surface is not None:
        item_surfaces = item.get("surfaces")
        if not isinstance(item_surfaces, list):
            item_surfaces = []
        if surface not in item_surfaces:
            item_surfaces.append(surface)
        item["surfaces"] = sorted(item_surfaces)
    events[key] = item

    # v1의 이벤트 순서 추정값은 v2의 검증 횟수로 승격하지 않는다.
    if old_version != OBSERVATION_VERSION:
        verified_count = 0
        observations.pop("last_verified_turn_id", None)
        observations.pop("last_verified_hash", None)
        observations.pop("last_verified_at", None)
    else:
        verified_count = observations.get("rollovers_verified", 0)
        if not isinstance(verified_count, int) or verified_count < 0:
            verified_count = 0
    observations.update(
        {
            "version": OBSERVATION_VERSION,
            "session_id": _safe_session_id(event.get("session_id")),
            "first_seen_at": (
                observations["first_seen_at"]
                if isinstance(observations.get("first_seen_at"), str)
                else observed_at
            ),
            "last_seen_at": observed_at,
            "events": events,
            "surfaces": sorted(
                {
                    value
                    for item_value in events.values()
                    if isinstance(item_value, dict)
                    for value in (item_value.get("surfaces") or [])
                    if value in {"cli", "desktop_app_server"}
                }
            ),
            "rollovers_verified": verified_count,
        }
    )
    observations.pop("rollover_phase", None)
    _atomic_write_json(observation_path, observations)


def _record_verified_rollover(
    event: dict[str, Any], checkpoint_hash: str, turn_id: str
) -> None:
    """정확히 주입된 체크포인트를 관찰 상태에 한 번만 기록한다."""
    session_dir = _session_dir(event)
    observation_path = session_dir / "observations.json"
    observations = _read_json(observation_path, {})
    if not isinstance(observations, dict):
        observations = {}
    if observations.get("version") != OBSERVATION_VERSION:
        observations["version"] = OBSERVATION_VERSION
        observations["rollovers_verified"] = 0
        observations.pop("last_verified_turn_id", None)
        observations.pop("last_verified_hash", None)
        observations.pop("last_verified_at", None)
    last_turn = observations.get("last_verified_turn_id")
    last_hash = observations.get("last_verified_hash")
    if last_turn == turn_id and last_hash == checkpoint_hash:
        return
    count = observations.get("rollovers_verified", 0)
    if not isinstance(count, int) or count < 0:
        count = 0
    verified_at = datetime.now(UTC).isoformat()
    observations.update(
        {
            "rollovers_verified": count + 1,
            "last_verified_turn_id": turn_id,
            "last_verified_hash": checkpoint_hash,
            "last_verified_at": verified_at,
        }
    )
    _atomic_write_json(observation_path, observations)


def _message_text(payload: dict[str, Any]) -> str:
    """response_item 메시지에서 텍스트 조각을 합친다."""
    if payload.get("type") != "message":
        return ""
    parts: list[str] = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def _record_turn_id(record: dict[str, Any]) -> str | None:
    """레코드에 기록된 직접 turn 식별자를 반환한다."""
    payload = record.get("payload")
    containers = [record]
    if isinstance(payload, dict):
        containers.append(payload)
    for container in containers:
        metadata = container.get("internal_chat_message_metadata_passthrough")
        if isinstance(metadata, dict):
            value = metadata.get("turn_id")
            if value is not None and str(value).strip():
                return str(value).strip()
    if record.get("type") == "turn_context" and isinstance(payload, dict):
        value = payload.get("turn_id")
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _raw_transcript_lines(path_value: Any) -> tuple[list[bytes], bool]:
    """최근 8MiB에서 LF로 끝난 물리 레코드만 역순으로 읽는다."""
    if not isinstance(path_value, str) or not path_value.strip():
        raise _TranscriptError("transcript_path is required for checkpoint discovery")
    path = Path(path_value)
    try:
        size = path.stat().st_size
        if size == 0:
            raise _TranscriptError("transcript is empty")
        start = max(0, size - MAX_TRANSCRIPT_BYTES)
        chunks: list[bytes] = []
        position = size
        with path.open("rb") as stream:
            while position > start:
                length = min(TRANSCRIPT_CHUNK_BYTES, position - start)
                position -= length
                stream.seek(position)
                chunk = stream.read(length)
                if len(chunk) != length:
                    raise _TranscriptError("transcript changed while it was being read")
                chunks.append(chunk)
        data = b"".join(reversed(chunks))
    except _TranscriptError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _TranscriptError(f"unable to read transcript: {exc}") from exc

    # Tail 시작점이 물리 레코드 중간이면 해당 부분은 stale prefix로 버린다.
    if start:
        first_lf = data.find(b"\n")
        if first_lf < 0:
            return [], not data == b""
        data = data[first_lf + 1 :]
    incomplete = not data.endswith(b"\n")
    if incomplete:
        if b"\n" not in data:
            return [], True
        data, _trailing = data.rsplit(b"\n", 1)
    else:
        data = data[:-1]
    if not data:
        return [], incomplete
    return list(reversed(data.split(b"\n"))), incomplete


def _is_marker(record: dict[str, Any]) -> bool:
    """체크포인트 요청 또는 재시도 출력 레코드인지 확인한다."""
    if record.get("type") != "response_item":
        return False
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    text = _message_text(payload)
    if payload.get("type") == "message" and payload.get("role") == "developer":
        return REQUEST_MARKER in text
    output = payload.get("output")
    return (
        payload.get("type") == "function_call_output"
        and isinstance(output, str)
        and RETRY_MARKER in output
    )


def _scan_relevant_records(event: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """최신 marker 또는 현재 turn 경계까지 역방향으로 JSONL을 해석한다."""
    raw_lines, incomplete = _raw_transcript_lines(event.get("transcript_path"))
    target_turn_id = _turn_id(event)
    parsed_reverse: list[dict[str, Any]] = []
    malformed_newer: str | None = None
    pending_marker_index: int | None = None
    marker_found = False
    target_boundary_found = False
    supported_records = 0

    for raw_line in raw_lines:
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if malformed_newer is None:
                malformed_newer = f"transcript contains malformed JSON in the relevant tail: {exc}"
            continue
        if not isinstance(record, dict):
            if malformed_newer is None:
                malformed_newer = "transcript relevant tail line must contain a JSON object"
            continue
        parsed_reverse.append(record)
        record_type = record.get("type")
        if record_type in {"turn_context", "response_item"}:
            supported_records += 1
            if not isinstance(record.get("payload"), dict):
                if malformed_newer is None:
                    malformed_newer = (
                        f"transcript relevant {record_type} record has an invalid payload"
                    )
        elif not isinstance(record_type, str) or not record_type:
            if malformed_newer is None:
                malformed_newer = "transcript relevant tail record has no supported record type"
        direct_turn_id = _record_turn_id(record)
        if pending_marker_index is not None and record.get("type") == "turn_context":
            context_turn_id = _record_turn_id(record)
            if context_turn_id == target_turn_id:
                marker_found = True
                target_boundary_found = True
                break
            pending_marker_index = None
        if _is_marker(record):
            if direct_turn_id == target_turn_id or (
                target_turn_id and direct_turn_id is None and pending_marker_index is None
            ):
                if direct_turn_id == target_turn_id:
                    marker_found = True
                    break
                pending_marker_index = len(parsed_reverse) - 1
            elif direct_turn_id is None and not target_turn_id:
                pending_marker_index = len(parsed_reverse) - 1
            continue
        if record.get("type") == "turn_context" and direct_turn_id == target_turn_id:
            target_boundary_found = True
            break

    if marker_found:
        if malformed_newer is not None:
            raise _TranscriptError(malformed_newer)
    elif target_boundary_found:
        if malformed_newer is not None:
            raise _TranscriptError(malformed_newer)
    elif malformed_newer is not None:
        # marker가 전혀 없으면 bounded tail 자체가 현재 탐색 대상이므로 진단한다.
        raise _TranscriptError(malformed_newer)

    if incomplete:
        if not parsed_reverse:
            raise _IncompleteTranscript()
        return list(reversed(parsed_reverse)), True

    if target_turn_id and not marker_found and not target_boundary_found:
        raise _TranscriptError(
            "transcript has no direct same-turn marker or matching turn boundary"
        )

    if not parsed_reverse:
        raise _TranscriptError("transcript has no supported turn_context or response_item records")
    if supported_records == 0:
        raise _TranscriptError(
            "transcript has no supported turn_context or response_item records"
        )
    return list(reversed(parsed_reverse)), incomplete


def _transcript_records(path_value: Any) -> list[dict[str, Any]]:
    """bounded tail의 완전한 JSONL 레코드를 반환한다."""
    records, _incomplete = _scan_relevant_records(
        {"transcript_path": path_value, "turn_id": ""}
    )
    return records


def _find_summary_candidate_once(event: dict[str, Any]) -> str | None:
    """현재 turn에서 마지막 체크포인트 후보를 찾는다."""
    records, incomplete = _scan_relevant_records(event)
    if incomplete:
        raise _IncompleteTranscript()
    target_turn_id = _turn_id(event)
    active_turn_id = ""
    marker_index: int | None = None
    assistant_messages: list[tuple[int, str, str | None]] = []

    for index, record in enumerate(records):
        if record.get("type") == "turn_context":
            active_turn_id = _record_turn_id(record) or ""
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        direct_turn_id = _record_turn_id(record)
        effective_turn_id = direct_turn_id or active_turn_id
        if target_turn_id and effective_turn_id and effective_turn_id != target_turn_id:
            continue
        if _is_marker(record):
            marker_index = index
            continue
        if payload.get("role") != "assistant":
            continue
        text = _message_text(payload)
        if text:
            phase = payload.get("phase")
            assistant_messages.append(
                (index, text, phase if isinstance(phase, str) else None)
            )

    if marker_index is not None:
        candidates = [
            (text, phase)
            for index, text, phase in assistant_messages
            if index > marker_index
        ]
        final_answers = [text for text, phase in candidates if phase == "final_answer"]
        if final_answers:
            return final_answers[-1]
        return candidates[-1][0] if candidates else None

    tagged = [
        text
        for _, text, _ in assistant_messages
        if "<summary" in text.lower() or "## current objective" in text.lower()
    ]
    return tagged[-1] if tagged else None


def _find_summary_candidate(event: dict[str, Any], wait_seconds: float = 0.0) -> str | None:
    """transcript 기록이 나타날 때까지 체크포인트 후보를 찾는다."""
    deadline = time.monotonic() + min(max(wait_seconds, 0.0), 1.5)
    while True:
        try:
            summary = _find_summary_candidate_once(event)
        except _IncompleteTranscript:
            summary = None
        if summary is not None or time.monotonic() >= deadline:
            return summary
        time.sleep(TRANSCRIPT_POLL_SECONDS)


def _canonical_checkpoint_text(text: str) -> str:
    """체크포인트의 양끝 공백을 제거한 정규 텍스트를 반환한다."""
    return text.strip()


def _checkpoint_sha256(text: str) -> str:
    """정규 체크포인트 텍스트를 대문자 SHA-256으로 해시한다."""
    canonical = _canonical_checkpoint_text(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()


def _read_checkpoint(path: Path) -> tuple[str, str]:
    """체크포인트 파일을 읽어 정규 텍스트와 해시를 반환한다."""
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            text = stream.read()
    except FileNotFoundError as exc:
        raise _TranscriptError(f"checkpoint file is missing: {path.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise _TranscriptError(f"unable to read checkpoint {path.name}: {exc}") from exc
    canonical = _canonical_checkpoint_text(text)
    if not canonical:
        raise _TranscriptError(f"checkpoint file is empty: {path.name}")
    return canonical, _checkpoint_sha256(canonical)


def _build_additional_context(summary: str) -> str:
    """체크포인트를 SessionStart 추가 문맥으로 감싼다."""
    return (
        "<long_task_continuity_checkpoint>\n"
        "This English checkpoint was written by this same thread immediately before "
        "the previous context window was replaced. Treat it as the authoritative "
        "continuation state. The rollover and any checkpoint instruction to call "
        "`new_context` have already been completed; do not call `new_context` again "
        "for that instruction. Continue with the first post-rollover task action and "
        "do not repeat completed work unless new evidence or verification requires "
        "it.\n\n"
        f"{_canonical_checkpoint_text(summary)}\n"
        "</long_task_continuity_checkpoint>"
    )


def _summary_problem(summary: str | None) -> str | None:
    """체크포인트가 없거나 추가 문맥 바이트 제한을 넘었는지 진단한다."""
    if summary is None or not _canonical_checkpoint_text(summary):
        return (
            f"[{RETRY_MARKER}] No checkpoint summary was found after the rollover "
            "request. Write one visible English assistant message that summarizes the "
            "current objective, plan status, completed and in-progress work, failed "
            "attempts, review status, current work, and exact next actions. Then call "
            "`new_context` again. The recommended format is optional."
        )
    canonical = _canonical_checkpoint_text(summary)
    context_bytes = len(_build_additional_context(canonical).encode("utf-8"))
    if context_bytes > MAX_ADDITIONAL_CONTEXT_BYTES:
        return (
            f"[{RETRY_MARKER}] The checkpoint additionalContext is {context_bytes:,} "
            f"UTF-8 bytes, which exceeds the {MAX_ADDITIONAL_CONTEXT_BYTES:,}-byte "
            "limit. Rewrite it without truncation, then call `new_context` again."
        )
    return None


def _load_state(session_dir: Path) -> dict[str, Any]:
    """내부 상태 JSON을 읽고 새 상태의 기본값을 보장한다."""
    state = _read_json(session_dir / "state.json", {})
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("window_count"), int) or state.get("window_count", 0) < 0:
        state["window_count"] = 0
    return state


def _new_active_rollover(event: dict[str, Any], generated_hash: str) -> dict[str, Any]:
    """pending 단계의 새 롤오버 상태를 만든다."""
    now = datetime.now(UTC).isoformat()
    return {
        "turn_id": _turn_id(event),
        "phase": "pending",
        "generated_sha256": generated_hash,
        "pending_sha256": generated_hash,
        "current_sha256": None,
        "injected_sha256": None,
        "trigger": event.get("trigger"),
        "generated_at": now,
        "pending_at": now,
    }


def _save_pending(event: dict[str, Any], summary: str) -> None:
    """pending을 저장하고 재독해 생성 해시와 일치하는지 확인한다."""
    canonical = _canonical_checkpoint_text(summary)
    generated_hash = _checkpoint_sha256(canonical)
    session_dir = _session_dir(event)
    pending_path = session_dir / "pending.md"
    _atomic_write_text(pending_path, canonical + "\n")
    persisted, pending_hash = _read_checkpoint(pending_path)
    if persisted != canonical or pending_hash != generated_hash:
        raise _TranscriptError("pending checkpoint changed while it was being saved")
    state = _load_state(session_dir)
    state["version"] = STATE_VERSION
    state["active_rollover"] = _new_active_rollover(event, generated_hash)
    _atomic_write_json(session_dir / "state.json", state)


def _pending_matches(event: dict[str, Any]) -> bool:
    """현재 pending이 같은 turn의 생성 해시 체인과 일치하는지 확인한다."""
    session_dir = _session_dir(event)
    state = _load_state(session_dir)
    active = state.get("active_rollover")
    if state.get("version") != STATE_VERSION or not isinstance(active, dict):
        return False
    if active.get("phase") != "pending" or active.get("turn_id") != _turn_id(event):
        return False
    generated = active.get("generated_sha256")
    pending_hash = active.get("pending_sha256")
    if not isinstance(generated, str) or generated != pending_hash:
        return False
    try:
        _text, file_hash = _read_checkpoint(session_dir / "pending.md")
    except _TranscriptError:
        return False
    return file_hash == pending_hash


def _update_pending_trigger(event: dict[str, Any]) -> None:
    """재사용한 pending에도 현재 압축 trigger를 기록한다."""
    session_dir = _session_dir(event)
    state = _load_state(session_dir)
    active = state.get("active_rollover")
    if state.get("version") != STATE_VERSION or not isinstance(active, dict):
        raise _TranscriptError("pending checkpoint state disappeared while it was reused")
    active["trigger"] = event.get("trigger")
    state["active_rollover"] = active
    _atomic_write_json(session_dir / "state.json", state)


def _invalidate_active_rollover(event: dict[str, Any]) -> None:
    """새 압축 시도 전에 이전 롤오버의 주입 자격을 제거한다."""
    session_dir = _session_dir(event)
    state = _load_state(session_dir)
    if "active_rollover" not in state:
        return
    state.pop("active_rollover", None)
    _atomic_write_json(session_dir / "state.json", state)


def _deny_pre_tool_use(reason: str) -> None:
    """new_context 호출을 거부하는 훅 응답을 출력한다."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()


def _compact_failure(reason: str) -> None:
    """압축을 중단하는 훅 응답을 출력한다."""
    sys.stdout.write(
        json.dumps(
            {
                "continue": False,
                "stopReason": reason,
                "systemMessage": reason,
            },
            ensure_ascii=False,
        )
    )
    sys.stdout.flush()


def _diagnose(reason: str) -> str:
    """진단을 stderr와 공용 오류 로그에 남기고 재시도 문구를 반환한다."""
    _report_error(reason)
    return f"[{RETRY_MARKER}] {reason}"


def _handle_pre_tool_use(event: dict[str, Any]) -> int:
    """new_context 직전에 체크포인트를 준비하거나 호출을 거부한다."""
    if event.get("tool_name") != "new_context":
        return 0
    target_turn_id = _turn_id(event)
    if not target_turn_id:
        _deny_pre_tool_use(_diagnose("hook input is missing turn_id"))
        return 0
    try:
        summary = _find_summary_candidate(event, wait_seconds=1.5)
        problem = _summary_problem(summary)
        if problem:
            _deny_pre_tool_use(problem)
            return 0
        _save_pending(event, summary or "")
    except (_TranscriptError, OSError, ValueError) as exc:
        _deny_pre_tool_use(_diagnose(str(exc)))
    return 0


def _handle_pre_compact(event: dict[str, Any]) -> int:
    """자동 또는 수동 압축 전에 같은 turn의 유효한 체크포인트를 확보한다."""
    if event.get("trigger") not in COMPACT_TRIGGERS:
        return 0
    if not _turn_id(event):
        problem = _diagnose("hook input is missing turn_id")
        _compact_failure(problem)
        return 0
    if _pending_matches(event):
        try:
            _update_pending_trigger(event)
        except (OSError, ValueError) as exc:
            _compact_failure(_diagnose(str(exc)))
        return 0
    try:
        _invalidate_active_rollover(event)
        summary = _find_summary_candidate(event, wait_seconds=1.5)
        problem = _summary_problem(summary)
        if problem:
            _compact_failure(problem)
            return 0
        _save_pending(event, summary or "")
    except (_TranscriptError, OSError, ValueError) as exc:
        _compact_failure(_diagnose(str(exc)))
    return 0


def _handle_post_compact(event: dict[str, Any]) -> int:
    """압축 완료 뒤 검증된 pending을 current로 원자적으로 승격한다."""
    if event.get("trigger") not in COMPACT_TRIGGERS:
        return 0
    session_dir = _session_dir(event)
    state = _load_state(session_dir)
    active = state.get("active_rollover")
    if (
        state.get("version") != STATE_VERSION
        or not isinstance(active, dict)
        or active.get("phase") != "pending"
        or not _turn_id(event)
        or active.get("turn_id") != _turn_id(event)
    ):
        _report_error("PostCompact ignored because the active pending turn does not match")
        return 0
    generated = active.get("generated_sha256")
    pending_hash = active.get("pending_sha256")
    pending_path = session_dir / "pending.md"
    current_path = session_dir / "current.md"
    try:
        _pending_text, recomputed_pending = _read_checkpoint(pending_path)
        if (
            not isinstance(generated, str)
            or recomputed_pending != generated
            or pending_hash != recomputed_pending
        ):
            raise _TranscriptError("pending checkpoint hash chain verification failed")
        current_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending_path, current_path)
        _current_text, current_hash = _read_checkpoint(current_path)
        if current_hash != generated or current_hash != recomputed_pending:
            raise _TranscriptError("current checkpoint hash chain verification failed")
        now = datetime.now(UTC).isoformat()
        active.update(
            {
                "phase": "committed",
                "current_sha256": current_hash,
                "committed_at": now,
                "trigger": event.get("trigger"),
            }
        )
        state["active_rollover"] = active
        state["window_count"] = state.get("window_count", 0) + 1
        _atomic_write_json(session_dir / "state.json", state)
    except (OSError, ValueError, _TranscriptError) as exc:
        _report_error(str(exc))
    return 0


def _session_start_failure(reason: str) -> None:
    """검증 실패를 stale 체크포인트 없이 짧은 추가 문맥으로 알린다."""
    _report_error(reason)
    context = f"[{RETRY_MARKER}] Checkpoint verification failed; no checkpoint was injected."
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    serialized = json.dumps(output, ensure_ascii=False)
    sys.stdout.write(serialized)
    sys.stdout.flush()


def _handle_session_start(event: dict[str, Any]) -> int:
    """압축으로 시작된 새 창에 검증된 롤오버의 current를 주입한다."""
    if event.get("source") != "compact":
        return 0
    session_dir = _session_dir(event)
    current_path = session_dir / "current.md"
    if not current_path.exists():
        _session_start_failure("SessionStart current checkpoint file is missing")
        return 0
    state = _load_state(session_dir)
    active = state.get("active_rollover")
    stored_turn_id = active.get("turn_id") if isinstance(active, dict) else None
    if (
        state.get("version") == STATE_VERSION
        and isinstance(active, dict)
        and active.get("phase") == "injected"
        and isinstance(stored_turn_id, str)
        and bool(stored_turn_id.strip())
    ):
        return 0
    if (
        state.get("version") != STATE_VERSION
        or not isinstance(active, dict)
        or active.get("phase") != "committed"
        or not isinstance(stored_turn_id, str)
        or not stored_turn_id.strip()
    ):
        _session_start_failure("SessionStart checkpoint state is not a committed v2 same-turn rollover")
        return 0
    generated = active.get("generated_sha256")
    pending_hash = active.get("pending_sha256")
    current_expected = active.get("current_sha256")
    try:
        summary, current_hash = _read_checkpoint(current_path)
        if (
            not isinstance(generated, str)
            or current_hash != generated
            or current_hash != pending_hash
            or current_hash != current_expected
        ):
            raise _TranscriptError("SessionStart current checkpoint hash chain verification failed")
        context = _build_additional_context(summary)
        context_bytes = len(context.encode("utf-8"))
        if context_bytes > MAX_ADDITIONAL_CONTEXT_BYTES:
            raise _TranscriptError(
                f"SessionStart additionalContext is {context_bytes:,} UTF-8 bytes"
            )
    except (_TranscriptError, OSError, ValueError) as exc:
        _session_start_failure(str(exc))
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    # JSON 직렬화 결과를 그대로 한 번 쓰고 flush한 뒤에만 주입 완료로 기록한다.
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    now = datetime.now(UTC).isoformat()
    active.update(
        {
            "phase": "injected",
            "injected_sha256": current_hash,
            "injected_at": now,
        }
    )
    state["active_rollover"] = active
    try:
        _atomic_write_json(session_dir / "state.json", state)
        _record_verified_rollover(event, current_hash, stored_turn_id)
    except (OSError, ValueError) as exc:
        _report_error(str(exc))
    return 0


def _handle_event(event: dict[str, Any]) -> int:
    """지원하는 훅 이벤트를 분기하고 나머지는 아무 작업도 하지 않는다."""
    observation_key = _observation_key(event)
    if observation_key is not None:
        try:
            _record_observation(event, observation_key)
        except (OSError, ValueError) as exc:
            reason = _diagnose(str(exc))
            if observation_key == "PreToolUse(new_context)":
                _deny_pre_tool_use(reason)
            elif observation_key.startswith("PreCompact("):
                _compact_failure(reason)
            elif observation_key == "SessionStart(compact)":
                _session_start_failure(reason)
            return 0
    event_name = event.get("hook_event_name")
    if event_name == "PreToolUse":
        return _handle_pre_tool_use(event)
    if event_name == "PreCompact":
        return _handle_pre_compact(event)
    if event_name == "PostCompact":
        return _handle_post_compact(event)
    if event_name == "SessionStart":
        return _handle_session_start(event)
    return 0


def _report_error(message: str) -> None:
    """오류를 stderr와 CODEX_HOME 아래 공용 로그에 기록한다."""
    print(f"codex-compressor continuity: {message}", file=sys.stderr)
    try:
        _atomic_write_text(
            _codex_home() / "continuity" / ".last-error.log",
            f"{datetime.now(UTC).isoformat()} {message}\n",
        )
    except OSError:
        pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """명령줄 인자를 파싱한다."""
    parser = argparse.ArgumentParser()
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """표준 입력의 훅 이벤트를 처리한다."""
    _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        _report_error(f"invalid hook input: {exc}")
        return 1
    if not isinstance(event, dict):
        _report_error("hook input must be a JSON object")
        return 1
    try:
        return _handle_event(event)
    except (OSError, ValueError) as exc:
        _report_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
