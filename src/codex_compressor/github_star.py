"""Codex Compressor 저장소 스타 추가를 한 번만 제안합니다."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO


_PROMPT = "GitHub에서 Codex Compressor 저장소에 스타를 추가할까요? [Y/n]"
_REPOSITORY_URL = "https://github.com/hehee9/codex-compressor"
_MARKER_NAME = ".star-prompted"
_COMMAND_TIMEOUT_SECONDS = 10
_AGENT_ENV_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CODEX_THREAD_ID",
    "CODEX_SHELL",
    "CODEX_CI",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "CURSOR_TRACE_ID",
    "CURSOR_SESSION_TOKEN",
    "CURSOR_AGENT",
    "AIDER_CHAT",
    "OPENCODE_BIN_PATH",
    "GEMINI_CLI",
    "REPL_ID",
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
    "CODESPACES",
)
_CommandRunner = Callable[[Sequence[str]], int]
_InputReader = Callable[[str], str]


def _run_command(command: Sequence[str]) -> int:
    """gh 명령을 조용히 실행하고 종료 코드를 반환합니다."""

    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return completed.returncode


def _is_agent_driven(env: Mapping[str, str]) -> bool:
    """opencodex와 같은 자동 실행 환경인지 확인합니다."""

    return any((env.get(name) or "").strip() for name in _AGENT_ENV_VARS)


def _write_marker(marker: Path) -> None:
    """스타 요청 응답을 다음 실행에도 유지합니다."""

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        # 제안 기능의 저장 실패가 설치·진단 결과를 바꾸면 안 됩니다.
        return


def _maybe_prompt_for_star(
    codex_home: Path,
    *,
    stdin: TextIO,
    stdout: TextIO,
    env: Mapping[str, str] | None = None,
    run_command: _CommandRunner = _run_command,
    input_reader: _InputReader | None = None,
) -> None:
    """조건을 만족하는 대화형 실행에서 스타 추가를 한 번 제안합니다."""

    marker = codex_home / "codex-compressor" / _MARKER_NAME
    if marker.exists() or not stdin.isatty() or not stdout.isatty():
        return
    if _is_agent_driven(os.environ if env is None else env):
        return
    if run_command(("gh", "auth", "status", "--hostname", "github.com")) != 0:
        return

    while True:
        try:
            answer = (input if input_reader is None else input_reader)(_PROMPT)
        except (EOFError, KeyboardInterrupt):
            return

        answer = answer.strip().lower()
        if answer in {"", "y", "yes"}:
            api_status = run_command(
                (
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "-X",
                    "PUT",
                    "/user/starred/hehee9/codex-compressor",
                )
            )
            if api_status != 0:
                print(_REPOSITORY_URL, file=stdout)
            break
        if answer in {"n", "no"}:
            break
    _write_marker(marker)
