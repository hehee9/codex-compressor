import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path


from codex_compressor import continuity


class ContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.previous_home = os.environ.get("LONG_TASK_CONTINUITY_HOME")
        os.environ["LONG_TASK_CONTINUITY_HOME"] = self.temp_dir.name
        self.addCleanup(self._restore_home)

    def _restore_home(self) -> None:
        if self.previous_home is None:
            os.environ.pop("LONG_TASK_CONTINUITY_HOME", None)
        else:
            os.environ["LONG_TASK_CONTINUITY_HOME"] = self.previous_home

    def _transcript(self, *records: dict) -> str:
        path = Path(self.temp_dir.name) / "transcript.jsonl"
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return str(path)

    def _event(self, name: str, transcript_path: str | None, **extra: object) -> dict:
        event = {
            "hook_event_name": name,
            "session_id": "session/one",
            "turn_id": "turn-1",
            "transcript_path": transcript_path,
        }
        event.update(extra)
        return event

    def _run(self, event: dict) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = continuity._handle_event(event)
        return result, output.getvalue(), errors.getvalue()

    def _summary_transcript(self, summary: str = "## Current Objective\nKeep going") -> str:
        return self._transcript(
            {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": continuity.REQUEST_MARKER}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": summary}],
                },
            },
        )

    def test_pre_and_post_compact_store_and_promote_checkpoint(self) -> None:
        transcript = self._summary_transcript()
        event = self._event("PreCompact", transcript, trigger="auto")
        result, output, errors = self._run(event)
        self.assertEqual(result, 0)
        self.assertEqual(output, "")
        self.assertEqual(errors, "")

        session_dir = Path(self.temp_dir.name) / "continuity" / "session_one"
        self.assertTrue((session_dir / "pending.md").is_file())
        post = self._event("PostCompact", None, trigger="auto")
        self.assertEqual(self._run(post), (0, "", ""))
        self.assertFalse((session_dir / "pending.md").exists())
        self.assertEqual((session_dir / "current.md").read_text(encoding="utf-8").strip(), "## Current Objective\nKeep going")
        state = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["window_count"], 1)

    def test_valid_pending_checkpoint_does_not_reread_transcript(self) -> None:
        session_dir = Path(self.temp_dir.name) / "continuity" / "session_one"
        session_dir.mkdir(parents=True)
        (session_dir / "pending.md").write_text("existing checkpoint\n", encoding="utf-8")
        event = self._event("PreCompact", None, trigger="manual")
        self.assertEqual(self._run(event), (0, "", ""))

    def test_missing_transcript_is_a_compaction_failure_and_logged(self) -> None:
        event = self._event("PreCompact", None, trigger="auto")
        result, output, errors = self._run(event)
        self.assertEqual(result, 0)
        response = json.loads(output)
        self.assertFalse(response["continue"])
        self.assertIn("transcript_path is required", response["stopReason"])
        self.assertIn("transcript_path is required", errors)
        self.assertIn("transcript_path is required", (Path(self.temp_dir.name) / "continuity" / ".last-error.log").read_text(encoding="utf-8"))

    def test_oversized_checkpoint_is_rejected(self) -> None:
        transcript = self._summary_transcript("x" * (continuity.MAX_SUMMARY_CHARS + 1))
        event = self._event("PreCompact", transcript, trigger="manual")
        result, output, _ = self._run(event)
        self.assertEqual(result, 0)
        response = json.loads(output)
        self.assertFalse(response["continue"])
        self.assertIn(f"{continuity.MAX_SUMMARY_CHARS:,}", response["stopReason"])

    def test_malformed_and_unsupported_transcripts_fail_diagnostically(self) -> None:
        cases = [
            ("{not json}\n", "malformed JSON"),
            (json.dumps({"type": "session_meta", "payload": {}}) + "\n", "no supported"),
        ]
        for content, expected in cases:
            with self.subTest(expected=expected):
                transcript = Path(self.temp_dir.name) / "bad.jsonl"
                transcript.write_text(content, encoding="utf-8")
                event = self._event("PreCompact", str(transcript), trigger="auto")
                result, output, errors = self._run(event)
                self.assertEqual(result, 0)
                self.assertIn(expected, json.loads(output)["stopReason"])
                self.assertIn(expected, errors)

    def test_new_context_denies_without_a_checkpoint(self) -> None:
        transcript = self._transcript(
            {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": continuity.REQUEST_MARKER}],
                },
            },
        )
        event = self._event("PreToolUse", transcript, tool_name="new_context")
        result, output, _ = self._run(event)
        self.assertEqual(result, 0)
        response = json.loads(output)
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(continuity.RETRY_MARKER, response["hookSpecificOutput"]["permissionDecisionReason"])

    def test_session_start_reinjects_current_checkpoint(self) -> None:
        session_dir = Path(self.temp_dir.name) / "continuity" / "session_one"
        session_dir.mkdir(parents=True)
        (session_dir / "current.md").write_text("Continue from here", encoding="utf-8")
        event = self._event("SessionStart", None, source="compact")
        result, output, errors = self._run(event)
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<long_task_continuity_checkpoint>", context)
        self.assertIn("Continue from here", context)
        self.assertIn("do not call `new_context` again", context)

    def test_multiple_rollovers_increment_window_count(self) -> None:
        session_dir = Path(self.temp_dir.name) / "continuity" / "session_one"
        for index, trigger in enumerate(("auto", "manual"), start=1):
            transcript = self._summary_transcript(f"## Current Objective\nrollover {index}")
            event = self._event("PreCompact", transcript, trigger=trigger)
            self.assertEqual(self._run(event)[0], 0)
            post = self._event("PostCompact", None, trigger=trigger, turn_id=f"turn-{index}")
            self.assertEqual(self._run(post), (0, "", ""))
            self.assertEqual(
                self._run(self._event("SessionStart", None, source="compact"))[0],
                0,
            )
        state = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["window_count"], 2)
        self.assertEqual((session_dir / "current.md").read_text(encoding="utf-8").strip(), "## Current Objective\nrollover 2")
        observations = json.loads((session_dir / "observations.json").read_text(encoding="utf-8"))
        self.assertEqual(observations["rollovers_verified"], 2)
        self.assertEqual(observations["events"]["PreCompact(auto)"]["count"], 1)
        self.assertEqual(observations["events"]["PreCompact(manual)"]["count"], 1)

    def test_supported_hook_events_write_atomic_observations(self) -> None:
        transcript = self._summary_transcript()
        self.assertEqual(
            self._run(self._event("PreToolUse", transcript, tool_name="new_context"))[0],
            0,
        )
        self.assertEqual(
            self._run(self._event("PreCompact", transcript, trigger="auto"))[0],
            0,
        )
        self.assertEqual(
            self._run(self._event("PostCompact", None, trigger="auto"))[0],
            0,
        )
        self.assertEqual(
            self._run(self._event("SessionStart", None, source="compact"))[0],
            0,
        )
        self._run({"hook_event_name": "Notification", "session_id": "session/one"})

        observation_path = (
            Path(self.temp_dir.name)
            / "continuity"
            / "session_one"
            / "observations.json"
        )
        observations = json.loads(observation_path.read_text(encoding="utf-8"))
        self.assertEqual(observations["version"], continuity.OBSERVATION_VERSION)
        self.assertEqual(observations["events"]["PreToolUse(new_context)"]["count"], 1)
        self.assertEqual(observations["events"]["PreCompact(auto)"]["count"], 1)
        self.assertEqual(observations["events"]["PostCompact(auto)"]["count"], 1)
        self.assertEqual(observations["events"]["SessionStart(compact)"]["count"], 1)
        self.assertNotIn("Notification", observations["events"])
        for event in observations["events"].values():
            self.assertIn("first_seen_at", event)
            self.assertIn("last_seen_at", event)

    def test_observation_surface_uses_session_metadata(self) -> None:
        for source, expected in (
            ("cli", "cli"),
            ("vscode", "desktop_app_server"),
        ):
            with self.subTest(source=source):
                transcript = self._transcript(
                    {
                        "type": "session_meta",
                        "payload": {"source": source},
                    }
                )
                event = self._event(
                    "PreCompact",
                    transcript,
                    trigger="manual",
                    session_id=f"surface-{source}",
                )
                self._run(event)
                observation_path = (
                    Path(self.temp_dir.name)
                    / "continuity"
                    / f"surface-{source}"
                    / "observations.json"
                )
                observations = json.loads(
                    observation_path.read_text(encoding="utf-8")
                )
                self.assertEqual(observations["surfaces"], [expected])

    def test_unrelated_events_and_triggers_are_no_ops(self) -> None:
        self.assertEqual(self._run({"hook_event_name": "Notification"}), (0, "", ""))
        event = self._event("PreCompact", None, trigger="other")
        self.assertEqual(self._run(event), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
