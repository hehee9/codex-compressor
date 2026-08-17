"""GitHub 스타 요청의 조건과 명령 실행을 검증합니다."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import codex_compressor.cli as cli
import codex_compressor.github_star as github_star
from codex_compressor.github_star import _maybe_prompt_for_star


class _TTYBuffer(io.StringIO):
    """테스트에서 대화형 표준 스트림을 표현합니다."""

    def isatty(self) -> bool:
        return True


class GithubStarTests(unittest.TestCase):
    """스타 요청의 일회성·억제·실패 동작을 확인합니다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()
        self.stdin = _TTYBuffer()
        self.stdout = _TTYBuffer()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_accept_runs_star_api_and_persists_marker_once(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run(command: tuple[str, ...]) -> int:
            commands.append(command)
            return 0

        prompts: list[str] = []
        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=lambda prompt: prompts.append(prompt) or "yes",
        )

        marker = self.home / "codex-compressor" / ".star-prompted"
        self.assertEqual(
            commands,
            [
                ("gh", "auth", "status", "--hostname", "github.com"),
                (
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "-X",
                    "PUT",
                    "/user/starred/hehee9/codex-compressor",
                ),
            ],
        )
        self.assertEqual(
            prompts,
            ["GitHub에서 Codex Compressor 저장소에 스타를 추가할까요? [Y/n]"],
        )
        self.assertTrue(marker.exists())

        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=Mock(side_effect=AssertionError("두 번째 제안")),
        )
        self.assertEqual(len(commands), 2)

    def test_decline_and_api_failure_still_settle_marker(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run(command: tuple[str, ...]) -> int:
            commands.append(command)
            return 1 if command[1] == "api" else 0

        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=lambda _prompt: "n",
        )
        marker = self.home / "codex-compressor" / ".star-prompted"
        self.assertTrue(marker.exists())
        self.assertEqual(
            commands,
            [("gh", "auth", "status", "--hostname", "github.com")],
        )

        marker.unlink()
        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=lambda _prompt: "y",
        )
        self.assertTrue(marker.exists())
        self.assertEqual(len(commands), 3)
        self.assertEqual(
            self.stdout.getvalue(),
            "https://github.com/hehee9/codex-compressor\n",
        )

    def test_empty_input_accepts(self) -> None:
        commands: list[tuple[str, ...]] = []

        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=lambda command: commands.append(command) or 0,
            input_reader=lambda _prompt: "",
        )

        self.assertEqual(commands[1][0:5], ("gh", "api", "--hostname", "github.com", "-X"))
        self.assertTrue((self.home / "codex-compressor" / ".star-prompted").exists())

    def test_invalid_input_repeats_until_settled(self) -> None:
        prompts: list[str] = []
        answers = iter(("maybe", "no"))

        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=lambda _command: 0,
            input_reader=lambda prompt: prompts.append(prompt) or next(answers),
        )

        self.assertEqual(prompts, [github_star._PROMPT, github_star._PROMPT])
        self.assertTrue((self.home / "codex-compressor" / ".star-prompted").exists())

    def test_guards_and_interrupted_input_do_not_prompt(self) -> None:
        run = Mock(return_value=0)
        reader = Mock(return_value="yes")

        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={"CI": "1"},
            run_command=run,
            input_reader=reader,
        )
        self.assertFalse(reader.called)
        self.assertFalse(run.called)

        run.return_value = 1
        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=reader,
        )
        self.assertFalse(reader.called)
        self.assertEqual(self.stdout.getvalue(), "")

        with patch.object(github_star.subprocess, "run", side_effect=FileNotFoundError):
            _maybe_prompt_for_star(
                self.home,
                stdin=self.stdin,
                stdout=self.stdout,
                env={},
                input_reader=reader,
            )
        self.assertFalse(reader.called)
        self.assertEqual(self.stdout.getvalue(), "")

        run.return_value = 0
        non_tty = io.StringIO()
        _maybe_prompt_for_star(
            self.home,
            stdin=non_tty,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=reader,
        )
        self.assertFalse(reader.called)

        _maybe_prompt_for_star(
            self.home,
            stdin=self.stdin,
            stdout=self.stdout,
            env={},
            run_command=run,
            input_reader=Mock(side_effect=KeyboardInterrupt),
        )
        self.assertFalse((self.home / "codex-compressor" / ".star-prompted").exists())

    def test_gh_timeout_is_treated_as_command_failure(self) -> None:
        with patch.object(
            github_star.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("gh", 10),
        ) as run:
            self.assertEqual(github_star._run_command(("gh", "auth", "status")), 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_cli_prompts_only_for_interactive_success_paths(self) -> None:
        manager = Mock()
        manager.codex_home = self.home
        manager.install.return_value = {"ok": True}
        manager.doctor.return_value = {"installed": True, "configured": True}
        with patch.object(cli, "Manager", return_value=manager), patch.object(
            cli, "_maybe_prompt_for_star"
        ) as prompt:
            self.assertEqual(cli.main(["install", "--mode", "plugin"]), 0)
            prompt.assert_called_once_with(self.home, stdin=cli.sys.stdin, stdout=cli.sys.stdout)

            prompt.reset_mock()
            self.assertEqual(cli.main(["doctor", "--json"]), 0)
            prompt.assert_not_called()

            self.assertEqual(cli.main(["doctor"]), 0)
            prompt.assert_called_once_with(self.home, stdin=cli.sys.stdin, stdout=cli.sys.stdout)


if __name__ == "__main__":
    unittest.main()
