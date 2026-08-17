"""같은 세션의 체크포인트를 컨텍스트 교체 뒤에 복원하는 훅 런타임."""

from __future__ import annotations

import argparse
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
MAX_SUMMARY_CHARS = 32_000
STATE_VERSION = 1
COMPACT_TRIGGERS = frozenset({"auto", "manual"})
OBSERVATION_VERSION = 1


class _TranscriptError(ValueError):
    """체크포인트 탐색에 사용할 transcript를 읽거나 해석하지 못한 경우."""


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


def _session_dir(event: dict[str, Any]) -> Path:
    """이벤트의 세션 상태 디렉터리를 반환한다."""
    return _codex_home() / "continuity" / _safe_session_id(event.get("session_id"))


def _read_json(path: Path, default: Any) -> Any:
    """선택적 내부 JSON 상태를 읽고 없거나 손상되면 기본값을 반환한다."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
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
        with Path(path_value).open(encoding="utf-8") as stream:
            first_record = json.loads(stream.readline())
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


def _advance_rollover(phase: int, count: int, key: str) -> tuple[int, int]:
    """관찰 순서를 압축 상태로 유지하고 완료된 롤오버 수를 갱신한다."""
    if key.startswith("PreCompact("):
        return 1, count
    if key.startswith("PostCompact(") and phase == 1:
        return 2, count
    if key == "SessionStart(compact)" and phase == 2:
        return 0, count + 1
    return phase, count


def _record_observation(event: dict[str, Any], key: str) -> None:
    """지원 훅 이벤트를 세션별 JSON에 원자적으로 누적한다."""
    session_dir = _session_dir(event)
    observation_path = session_dir / "observations.json"
    observations = _read_json(observation_path, {})
    if not isinstance(observations, dict):
        observations = {}
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
    phase = observations.get("rollover_phase", 0)
    count = observations.get("rollovers_verified", 0)
    if not isinstance(phase, int) or phase not in {0, 1, 2}:
        phase = 0
    if not isinstance(count, int) or count < 0:
        count = 0
    phase, count = _advance_rollover(phase, count, key)
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
            "rollover_phase": phase,
            "rollovers_verified": count,
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


def _transcript_records(path_value: Any) -> list[dict[str, Any]]:
    """JSONL transcript를 엄격히 읽어 체크포인트 탐색 가능한 레코드로 반환한다."""
    if not isinstance(path_value, str) or not path_value.strip():
        raise _TranscriptError("transcript_path is required for checkpoint discovery")

    path = Path(path_value)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _TranscriptError(f"unable to read transcript: {exc}") from exc
    if not lines:
        raise _TranscriptError("transcript is empty")

    records: list[dict[str, Any]] = []
    supported_records = 0
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _TranscriptError(
                f"transcript contains malformed JSON on line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise _TranscriptError(
                f"transcript line {line_number} must contain a JSON object"
            )
        record_type = record.get("type")
        if not isinstance(record_type, str) or not record_type:
            raise _TranscriptError(
                f"transcript line {line_number} has no supported record type"
            )
        if record_type in {"turn_context", "response_item"}:
            if not isinstance(record.get("payload"), dict):
                raise _TranscriptError(
                    f"transcript line {line_number} has an invalid {record_type} payload"
                )
            supported_records += 1
        records.append(record)

    if supported_records == 0:
        raise _TranscriptError(
            "transcript has no supported turn_context or response_item records"
        )
    return records


def _find_summary_candidate_once(event: dict[str, Any]) -> str | None:
    """현재 turn에서 마지막 체크포인트 후보를 찾는다."""
    records = _transcript_records(event.get("transcript_path"))
    target_turn_id = str(event.get("turn_id") or "")
    active_turn_id = ""
    marker_index: int | None = None
    assistant_messages: list[tuple[int, str, str | None]] = []

    for index, record in enumerate(records):
        if record.get("type") == "turn_context":
            payload = record["payload"]
            active_turn_id = str(payload.get("turn_id") or "")
            continue
        if target_turn_id and active_turn_id and active_turn_id != target_turn_id:
            continue

        if record.get("type") != "response_item":
            continue
        payload = record["payload"]
        payload_type = payload.get("type")
        role = payload.get("role")
        text = _message_text(payload)

        is_request = (
            payload_type == "message"
            and role == "developer"
            and REQUEST_MARKER in text
        )
        output = payload.get("output")
        is_retry = (
            payload_type == "function_call_output"
            and isinstance(output, str)
            and RETRY_MARKER in output
        )
        if is_request or is_retry:
            marker_index = index
            continue

        if role != "assistant":
            continue
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
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        summary = _find_summary_candidate_once(event)
        if summary is not None or time.monotonic() >= deadline:
            return summary
        time.sleep(0.05)


def _summary_problem(summary: str | None) -> str | None:
    """체크포인트가 없거나 허용 크기를 넘었는지 진단한다."""
    if summary is None or not summary.strip():
        return (
            f"[{RETRY_MARKER}] No checkpoint summary was found after the rollover "
            "request. Write one visible English assistant message that summarizes the "
            "current objective, plan status, completed and in-progress work, failed "
            "attempts, review status, current work, and exact next actions. Then call "
            "`new_context` again. The recommended format is optional."
        )
    if len(summary) > MAX_SUMMARY_CHARS:
        return (
            f"[{RETRY_MARKER}] The checkpoint summary is {len(summary):,} characters, "
            f"which exceeds the {MAX_SUMMARY_CHARS:,}-character limit. Rewrite it in "
            "concise English without losing information needed to continue, then call "
            "`new_context` again. The recommended format is optional."
        )
    return None


def _load_state(session_dir: Path) -> dict[str, Any]:
    """내부 상태 JSON을 읽고 기본 버전을 보장한다."""
    state = _read_json(session_dir / "state.json", {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", STATE_VERSION)
    state.setdefault("window_count", 0)
    return state


def _save_pending(event: dict[str, Any], summary: str) -> None:
    """pending 체크포인트와 관련 상태를 원자적으로 저장한다."""
    session_dir = _session_dir(event)
    _atomic_write_text(session_dir / "pending.md", summary.rstrip() + "\n")
    state = _load_state(session_dir)
    state.update(
        {
            "pending_turn_id": str(event.get("turn_id") or ""),
            "pending_trigger": event.get("trigger"),
            "pending_updated_at": datetime.now(UTC).isoformat(),
        }
    )
    _atomic_write_json(session_dir / "state.json", state)


def _pending_summary(session_dir: Path) -> str | None:
    """유효한 pending 체크포인트를 읽고 아니면 None을 반환한다."""
    pending_path = session_dir / "pending.md"
    try:
        pending = pending_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return None
    return pending if _summary_problem(pending) is None else None


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


def _diagnose(reason: str) -> str:
    """진단을 stderr와 공용 오류 로그에 남기고 재시도 문구를 반환한다."""
    _report_error(reason)
    return f"[{RETRY_MARKER}] {reason}"


def _handle_pre_tool_use(event: dict[str, Any]) -> int:
    """new_context 직전에 체크포인트를 준비하거나 호출을 거부한다."""
    if event.get("tool_name") != "new_context":
        return 0
    try:
        summary = _find_summary_candidate(event, wait_seconds=1.5)
    except _TranscriptError as exc:
        _deny_pre_tool_use(_diagnose(str(exc)))
        return 0
    problem = _summary_problem(summary)
    if problem:
        _deny_pre_tool_use(problem)
        return 0
    _save_pending(event, summary or "")
    return 0


def _handle_pre_compact(event: dict[str, Any]) -> int:
    """자동 또는 수동 압축 전에 유효한 체크포인트를 확보한다."""
    if event.get("trigger") not in COMPACT_TRIGGERS:
        return 0
    session_dir = _session_dir(event)
    if _pending_summary(session_dir) is not None:
        return 0

    try:
        summary = _find_summary_candidate(event, wait_seconds=1.5)
    except _TranscriptError as exc:
        problem = _diagnose(str(exc))
    else:
        problem = _summary_problem(summary)
    if problem:
        sys.stdout.write(
            json.dumps(
                {
                    "continue": False,
                    "stopReason": problem,
                    "systemMessage": problem,
                },
                ensure_ascii=False,
            )
        )
        return 0
    _save_pending(event, summary or "")
    return 0


def _handle_post_compact(event: dict[str, Any]) -> int:
    """압축 완료 뒤 pending을 current로 원자적으로 승격한다."""
    if event.get("trigger") not in COMPACT_TRIGGERS:
        return 0
    session_dir = _session_dir(event)
    pending_path = session_dir / "pending.md"
    current_path = session_dir / "current.md"
    if _pending_summary(session_dir) is None:
        return 0

    current_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(pending_path, current_path)
    state = _load_state(session_dir)
    state["window_count"] = int(state.get("window_count") or 0) + 1
    state["last_compacted_turn_id"] = str(event.get("turn_id") or "")
    state["last_compacted_trigger"] = event.get("trigger")
    state["last_compacted_at"] = datetime.now(UTC).isoformat()
    for key in ("pending_turn_id", "pending_trigger", "pending_updated_at"):
        state.pop(key, None)
    _atomic_write_json(session_dir / "state.json", state)
    return 0


def _handle_session_start(event: dict[str, Any]) -> int:
    """압축으로 시작된 새 창에 같은 세션의 current 체크포인트를 주입한다."""
    if event.get("source") != "compact":
        return 0
    current_path = _session_dir(event) / "current.md"
    try:
        summary = current_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return 0
    if not summary:
        return 0

    context = (
        "<long_task_continuity_checkpoint>\n"
        "This English checkpoint was written by this same thread immediately before "
        "the previous context window was replaced. Treat it as the authoritative "
        "continuation state. The rollover and any checkpoint instruction to call "
        "`new_context` have already been completed; do not call `new_context` again "
        "for that instruction. Continue with the first post-rollover task action and "
        "do not repeat completed work unless new evidence or verification requires "
        "it.\n\n"
        f"{summary}\n"
        "</long_task_continuity_checkpoint>"
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    return 0


def _handle_event(event: dict[str, Any]) -> int:
    """지원하는 훅 이벤트를 분기하고 나머지는 아무 작업도 하지 않는다."""
    observation_key = _observation_key(event)
    if observation_key is not None:
        _record_observation(event, observation_key)
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
