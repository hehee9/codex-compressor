"""보존적 TOML 편집기의 집중 검증입니다."""

from __future__ import annotations

import unittest

from codex_compressor.configuration import (
    DEFAULT_FALLBACK_PROMPT,
    ConfigurationConflict,
    inspect_token_budget,
    prepare_token_budget,
    remove_token_budget,
)


class ConfigurationTests(unittest.TestCase):
    """필드 추가, 충돌, 줄바꿈 보존을 확인합니다."""

    def test_empty_config_adds_only_managed_table_and_removes_it(self) -> None:
        edit = prepare_token_budget("")
        self.assertEqual(inspect_token_budget(edit.text)["auto_compact_fallback_buffer_tokens"], 16384)
        restored = remove_token_budget(
            edit.text, {"installed": edit.values, "original": edit.original}
        )
        self.assertEqual(restored.text, "")

    def test_existing_comments_and_crlf_are_preserved(self) -> None:
        original = '# 사용자 주석\r\nmodel = "keep"\r\n\r\n[features]\r\nother = true\r\n'
        edit = prepare_token_budget(original)
        self.assertIn("# 사용자 주석\r\nmodel = \"keep\"\r\n", edit.text)
        self.assertNotIn("model_context_window", edit.text)
        self.assertNotIn("\n[features.token_budget]\n", edit.text)
        self.assertEqual(edit.text.replace("\r\n", "").count("\n"), 0)

    def test_conflict_requires_explicit_replacement(self) -> None:
        original = "[features.token_budget]\nenabled = false\n"
        with self.assertRaises(ConfigurationConflict):
            prepare_token_budget(original)
        edit = prepare_token_budget(original, replace=True)
        self.assertIn("enabled = true", edit.text)
        self.assertTrue(edit.original["enabled"]["present"])

    def test_prompt_is_exact_and_top_level_values_are_untouched(self) -> None:
        original = "model_context_window = 400000\nmodel_auto_compact_token_limit = 244800\n"
        edit = prepare_token_budget(original)
        self.assertEqual(inspect_token_budget(edit.text)["auto_compact_fallback_prompt"], DEFAULT_FALLBACK_PROMPT)
        self.assertIn("Keep the visible checkpoint below 9,000 UTF-8 bytes.", DEFAULT_FALLBACK_PROMPT)
        self.assertTrue(
            DEFAULT_FALLBACK_PROMPT.endswith(
                "After the summary is visible, call `new_context`. Do not use another tool first.\n"
            )
        )
        self.assertIn("model_context_window = 400000\n", edit.text)
        self.assertIn("model_auto_compact_token_limit = 244800\n", edit.text)

    def test_replaced_prompt_with_quotes_backslashes_and_controls_is_valid_toml(self) -> None:
        original = (
            "model = \"keep\"\n"
            "\n[features.token_budget]\n"
            "enabled = true\n"
            "auto_compact_fallback_prompt = \"old\" # keep this comment\n"
            "auto_compact_fallback_buffer_tokens = 16384\n"
            "\n[other]\nvalue = true\n"
        )
        prompt = 'quote """ slash \\ control \x00\nnext'
        edit = prepare_token_budget(
            original,
            replace=True,
            desired={
                "enabled": True,
                "auto_compact_fallback_prompt": prompt,
                "auto_compact_fallback_buffer_tokens": 16384,
            },
        )
        self.assertEqual(inspect_token_budget(edit.text)["auto_compact_fallback_prompt"], prompt)
        self.assertIn("model = \"keep\"\n", edit.text)
        self.assertIn("# keep this comment\n", edit.text)
        self.assertIn("\n[other]\nvalue = true\n", edit.text)
        restored = remove_token_budget(
            edit.text, {"installed": edit.values, "original": edit.original}
        )
        self.assertEqual(inspect_token_budget(restored.text)["auto_compact_fallback_prompt"], "old")
        self.assertIn("# keep this comment\n", restored.text)


if __name__ == "__main__":
    unittest.main()
