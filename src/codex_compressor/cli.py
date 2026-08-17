"""Codex Compressor 명령줄 인터페이스입니다."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .github_star import _maybe_prompt_for_star
from .manager import Manager, ManagerError, VERSION


def _parser() -> argparse.ArgumentParser:
    """지원 명령과 옵션을 구성합니다."""

    parser = argparse.ArgumentParser(prog="codex-compressor")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install", help="설치하거나 모드를 전환합니다")
    install.add_argument("--mode", choices=("standalone", "plugin"), required=True)
    install.add_argument("--codex-home", type=str)
    install.add_argument("--replace-token-budget", action="store_true")

    status = commands.add_parser("status", help="설치 상태를 확인합니다")
    status.add_argument("--codex-home", type=str)
    status.add_argument("--json", action="store_true", dest="as_json")

    doctor = commands.add_parser("doctor", help="설정과 CLI를 진단합니다")
    doctor.add_argument("--codex-home", type=str)
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--live-probe", action="store_true")

    uninstall = commands.add_parser("uninstall", help="설치를 제거합니다")
    uninstall.add_argument("--codex-home", type=str)
    uninstall.add_argument("--purge-state", action="store_true")

    restore = commands.add_parser("restore", help="백업을 복원합니다")
    restore.add_argument("--codex-home", type=str)
    restore.add_argument("--backup", required=True)
    restore.add_argument("--force", action="store_true")
    return parser


def _print_result(result: dict[str, object], as_json: bool) -> None:
    """결과를 JSON 또는 짧은 사람이 읽는 형식으로 출력합니다."""

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key, value in result.items():
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    """명령줄을 실행하고 종료 코드를 반환합니다."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            manager = Manager(codex_home=args.codex_home)
            result = manager.install(
                args.mode,
                replace_token_budget=args.replace_token_budget,
            )
            _print_result(result, False)
            if result.get("ok") is True:
                _maybe_prompt_for_star(manager.codex_home, stdin=sys.stdin, stdout=sys.stdout)
            return 0
        manager = Manager(codex_home=args.codex_home)
        if args.command == "status":
            _print_result(manager.status(), args.as_json)
            return 0
        if args.command == "doctor":
            result = manager.doctor(live_probe=args.live_probe)
            _print_result(result, args.as_json)
            if (
                not args.as_json
                and result.get("installed") is True
                and result.get("configured") is True
            ):
                _maybe_prompt_for_star(manager.codex_home, stdin=sys.stdin, stdout=sys.stdout)
            return 0
        if args.command == "uninstall":
            _print_result(manager.uninstall(purge_state=args.purge_state), False)
            return 0
        if args.command == "restore":
            _print_result(manager.restore(args.backup, force=args.force), False)
            return 0
    except (ManagerError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    parser.error("알 수 없는 명령입니다")
    return 2
