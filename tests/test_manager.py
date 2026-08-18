"""설치 관리자 수명주기의 집중 검증입니다."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_compressor.manager as manager_module
from codex_compressor.configuration import (
    DEFAULT_FALLBACK_PROMPT,
    inspect_token_budget,
    prepare_token_budget,
)
from codex_compressor.manager import Manager, ManagerError


class ManagerTests(unittest.TestCase):
    """설치, 모드 전환, 복구, 사용자 충돌을 확인합니다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()
        self.manager = Manager(codex_home=self.home, source_root=Path(__file__).parents[1])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_standalone_preserves_other_hooks_and_is_idempotent(self) -> None:
        hooks = {
            "description": "keep",
            "hooks": {
                "PreToolUse": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "echo keep"}]}
                ]
            },
        }
        (self.home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
        first = self.manager.install("standalone")
        before = (self.home / "hooks.json").read_bytes()
        second = self.manager.install("standalone")
        self.assertIsNone(second["backup_id"])
        self.assertEqual(before, (self.home / "hooks.json").read_bytes())
        current = json.loads(before)
        self.assertEqual(current["description"], "keep")
        self.assertEqual(len(current["hooks"]), 4)
        self.assertTrue(first["changed"]["config"])
        installed_hook = next(
            item
            for entries in current["hooks"].values()
            for item in entries
            if self.manager._hook_is_compressor(item)
        )
        hook = installed_hook["hooks"][0]
        self.assertEqual(hook["timeout"], 10)
        self.assertNotIn("py -3.11", hook["commandWindows"])
        self.assertIn(str(Path(sys.executable).resolve()), hook["command"])
        self.assertIn(str((self.manager.runtime_root / "codex_compressor" / "continuity.py").resolve()), hook["command"])

    def test_clean_install_reinstall_and_uninstall_preserves_ownership(self) -> None:
        first = self.manager.install("standalone")
        first_state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        second = self.manager.install("standalone")
        second_state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(second["backup_id"])
        self.assertEqual(second_state["token_budget"], first_state["token_budget"])
        result = self.manager.uninstall()
        self.assertTrue(result["ok"])
        self.assertFalse(self.home.joinpath("config.toml").exists())
        self.assertFalse(self.home.joinpath("hooks.json").exists())
        self.assertFalse(self.manager.state_path.exists())

    def test_reinstall_upgrades_unchanged_previous_managed_defaults(self) -> None:
        self.manager.install("standalone")
        first_state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        old_prompt = DEFAULT_FALLBACK_PROMPT.replace(
            "Keep the visible checkpoint below 9,000 UTF-8 bytes.\n", ""
        )
        config = self.home / "config.toml"
        config.write_text(
            prepare_token_budget(
                config.read_text(encoding="utf-8"),
                replace=True,
                desired={
                    "enabled": True,
                    "auto_compact_fallback_prompt": old_prompt,
                    "auto_compact_fallback_buffer_tokens": 16_384,
                },
            ).text,
            encoding="utf-8",
        )
        state = first_state
        state["token_budget"]["installed"]["auto_compact_fallback_prompt"] = old_prompt
        self.manager.state_path.write_text(json.dumps(state), encoding="utf-8")

        result = self.manager.install("standalone")

        self.assertTrue(result["changed"]["config"])
        self.assertEqual(
            inspect_token_budget(config.read_text(encoding="utf-8"))[
                "auto_compact_fallback_prompt"
            ],
            DEFAULT_FALLBACK_PROMPT,
        )
        upgraded_state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            upgraded_state["token_budget"]["original"],
            first_state["token_budget"]["original"],
        )
        self.assertEqual(
            upgraded_state["token_budget"]["legacy_migrated"],
            first_state["token_budget"]["legacy_migrated"],
        )

    def test_reinstall_upgrades_legacy_prompt_and_preserves_buffer_ownership(self) -> None:
        self.manager.install("standalone")
        first_state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        old_prompt = DEFAULT_FALLBACK_PROMPT.replace(
            "Keep the visible checkpoint below 9,000 UTF-8 bytes.\n", ""
        )
        config = self.home / "config.toml"
        config.write_text(
            prepare_token_budget(
                config.read_text(encoding="utf-8"),
                replace=True,
                desired={
                    "enabled": True,
                    "auto_compact_fallback_prompt": old_prompt,
                    "auto_compact_fallback_buffer_tokens": 155200,
                },
            ).text,
            encoding="utf-8",
        )
        state = first_state
        state["token_budget"]["installed"] = {
            "enabled": True,
            "auto_compact_fallback_prompt": old_prompt,
            "auto_compact_fallback_buffer_tokens": 155200,
        }
        state["token_budget"]["legacy_migrated"] = True
        self.manager.state_path.write_text(json.dumps(state), encoding="utf-8")

        self.manager.install("standalone")

        installed = inspect_token_budget(config.read_text(encoding="utf-8"))
        self.assertEqual(installed["auto_compact_fallback_prompt"], DEFAULT_FALLBACK_PROMPT)
        self.assertEqual(installed["auto_compact_fallback_buffer_tokens"], 155200)
        upgraded_state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        self.assertTrue(upgraded_state["token_budget"]["legacy_migrated"])
        self.assertEqual(
            upgraded_state["token_budget"]["original"],
            first_state["token_budget"]["original"],
        )

    def test_reinstall_protects_user_modified_prompt_until_explicit_replacement(self) -> None:
        self.manager.install("standalone")
        config = self.home / "config.toml"
        user_prompt = "사용자가 수정한 프롬프트"
        config.write_text(
            prepare_token_budget(
                config.read_text(encoding="utf-8"),
                replace=True,
                desired={
                    "enabled": True,
                    "auto_compact_fallback_prompt": user_prompt,
                    "auto_compact_fallback_buffer_tokens": 16_384,
                },
            ).text,
            encoding="utf-8",
        )

        with self.assertRaises(ManagerError):
            self.manager.install("standalone")
        self.assertEqual(
            inspect_token_budget(config.read_text(encoding="utf-8"))[
                "auto_compact_fallback_prompt"
            ],
            user_prompt,
        )

        self.manager.install("standalone", replace_token_budget=True)
        self.assertEqual(
            inspect_token_budget(config.read_text(encoding="utf-8"))[
                "auto_compact_fallback_prompt"
            ],
            DEFAULT_FALLBACK_PROMPT,
        )

    def test_uninstall_retains_star_marker_unless_purged(self) -> None:
        self.manager.install("standalone")
        marker = self.manager.runtime_root / ".star-prompted"
        marker.touch()

        self.assertTrue(self.manager.uninstall()["ok"])
        self.assertTrue(marker.exists())

        self.manager.install("standalone")
        self.assertTrue(self.manager.uninstall(purge_state=True)["ok"])
        self.assertFalse(marker.exists())

        marker.touch()
        self.assertTrue(self.manager.uninstall(purge_state=True)["ok"])
        self.assertFalse(marker.exists())

    def test_legacy_approval_without_fingerprint_requires_reapproval(self) -> None:
        self.manager.install("standalone")
        state = self.manager._state()
        state["trust_approved"] = True
        state.pop("trust_approved_fingerprint", None)
        self.manager._write_state(state)

        self.manager.install("standalone")

        migrated = self.manager._state()
        self.assertIs(migrated["trust_approved"], False)
        self.assertIsNone(migrated["trust_approved_fingerprint"])

    def test_matching_hook_fingerprint_reuses_approval(self) -> None:
        self.manager.install("standalone")
        config = self.home / "config.toml"
        text = config.read_text(encoding="utf-8")
        for index, label in enumerate(
            ("pre_tool_use", "pre_compact", "post_compact", "session_start"),
            start=1,
        ):
            key = f"{self.manager.hooks_path}:{label}:0:0"
            text += (
                f"\n[hooks.state.'{key}']\n"
                f'trusted_hash = "sha256:{index:064x}"\n'
            )
        config.write_text(text, encoding="utf-8")
        state = self.manager._state()
        self.assertIs(self.manager._trust_approval_status(state), True)

        self.manager.install("standalone")
        reinstalled = self.manager._state()
        self.assertIs(reinstalled["trust_approved"], True)
        self.assertEqual(
            reinstalled["trust_approved_fingerprint"],
            self.manager._hook_fingerprint(reinstalled["hooks"]),
        )
        self.assertIs(self.manager._trust_approval_status(reinstalled), True)

        result = self.manager.install("standalone")
        self.assertIsNone(result["backup_id"])
        self.assertIs(self.manager._state()["trust_approved"], True)

    def test_changed_hook_declaration_invalidates_approval(self) -> None:
        old_entry = self.manager._hook_entry()
        old_entry["timeout"] = 5
        with patch.object(self.manager, "_hook_entry", return_value=old_entry):
            self.manager.install("standalone")
            config = self.home / "config.toml"
            text = config.read_text(encoding="utf-8")
            for index, label in enumerate(
                ("pre_tool_use", "pre_compact", "post_compact", "session_start"),
                start=1,
            ):
                key = f"{self.manager.hooks_path}:{label}:0:0"
                text += (
                    f"\n[hooks.state.'{key}']\n"
                    f'trusted_hash = "sha256:{index:064x}"\n'
                )
            config.write_text(text, encoding="utf-8")
            self.manager.install("standalone")
            approved = self.manager._state()
            self.assertIs(approved["trust_approved"], True)

        result = self.manager.install("standalone")

        self.assertTrue(result["changed"]["hooks"])
        invalidated = self.manager._state()
        self.assertIs(invalidated["trust_approved"], False)
        self.assertIsNone(invalidated["trust_approved_fingerprint"])

    def test_untouched_restore_succeeds_and_later_edit_is_refused(self) -> None:
        backup_id = self.manager.install("standalone")["backup_id"]
        self.assertTrue(self.manager.restore(backup_id)["ok"])

        self.manager.install("standalone")
        second_backup = self.manager._state()["last_backup_id"]
        config = self.home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        with self.assertRaises(ManagerError):
            self.manager.restore(second_backup)

    def test_uninstall_reports_user_edited_conflict(self) -> None:
        self.manager.install("standalone")
        config = self.home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace("enabled = true", "enabled = false"), encoding="utf-8")
        result = self.manager.uninstall()
        self.assertFalse(result["ok"])
        self.assertTrue(any("enabled" in item for item in result["conflicts"]))
        self.assertTrue(config.exists())

    def test_restore_refuses_changed_file_and_force_restores(self) -> None:
        first_backup = self.manager.install("standalone")["backup_id"]
        self.assertIsNotNone(first_backup)
        config = self.home / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        with self.assertRaises(ManagerError):
            self.manager.restore(first_backup)
        result = self.manager.restore(first_backup, force=True)
        self.assertTrue(result["ok"])

    def test_switching_modes_removes_standalone_hooks(self) -> None:
        self.manager.install("standalone")
        self.manager.install("plugin")
        hooks = json.loads((self.home / "hooks.json").read_text(encoding="utf-8"))
        self.assertFalse(any(self.manager._hook_is_compressor(item) for entries in hooks["hooks"].values() for item in entries))
        self.assertEqual(self.manager._state()["mode"], "plugin")

    def test_config_hook_write_failure_rolls_back_both_targets(self) -> None:
        original_write = manager_module._atomic_write

        def fail_config(path: Path, data: bytes) -> None:
            if Path(path) == self.manager.config_path:
                raise OSError("의도적 검사 실패")
            original_write(path, data)

        manager_module._atomic_write = fail_config
        try:
            with self.assertRaises(OSError):
                self.manager.install("standalone")
        finally:
            manager_module._atomic_write = original_write
        self.assertFalse((self.home / "config.toml").exists())
        self.assertFalse((self.home / "hooks.json").exists())

    def test_runtime_copy_and_state_write_failures_roll_back_everything(self) -> None:
        original_write = manager_module._atomic_write

        def fail_runtime(path: Path, data: bytes) -> None:
            if Path(path).name == "manager.py" and "codex_compressor" in str(path):
                raise OSError("의도적 런타임 복사 실패")
            original_write(path, data)

        manager_module._atomic_write = fail_runtime
        try:
            with self.assertRaises(OSError):
                self.manager.install("standalone")
        finally:
            manager_module._atomic_write = original_write
        self.assertFalse((self.home / "config.toml").exists())
        self.assertFalse((self.home / "hooks.json").exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse((self.manager.runtime_root / "codex_compressor" / "manager.py").exists())

        def fail_state(path: Path, data: bytes) -> None:
            if Path(path) == self.manager.state_path:
                raise OSError("의도적 상태 저장 실패")
            original_write(path, data)

        manager_module._atomic_write = fail_state
        try:
            with self.assertRaises(OSError):
                self.manager.install("standalone")
        finally:
            manager_module._atomic_write = original_write
        self.assertFalse((self.home / "config.toml").exists())
        self.assertFalse((self.home / "hooks.json").exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse((self.manager.runtime_root / "codex_compressor" / "manager.py").exists())

    def test_runtime_conflict_keeps_runtime_and_state_for_retry(self) -> None:
        self.manager.install("standalone")
        runtime_file = self.manager.runtime_root / "codex_compressor" / "manager.py"
        runtime_file.write_bytes(b"user edit")
        result = self.manager.uninstall()
        self.assertFalse(result["ok"])
        self.assertTrue(self.manager.state_path.exists())
        self.assertTrue(runtime_file.exists())
        self.assertTrue((self.home / "config.toml").exists())

    def test_legacy_155200_survives_mode_switch_and_uninstall(self) -> None:
        legacy_script = Path(r"K:\.codex\hooks\long-task-continuity.py")
        if not legacy_script.exists():
            self.skipTest("기존 레거시 스크립트가 없는 환경")
        hooks_dir = self.home / "hooks"
        hooks_dir.mkdir()
        shutil.copy2(legacy_script, hooks_dir / "long-task-continuity.py")
        legacy_hook = {
            "type": "command",
            "command": "python3 K:/.codex/hooks/long-task-continuity.py",
            "commandWindows": "py -3.11 K:\\.codex\\hooks\\long-task-continuity.py",
            "timeout": 5,
        }
        (self.home / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [{"matcher": ".*", "hooks": [legacy_hook]}]
                        for event in ("PreToolUse", "PreCompact", "PostCompact", "SessionStart")
                    }
                }
            ),
            encoding="utf-8",
        )
        from codex_compressor.configuration import DEFAULT_FALLBACK_PROMPT, inspect_token_budget

        (self.home / "config.toml").write_text(
            "[features.token_budget]\n"
            "enabled = true\n"
            "auto_compact_fallback_prompt = \"\"\"\n"
            f"{DEFAULT_FALLBACK_PROMPT}\"\"\"\n"
            "auto_compact_fallback_buffer_tokens = 155200\n",
            encoding="utf-8",
        )
        self.assertTrue(self.manager.install("standalone")["legacy_migrated"])
        self.assertTrue(self.manager.install("plugin")["legacy_migrated"])
        self.assertEqual(
            inspect_token_budget((self.home / "config.toml").read_text(encoding="utf-8"))[
                "auto_compact_fallback_buffer_tokens"
            ],
            155200,
        )
        self.assertTrue(self.manager.uninstall()["ok"])
        self.assertFalse(self.manager.state_path.exists())

    def test_cli_custom_home_is_supported_for_management_commands(self) -> None:
        from codex_compressor.cli import _parser

        for command in ("status", "doctor", "uninstall", "restore"):
            argv = [command, "--codex-home", str(self.home)]
            if command == "restore":
                argv.extend(["--backup", "example"])
            args = _parser().parse_args(argv)
            self.assertEqual(args.codex_home, str(self.home))

    def test_known_legacy_installation_keeps_155200_and_top_level_values(self) -> None:
        legacy_script = Path(r"K:\.codex\hooks\long-task-continuity.py")
        if not legacy_script.exists():
            self.skipTest("기존 레거시 스크립트가 없는 환경")
        hooks_dir = self.home / "hooks"
        hooks_dir.mkdir()
        shutil.copy2(legacy_script, hooks_dir / "long-task-continuity.py")
        legacy_hook = {
            "type": "command",
            "command": "python3 K:/.codex/hooks/long-task-continuity.py",
            "commandWindows": "py -3.11 K:\\.codex\\hooks\\long-task-continuity.py",
            "timeout": 5,
        }
        hooks = {
            "hooks": {
                event: [{"matcher": ".*", "hooks": [legacy_hook]}]
                for event in ("PreToolUse", "PreCompact", "PostCompact", "SessionStart")
            }
        }
        (self.home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
        from codex_compressor.configuration import DEFAULT_FALLBACK_PROMPT

        config = (
            "model_context_window = 400000\n"
            "model_auto_compact_token_limit = 244800\n"
            "\n[features.token_budget]\n"
            "enabled = true\n"
            "auto_compact_fallback_prompt = \"\"\"\n"
            f"{DEFAULT_FALLBACK_PROMPT}\"\"\"\n"
            "auto_compact_fallback_buffer_tokens = 155200\n"
        )
        (self.home / "config.toml").write_text(config, encoding="utf-8")
        result = self.manager.install("standalone")
        self.assertTrue(result["legacy_migrated"])
        self.assertIn("auto_compact_fallback_buffer_tokens = 155200", (self.home / "config.toml").read_text(encoding="utf-8"))
        self.assertIn("model_context_window = 400000", (self.home / "config.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
