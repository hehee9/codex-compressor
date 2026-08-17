"""Codex CLI와 설정 파일의 관찰 가능한 호환성 정보를 수집합니다."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .configuration import (
    DEFAULT_BUFFER_TOKENS,
    DEFAULT_FALLBACK_PROMPT,
    ConfigurationError,
    inspect_token_budget,
    parse_config,
)


BASELINE_VERSION = (0, 145, 0)
_TOKEN_BUDGET_FIELDS = frozenset(
    {
        "enabled",
        "auto_compact_fallback_prompt",
        "auto_compact_fallback_buffer_tokens",
    }
)
_EXACT_21639_VERSIONS = frozenset(
    {
        (0, 129, 0, None),
        (0, 130, 0, None),
        (0, 131, 0, ("alpha", "4")),
        (0, 140, 0, ("alpha", "2")),
        (0, 146, 0, None),
        (0, 146, 0, ("alpha", "9", "2")),
    }
)


@dataclass(frozen=True)
class _CodexVersion:
    """숫자 버전과 사전 출시 식별자를 함께 보존합니다."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] | None = None

    def _key(self) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
        if self.prerelease is None:
            return (self.major, self.minor, self.patch, 1, ())
        identifiers: list[tuple[int, int | str]] = []
        for identifier in self.prerelease:
            if identifier.isdigit():
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
        return (self.major, self.minor, self.patch, 0, tuple(identifiers))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _CodexVersion):
            return NotImplemented
        return self._key() < other._key()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, _CodexVersion):
            return NotImplemented
        return self._key() <= other._key()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        suffix = ".".join(self.prerelease) if self.prerelease else ""
        return base if not suffix else f"{base}-{suffix}"

    def as_tuple(self) -> tuple[int, int, int, tuple[str, ...] | None]:
        """비교와 진단에 사용할 안정적인 버전 표현을 반환합니다."""

        return (self.major, self.minor, self.patch, self.prerelease)


def _parse_version(text: str) -> _CodexVersion | None:
    """버전 출력에서 숫자 버전과 alpha 등의 접미사를 찾습니다."""

    match = re.search(
        r"(?<![0-9])(?:v|version\s*)?(\d+)\.(\d+)\.(\d+)"
        r"(?:-([0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    prerelease = (
        tuple(re.split(r"[.-]", match.group(4))) if match.group(4) else None
    )
    return _CodexVersion(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        prerelease,
    )


def _version_tuple(text: str) -> _CodexVersion | None:
    """이전 내부 호출자와 호환되는 버전 파서를 제공합니다."""

    return _parse_version(text)


def _feature_names(output: str) -> frozenset[str]:
    """기능 목록에서 완전한 기능 이름 토큰만 추출합니다."""

    names = re.findall(
        r"(?<![A-Za-z0-9_-])(hooks|token_budget)(?![A-Za-z0-9_-])",
        output,
        re.IGNORECASE,
    )
    return frozenset(name.lower() for name in names)


def _run(
    command: str,
    args: list[str],
    timeout: float = 5.0,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """진단 명령을 실행하고 관찰 결과를 반환합니다."""

    try:
        options: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "check": False,
        }
        if env is not None:
            options["env"] = dict(env)
        completed = subprocess.run(
            [command, *args],
            **options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "output": str(exc), "returncode": None}
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return {
        "ok": completed.returncode == 0,
        "output": output,
        "returncode": completed.returncode,
    }


def _version_evidence(version: _CodexVersion | None) -> list[dict[str, str]]:
    """확인된 Codex 버전 범위에 해당하는 이슈 경고를 구성합니다."""

    if version is None:
        return []
    evidence: list[dict[str, str]] = []
    version_text = str(version)
    if version.as_tuple() == (0, 125, 0, None):
        evidence.append(
            {
                "issue": "#19780",
                "surface": "CLI",
                "version": version_text,
                "message": (
                    f"Codex CLI {version_text}는 대화형 세션에서 훅이 실행되지 않는 "
                    "#19780 사례와 일치합니다."
                ),
            }
        )
    if version.as_tuple() in _EXACT_21639_VERSIONS:
        evidence.append(
            {
                "issue": "#21639",
                "surface": "Desktop/App Server",
                "version": version_text,
                "message": (
                    f"Codex {version_text}는 Desktop/App Server 훅 회귀가 보고된 "
                    "#21639의 확인된 버전 표면입니다."
                ),
            }
        )
    if version.as_tuple() == (0, 130, 0, None):
        evidence.append(
            {
                "issue": "#24228",
                "surface": "CLI",
                "version": version_text,
                "message": (
                    f"Codex CLI {version_text}는 자동 복원 경로에서 SessionStart 훅이 "
                    "누락되는 #24228 사례와 일치합니다."
                ),
            }
        )
    lower_bound = _CodexVersion(0, 144, 2)
    upper_bound = _CodexVersion(0, 145, 0)
    if lower_bound <= version < upper_bound:
        evidence.append(
            {
                "issue": "#28736",
                "surface": "CLI",
                "version": version_text,
                "message": (
                    f"Codex CLI {version_text}는 compact SessionStart 훅 순서 문제가 "
                    "보고된 0.144.2 이상 0.145.0 미만 구간(#28736)입니다."
                ),
            }
        )
    return evidence


def _probe_config_text() -> str:
    """실제 CODEX_HOME과 분리된 기능 목록 probe용 최소 설정을 만듭니다."""

    return (
        "[features.token_budget]\n"
        "enabled = true\n"
        "auto_compact_fallback_prompt = "
        f"{json.dumps(DEFAULT_FALLBACK_PROMPT, ensure_ascii=False)}\n"
        f"auto_compact_fallback_buffer_tokens = {DEFAULT_BUFFER_TOKENS}\n"
    )


def _live_probe(command: str) -> dict[str, Any]:
    """임시 CODEX_HOME에서 설정을 읽지 않는 기능 목록 probe를 수행합니다."""

    with tempfile.TemporaryDirectory(prefix="codex-compressor-probe-") as temporary:
        probe_home = Path(temporary)
        config_path = probe_home / "config.toml"
        config_path.write_text(_probe_config_text(), encoding="utf-8")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(probe_home)
        environment.pop("LONG_TASK_CONTINUITY_HOME", None)
        result = _run(command, ["features", "list"], env=environment)
        names = sorted(_feature_names(result["output"]))
        result.update(
            {
                "feature_names": names,
                "hooks": "hooks" in names,
                "token_budget": "token_budget" in names,
                "config": inspect_token_budget(config_path.read_text(encoding="utf-8")),
            }
        )
        result["ok"] = bool(result["ok"] and result["hooks"] and result["token_budget"])
        return result


def _observation_summary(home: Path) -> dict[str, Any]:
    """세션별 observation 파일에서 훅과 롤오버의 실제 증거를 합칩니다."""

    continuity_dir = home / "continuity"
    sessions = 0
    hook_observed = False
    rollover_count = 0
    surfaces: set[str] = set()
    records: list[dict[str, Any]] = []
    if not continuity_dir.is_dir():
        return {
            "sessions": 0,
            "hook_observed": False,
            "rollovers_verified": 0,
            "surfaces": [],
            "records": records,
        }
    for observation_path in continuity_dir.glob("*/observations.json"):
        try:
            observation = _load_json(observation_path)
        except (OSError, ValueError):
            continue
        if not isinstance(observation, dict):
            continue
        sessions += 1
        events = observation.get("events")
        if isinstance(events, dict) and events:
            hook_observed = True
        value = observation.get("rollovers_verified", 0)
        if isinstance(value, int) and value > 0:
            rollover_count += value
        for surface in observation.get("surfaces", []):
            if surface in {"cli", "desktop_app_server"}:
                surfaces.add(surface)
        records.append(observation)
    return {
        "sessions": sessions,
        "hook_observed": hook_observed,
        "rollovers_verified": rollover_count,
        "surfaces": sorted(surfaces),
        "records": records,
    }


def _load_json(path: Path) -> Any:
    """관찰 파일을 읽고 JSON 형식 오류를 호출자에게 전달합니다."""

    return json.loads(path.read_text(encoding="utf-8"))


def inspect_compatibility(
    codex_home: str | Path,
    *,
    live_probe: bool = False,
    run_cli: bool = True,
) -> dict[str, Any]:
    """설정, CLI 기능, 실제 훅 관찰, 버전 근거를 분리해 반환합니다."""

    home = Path(codex_home).expanduser().resolve()
    config_path = home / "config.toml"
    warnings = [
        "Codex token_budget 기능은 현재 개발 중이므로 동작이 변경될 수 있습니다.",
    ]
    config_parse = "missing"
    token_budget: dict[str, Any] = {}
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        text = ""
        config_parse = "error"
        warnings.append(f"config.toml을 읽지 못했습니다: {exc}")
    else:
        try:
            parse_config(text)
            token_budget = inspect_token_budget(text)
            config_parse = "ok"
        except ConfigurationError as exc:
            config_parse = "error"
            warnings.append(str(exc))

    command = os.environ.get("CODEX_CLI_PATH", "codex")
    version = {"ok": False, "output": "관찰하지 않음", "returncode": None}
    features = {"ok": False, "output": "관찰하지 않음", "returncode": None}
    feature_names: frozenset[str] = frozenset()
    if run_cli:
        version = _run(command, ["--version"])
        features = _run(command, ["features", "list"])
        feature_names = _feature_names(features["output"])

    parsed_version = _parse_version(version["output"])
    evidence = _version_evidence(parsed_version)
    warnings.extend(item["message"] for item in evidence)
    if parsed_version and parsed_version < _CodexVersion(*BASELINE_VERSION):
        warnings.append(
            f"Codex CLI {parsed_version}는 기준 버전 0.145.0보다 낮습니다."
        )
    elif run_cli and not version["ok"]:
        warnings.append("codex --version을 관찰하지 못했습니다.")
    if run_cli and not features["ok"]:
        warnings.append("codex features list를 관찰하지 못했습니다.")
    if features["ok"] and "hooks" not in feature_names:
        warnings.append("CLI 기능 목록에서 정확한 hooks 기능을 확인하지 못했습니다.")
    if features["ok"] and "token_budget" not in feature_names:
        warnings.append(
            "CLI 기능 목록에서 정확한 token_budget 기능을 확인하지 못했습니다."
        )

    live_result: dict[str, Any] | None = None
    if live_probe:
        live_result = _live_probe(command)

    observations = _observation_summary(home)
    surfaces = set(observations["surfaces"])
    stages = {
        "configured": config_parse == "ok"
        and _TOKEN_BUDGET_FIELDS <= token_budget.keys(),
        "trust_approved": "unobserved",
        "cli_observed": True if "cli" in surfaces else "unobserved",
        "desktop_app_server_observed": (
            True if "desktop_app_server" in surfaces else "unobserved"
        ),
        "rollover_verified": (
            True if observations["rollovers_verified"] > 0 else "unobserved"
        ),
    }
    cli_report = {
        "version": version,
        "features": features,
        "feature_names": sorted(feature_names),
        "hooks": "hooks" in feature_names,
        "token_budget": "token_budget" in feature_names,
        "hook_observed": observations["hook_observed"],
    }
    result = {
        "config_parse": config_parse,
        "token_budget": token_budget,
        "stages": stages,
        "configured": stages["configured"],
        "trust_approved": stages["trust_approved"],
        "cli_observed": stages["cli_observed"],
        "desktop_app_server_observed": stages["desktop_app_server_observed"],
        "rollover_verified": stages["rollover_verified"],
        "hook_observed": observations["hook_observed"],
        "observations": observations,
        "cli": cli_report,
        "version_evidence": evidence,
        "codex_version": version["output"] if live_probe else None,
        "features_list": features["output"] if live_probe else None,
        "live_probe": live_result,
        "warnings": warnings,
    }
    return result
