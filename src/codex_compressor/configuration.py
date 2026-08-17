"""Codex 설정 파일을 보존적으로 편집하는 기능을 제공합니다."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from typing import Any


DEFAULT_BUFFER_TOKENS = 16_384
LEGACY_BUFFER_TOKENS = 155_200
REQUEST_MARKER = "LONG_TASK_CONTINUITY_SUMMARY_REQUEST_V1"
LEGACY_SCRIPT_SHA256 = (
    "826FEA8CB15E9AF34C39924A40155C26297B26346E22B46FD7E996161934802B"
)

# 현재 K:\.codex\config.toml의 프롬프트를 그대로 보존합니다.
DEFAULT_FALLBACK_PROMPT = (
    "[LONG_TASK_CONTINUITY_SUMMARY_REQUEST_V1]\n"
    "The current context window has reached its rollover point. Before doing any more task work, write one visible assistant message in English that preserves the operational continuation state, then call `new_context`. Do not start another model, fork a thread, or copy the transcript into another request. The format below is recommended, not mandatory. Include enough detail to continue reliably, but omit exact commands and raw tool output by default.\n"
    "Keep the visible checkpoint below 9,000 UTF-8 bytes.\n"
    "\n"
    "<summary>\n"
    "## Current objective and active constraints\n"
    "## Full plan checklist\n"
    "- Completed\n"
    "- In progress\n"
    "- Needs review\n"
    "- Not started\n"
    "## Summary of previous context windows\n"
    "Condense cumulative decisions, findings, and state from earlier windows. Include this from the second rollover onward; do not copy the previous summary verbatim.\n"
    "## Progress in the current context window\n"
    "Cover completed work and affected files or scope; review status; work in progress; failed attempts and ruled-out approaches; relevant sources and paths; unresolved review findings; the exact work underway; user messages and responses in this window; and exact next actions.\n"
    "</summary>\n"
    "\n"
    "After the summary is visible, call `new_context`. Do not use another tool first.\n"
)

_FIELDS = (
    "enabled",
    "auto_compact_fallback_prompt",
    "auto_compact_fallback_buffer_tokens",
)
_SECTION_HEADER = "[features.token_budget]"
_SECTION_RE = re.compile(
    r"(?m)^\[features\.token_budget\][ \t]*(?:#.*)?(?:\r?\n|$)"
)
_NEXT_TABLE_RE = re.compile(r"^\[[^\r\n]+\][ \t]*(?:#.*)?(?:\r?\n|$)")


class ConfigurationError(ValueError):
    """설정이 TOML 계약을 만족하지 않을 때 발생합니다."""


class ConfigurationConflict(ConfigurationError):
    """사용자가 수정한 관리 필드와 충돌할 때 발생합니다."""

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__("관리 필드 충돌: " + ", ".join(conflicts))


@dataclass(frozen=True)
class TokenBudgetEdit:
    """토큰 예산 편집 결과와 소유권에 필요한 원래 값을 담습니다."""

    text: str
    changed: bool
    section_created: bool
    values: dict[str, Any]
    original: dict[str, dict[str, Any]]
    conflicts: tuple[str, ...] = ()


def sha256_bytes(data: bytes) -> str:
    """바이트의 SHA-256 해시를 대문자로 반환합니다."""

    return hashlib.sha256(data).hexdigest().upper()


def parse_config(text: str) -> dict[str, Any]:
    """문자열을 엄격하게 TOML로 해석합니다."""

    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise ConfigurationError(f"config.toml TOML 해석 실패: {exc}") from exc


def _line_ending(text: str) -> str:
    """기존 파일의 줄바꿈을 선택합니다."""

    return "\r\n" if "\r\n" in text else "\n"


def _token_budget_value(parsed: dict[str, Any], key: str) -> tuple[bool, Any]:
    """파싱된 TOML에서 관리 필드의 존재와 값을 읽습니다."""

    features = parsed.get("features")
    if features is None:
        return False, None
    if not isinstance(features, dict):
        raise ConfigurationError("features가 테이블이 아닙니다")
    table = features.get("token_budget")
    if table is None:
        return False, None
    if not isinstance(table, dict):
        raise ConfigurationError("features.token_budget이 테이블이 아닙니다")
    return key in table, table.get(key)


def inspect_token_budget(text: str) -> dict[str, Any]:
    """토큰 예산 관리 필드의 현재 값을 반환합니다."""

    parsed = parse_config(text)
    result: dict[str, Any] = {}
    for key in _FIELDS:
        present, value = _token_budget_value(parsed, key)
        if present:
            result[key] = value
    return result


def _section_bounds(text: str) -> tuple[int, int] | None:
    """관리 테이블의 본문 범위를 찾습니다."""

    match = _SECTION_RE.search(text)
    if match is None:
        return None
    end = len(text)
    # 멀티라인 문자열 안의 [LONG_TASK...]는 TOML 테이블 헤더가
    # 아니므로, 문자열 경계를 추적한 뒤 다음 헤더만 찾습니다.
    in_triple = False
    offset = match.end()
    for line in text[offset:].splitlines(keepends=True):
        if not in_triple and _NEXT_TABLE_RE.match(line):
            end = offset
            break
        triple_count = line.count('"""') + line.count("'''")
        if triple_count % 2:
            in_triple = not in_triple
        offset += len(line)
    return match.end(), end


def _render_value(key: str, value: Any, newline: str) -> str:
    """관리 필드 한 줄 또는 멀티라인 값을 렌더링합니다."""

    if key == "enabled":
        return f"enabled = {'true' if value else 'false'}{newline}"
    if key == "auto_compact_fallback_buffer_tokens":
        return f"auto_compact_fallback_buffer_tokens = {int(value)}{newline}"
    if key == "auto_compact_fallback_prompt":
        return f"auto_compact_fallback_prompt = {_render_toml_string(str(value))}{newline}"
    raise KeyError(key)


def _render_toml_string(value: str) -> str:
    """임의의 문자열을 TOML 기본 문자열로 안전하게 렌더링합니다."""

    # JSON의 큰따옴표 문자열 이스케이프는 TOML 기본 문자열에서도
    # 유효하며, 역슬래시·제어 문자·큰따옴표를 값으로부터 분리합니다.
    return json.dumps(value, ensure_ascii=False)


def _field_match(text: str, key: str, start: int, end: int) -> re.Match[str] | None:
    """관리 테이블에서 한 필드의 시작 줄을 찾습니다."""

    return re.search(
        rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[ \t]*",
        text[start:end],
    )


def _triple_string_end(text: str, delimiter: str, start: int, end: int) -> int:
    """이스케이프된 따옴표를 건너뛰고 멀티라인 문자열 끝을 찾습니다."""

    offset = start
    while True:
        close = text.find(delimiter, offset, end)
        if close < 0:
            return -1
        backslashes = 0
        cursor = close - 1
        while cursor >= start and text[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return close
        offset = close + len(delimiter)


def _replace_field(text: str, key: str, value: Any) -> str:
    """관리 테이블의 값을 서식 손실 없이 교체합니다."""

    bounds = _section_bounds(text)
    if bounds is None:
        raise ConfigurationError(f"관리 테이블이 없어 {key}를 교체할 수 없습니다")
    start, end = bounds
    section = text[start:end]
    match = _field_match(text, key, start, end)
    if match is None:
        raise ConfigurationError(f"관리 필드가 없어 {key}를 교체할 수 없습니다")
    absolute_start = start + match.start()
    value_start = start + match.end()
    newline = _line_ending(text)

    if key == "auto_compact_fallback_prompt":
        delimiter_match = re.match(r"\"\"\"|'''", text[value_start:])
        if delimiter_match:
            delimiter = delimiter_match.group(0)
            close = _triple_string_end(
                text, delimiter, value_start + len(delimiter), end
            )
            if close < 0 or close >= end:
                raise ConfigurationError("멀티라인 프롬프트의 끝을 찾을 수 없습니다")
            after = close + len(delimiter)
            line_end = text.find("\n", after)
            if line_end < 0 or line_end > end:
                line_end = end
            line_break = "\r" if line_end > after and text[line_end - 1] == "\r" else ""
            content_end = line_end - len(line_break)
            replacement = (
                text[absolute_start:value_start]
                + _render_toml_string(str(value))
                + text[after:content_end]
                + line_break
            )
            return text[:absolute_start] + replacement + text[line_end:]
        # 기존 값이 한 줄 기본 문자열이어도 동일한 멀티라인 형식으로
        # 교체하여 영어 프롬프트의 줄바꿈을 안전하게 보존합니다.
        line_end = text.find("\n", value_start)
        if line_end < 0 or line_end > end:
            line_end = end
        line_break = "\r" if line_end > value_start and text[line_end - 1] == "\r" else ""
        content_end = line_end - len(line_break)
        suffix_match = re.search(r"([ \t]*(?:#.*)?)$", text[value_start:content_end])
        suffix = suffix_match.group(1) if suffix_match else ""
        replacement = (
            text[absolute_start:value_start]
            + _render_toml_string(str(value))
            + suffix
            + line_break
        )
        return text[:absolute_start] + replacement + text[line_end:]

    line_end = text.find("\n", value_start)
    if line_end < 0 or line_end > end:
        line_end = end
    line_break = "\r" if line_end > value_start and text[line_end - 1] == "\r" else ""
    content_end = line_end - len(line_break)
    prefix = text[absolute_start:value_start]
    suffix_match = re.search(r"([ \t]*(?:#.*)?)$", text[value_start:content_end])
    suffix = suffix_match.group(1) if suffix_match else ""
    scalar = (
        ("true" if value else "false")
        if key == "enabled"
        else str(int(value))
        if key == "auto_compact_fallback_buffer_tokens"
        else str(value)
    )
    replacement = prefix + scalar + suffix + line_break
    return text[:absolute_start] + replacement + text[line_end:]


def _append_fields(text: str, missing: list[str], values: dict[str, Any]) -> str:
    """기존 관리 테이블 또는 파일 끝에 없는 필드를 추가합니다."""

    newline = _line_ending(text)
    rendered = "".join(_render_value(key, values[key], newline) for key in missing)
    bounds = _section_bounds(text)
    if bounds is None:
        separator = "" if not text else ("" if text.endswith(("\n", "\r")) else newline)
        if text and text.endswith(("\n", "\r")):
            separator = newline
        return text + separator + _SECTION_HEADER + newline + rendered
    _, end = bounds
    prefix = text[:end]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    return prefix + rendered + text[end:]


def _validate_types(values: dict[str, Any]) -> None:
    """관리 필드의 최종 타입을 검사합니다."""

    if not isinstance(values.get("enabled"), bool):
        raise ConfigurationError("features.token_budget.enabled는 불리언이어야 합니다")
    if not isinstance(values.get("auto_compact_fallback_prompt"), str):
        raise ConfigurationError("auto_compact_fallback_prompt는 문자열이어야 합니다")
    if not isinstance(values.get("auto_compact_fallback_buffer_tokens"), int):
        raise ConfigurationError(
            "auto_compact_fallback_buffer_tokens는 정수여야 합니다"
        )


def prepare_token_budget(
    text: str,
    *,
    replace: bool = False,
    desired: dict[str, Any] | None = None,
) -> TokenBudgetEdit:
    """토큰 예산 필드를 추가하거나 명시적으로 교체할 편집을 준비합니다."""

    parsed = parse_config(text)
    desired_values = {
        "enabled": True,
        "auto_compact_fallback_prompt": DEFAULT_FALLBACK_PROMPT,
        "auto_compact_fallback_buffer_tokens": DEFAULT_BUFFER_TOKENS,
    }
    if desired:
        desired_values.update(desired)
    _validate_types(desired_values)

    current = inspect_token_budget(text)
    original: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    missing: list[str] = []
    for key in _FIELDS:
        if key not in current:
            missing.append(key)
            original[key] = {"present": False}
        else:
            original[key] = {"present": True, "value": current[key]}
            if current[key] != desired_values[key]:
                conflicts.append(key)
    if conflicts and not replace:
        raise ConfigurationConflict(conflicts)

    result = text
    for key in _FIELDS:
        if key in current and current[key] != desired_values[key]:
            result = _replace_field(result, key, desired_values[key])
    if missing:
        result = _append_fields(result, missing, desired_values)
    final = inspect_token_budget(result)
    for key in _FIELDS:
        if final.get(key) != desired_values[key]:
            raise ConfigurationError(f"{key}의 최종 값 검증 실패")
    parse_config(result)
    return TokenBudgetEdit(
        text=result,
        changed=result != text,
        section_created=_section_bounds(text) is None,
        values=desired_values,
        original=original,
        conflicts=tuple(conflicts),
    )


def remove_token_budget(text: str, ownership: dict[str, Any]) -> TokenBudgetEdit:
    """소유한 필드만 현재 설치값과 일치할 때 제거하거나 원래 값으로 복구합니다."""

    parse_config(text)
    installed = ownership.get("installed", {})
    original = ownership.get("original", {})
    current = inspect_token_budget(text)
    conflicts: list[str] = []
    result = text

    for key in _FIELDS:
        if key not in installed or key not in current:
            continue
        if current[key] != installed[key]:
            conflicts.append(key)
            continue
        previous = original.get(key, {})
        if previous.get("present"):
            result = _replace_field(result, key, previous.get("value"))
            continue
        bounds = _section_bounds(result)
        if bounds is None:
            continue
        start, end = bounds
        match = _field_match(result, key, start, end)
        if match is None:
            continue
        absolute_start = start + match.start()
        line_end = result.find("\n", absolute_start)
        if line_end < 0 or line_end > end:
            line_end = end
        else:
            line_end += 1
        if key == "auto_compact_fallback_prompt":
            value_start = start + match.end()
            delimiter_match = re.match(r"\"\"\"|'''", result[value_start:])
            if delimiter_match:
                delimiter = delimiter_match.group(0)
                close = _triple_string_end(
                    result, delimiter, value_start + len(delimiter), end
                )
                if close < 0 or close >= end:
                    raise ConfigurationError("프롬프트 제거 범위를 찾을 수 없습니다")
                line_end = result.find("\n", close)
                line_end = end if line_end < 0 or line_end > end else line_end + 1
        result = result[:absolute_start] + result[line_end:]

    bounds = _section_bounds(result)
    if bounds is not None:
        start, end = bounds
        body = result[start:end]
        if not any(
            line.strip() and not line.lstrip().startswith("#")
            for line in body.splitlines()
        ):
            header_start = result.rfind(_SECTION_HEADER, 0, start)
            if header_start >= 0:
                header_line_start = result.rfind("\n", 0, header_start) + 1
                result = result[:header_line_start] + result[end:]

    parse_config(result)
    return TokenBudgetEdit(
        text=result,
        changed=result != text,
        section_created=False,
        values=installed,
        original=original,
        conflicts=tuple(conflicts),
    )


def legacy_script_matches(script_bytes: bytes) -> bool:
    """알려진 장기 작업 스크립트인지 확인합니다."""

    return (
        REQUEST_MARKER.encode("utf-8") in script_bytes
        and sha256_bytes(script_bytes) == LEGACY_SCRIPT_SHA256
    )
