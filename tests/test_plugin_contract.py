from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "codex-compressor"
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"
MARKETPLACE_PATH = REPOSITORY_ROOT / ".codex-plugin" / "marketplace.json"
CANONICAL_MARKETPLACE_PATH = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"
VALIDATOR_PATH = Path(
    os.environ.get(
        "CODEX_PLUGIN_VALIDATOR",
        r"K:\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py",
    )
)


class PluginContractTests(unittest.TestCase):
    def test_manifest_uses_supported_shape_and_default_discovery(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "codex-compressor")
        project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], project["project"]["version"])
        self.assertEqual(manifest["license"], "Apache-2.0")
        self.assertEqual(manifest["author"]["name"], "hehee9")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertNotIn("hooks", manifest)
        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("apps", manifest)
        self.assertEqual(len(manifest["interface"]["defaultPrompt"]), 3)

    def test_package_cli_version_matches_project(self) -> None:
        project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "codex_compressor", "--version"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), project["project"]["version"])

    def test_marketplace_uses_local_plugin_contract(self) -> None:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        canonical = json.loads(CANONICAL_MARKETPLACE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(canonical, marketplace)
        entry = next(
            plugin
            for plugin in marketplace["plugins"]
            if plugin["name"] == "codex-compressor"
        )

        self.assertEqual(
            entry["source"],
            {"source": "local", "path": "./plugins/codex-compressor"},
        )
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Productivity")

    def test_hooks_are_default_discovered_and_use_plugin_root(self) -> None:
        hooks_path = PLUGIN_ROOT / "hooks" / "hooks.json"
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(hooks["hooks"]),
            {"PreToolUse", "PreCompact", "PostCompact", "SessionStart"},
        )
        command_hooks = [
            event["hooks"][0]
            for entries in hooks["hooks"].values()
            for event in entries
        ]

        self.assertTrue(all(hook["type"] == "command" for hook in command_hooks))
        self.assertTrue(all(hook["timeout"] == 10 for hook in command_hooks))
        self.assertTrue(
            all("${PLUGIN_ROOT}" in hook["command"] for hook in command_hooks)
        )
        self.assertTrue(
            all("${PLUGIN_ROOT}" in hook["commandWindows"] for hook in command_hooks)
        )
        windows_commands = [hook["commandWindows"] for hook in command_hooks]
        self.assertTrue(all(command.startswith("py -3 ") for command in windows_commands))
        self.assertTrue(all("py -3.11" not in command for command in windows_commands))
        self.assertTrue((PLUGIN_ROOT / "hooks" / "launch.py").is_file())
        self.assertTrue((PLUGIN_ROOT / "hooks" / "launch.sh").is_file())
        self.assertTrue((PLUGIN_ROOT / "hooks" / "launch.cmd").is_file())

        launcher = (PLUGIN_ROOT / "hooks" / "launch.py").read_text(encoding="utf-8")
        self.assertIn("sys.version_info < (3, 11)", launcher)
        self.assertIn("Python 3.11", launcher)

    def test_skills_use_bundled_runtime(self) -> None:
        for skill_path in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
            contents = skill_path.read_text(encoding="utf-8")
            self.assertIn('${PLUGIN_ROOT}/runtime"', contents, skill_path)
            self.assertNotIn("python -m codex_compressor", contents, skill_path)

    def test_runtime_is_synced_from_source_core(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "tools" / "sync_plugin_runtime.py"),
                "--check",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(VALIDATOR_PATH.is_file(), "공식 플러그인 검증기가 없는 환경")
    def test_official_plugin_validator(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(PLUGIN_ROOT)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()


