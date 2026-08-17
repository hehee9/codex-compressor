"""호환성 경고, 격리 probe, 실제 훅 관찰의 집중 검증입니다."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_compressor import compatibility, continuity
from codex_compressor.configuration import prepare_token_budget


class CompatibilityTests(unittest.TestCase):
    """버전·기능·관찰 단계가 서로 섞이지 않는지 확인합니다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()

    def _configured_home(self) -> None:
        self.home.joinpath("config.toml").write_text(
            prepare_token_budget("").text,
            encoding="utf-8",
        )

    def _run_stub(self, version: str, features: str = "hooks\ntoken_budget\n"):
        def run(command: list[str], **kwargs: object):
            output = version if command[-1] == "--version" else features
            return type(
                "Completed",
                (),
                {"stdout": output, "stderr": "", "returncode": 0},
            )()

        return run

    def test_token_budget_warning_is_always_present(self) -> None:
        result = compatibility.inspect_compatibility(self.home, run_cli=False)
        self.assertTrue(any("token_budget" in warning for warning in result["warnings"]))

    def test_feature_matching_requires_exact_names(self) -> None:
        self._configured_home()
        features = "my_hooks\nhooking\ntoken_budget_extra\nhooks_extra\n"
        with patch.object(
            compatibility.subprocess,
            "run",
            side_effect=self._run_stub("codex-cli 0.147.0", features),
        ):
            result = compatibility.inspect_compatibility(self.home)
        self.assertFalse(result["cli"]["hooks"])
        self.assertFalse(result["cli"]["token_budget"])
        self.assertTrue(any("정확한 hooks" in warning for warning in result["warnings"]))
        self.assertTrue(any("정확한 token_budget" in warning for warning in result["warnings"]))

    def test_evidence_warnings_respect_boundaries_and_alpha_suffixes(self) -> None:
        cases = {
            "0.125.0": {"#19780"},
            "0.124.9": set(),
            "0.131.0-alpha.4": {"#21639"},
            "0.131.0-alpha.5": set(),
            "0.131.0": set(),
            "0.144.2-alpha.1": set(),
            "0.144.2": {"#28736"},
            "0.145.0": set(),
            "0.146.0-alpha.9.2": {"#21639"},
            "0.146.0-alpha.9.3": set(),
        }
        for version, expected in cases.items():
            with self.subTest(version=version):
                with patch.object(
                    compatibility.subprocess,
                    "run",
                    side_effect=self._run_stub(f"codex-cli {version}"),
                ):
                    result = compatibility.inspect_compatibility(self.home)
                actual = {item["issue"] for item in result["version_evidence"]}
                self.assertEqual(actual, expected)
                self.assertNotIn("#20862", " ".join(result["warnings"]))

    def test_live_probe_uses_isolated_home_and_only_features_command(self) -> None:
        captured: dict[str, object] = {}

        def run(command: list[str], **kwargs: object):
            captured["command"] = command
            environment = kwargs["env"]
            self.assertIsInstance(environment, dict)
            probe_home = Path(environment["CODEX_HOME"])
            captured["probe_home"] = probe_home
            captured["config"] = (probe_home / "config.toml").read_text(encoding="utf-8")
            return type(
                "Completed",
                (),
                {"stdout": "hooks\ntoken_budget\n", "stderr": "", "returncode": 0},
            )()

        with patch.object(compatibility.subprocess, "run", side_effect=run):
            result = compatibility.inspect_compatibility(
                self.home,
                live_probe=True,
                run_cli=False,
            )
        self.assertEqual(captured["command"][-2:], ["features", "list"])
        config = str(captured["config"])
        self.assertTrue(config.endswith("\n"))
        self.assertIn("enabled = true", config)
        self.assertIn("auto_compact_fallback_prompt", config)
        self.assertIn("auto_compact_fallback_buffer_tokens", config)
        self.assertNotIn("continuity", config)
        self.assertTrue(result["live_probe"]["ok"])
        self.assertFalse((self.home / "continuity").exists())
        self.assertFalse(Path(captured["probe_home"]).exists())

    def test_unknown_surface_keeps_surface_stages_unobserved(self) -> None:
        original_home = continuity.os.environ.get("LONG_TASK_CONTINUITY_HOME")
        continuity.os.environ["LONG_TASK_CONTINUITY_HOME"] = self.temp.name
        self.addCleanup(self._restore_continuity_home, original_home)
        session_dir = self.home / "continuity" / "session"
        session_dir.mkdir(parents=True)
        for event in (
            {"hook_event_name": "PreCompact", "trigger": "auto", "session_id": "session"},
            {"hook_event_name": "PostCompact", "trigger": "auto", "session_id": "session"},
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": "session"},
        ):
            continuity._record_observation(event, continuity._observation_key(event))
        result = compatibility.inspect_compatibility(self.temp.name, run_cli=False)
        self.assertTrue(result["hook_observed"])
        self.assertTrue(result["rollover_verified"])
        self.assertEqual(result["cli_observed"], "unobserved")
        self.assertEqual(result["desktop_app_server_observed"], "unobserved")

    @staticmethod
    def _restore_continuity_home(previous: str | None) -> None:
        if previous is None:
            continuity.os.environ.pop("LONG_TASK_CONTINUITY_HOME", None)
        else:
            continuity.os.environ["LONG_TASK_CONTINUITY_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
