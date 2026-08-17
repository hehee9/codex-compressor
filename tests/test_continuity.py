import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def _transcript(self, *records: dict, newline: bytes = b"\n") -> str:
        path = Path(self.temp_dir.name) / "transcript.jsonl"
        with path.open("wb") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                stream.write(newline)
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

    def _summary_records(
        self,
        summary: str,
        turn_id: str = "turn-1",
        *,
        direct_metadata: bool = False,
        phase: str = "final_answer",
    ) -> tuple[dict, dict, dict]:
        context = {"type": "turn_context", "payload": {"turn_id": turn_id}}
        marker = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": continuity.REQUEST_MARKER}],
            },
        }
        assistant = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": phase,
                "content": [{"type": "output_text", "text": summary}],
            },
        }
        if direct_metadata:
            marker["payload"]["internal_chat_message_metadata_passthrough"] = {
                "turn_id": turn_id
            }
            assistant["payload"]["internal_chat_message_metadata_passthrough"] = {
                "turn_id": turn_id
            }
        return context, marker, assistant

    def _session_dir(self, session_id: str = "session_one") -> Path:
        return Path(self.temp_dir.name) / "continuity" / session_id

    def _state(self) -> dict:
        return json.loads((self._session_dir() / "state.json").read_text(encoding="utf-8"))

    def _rollover(self, summary: str, turn_id: str = "turn-1") -> str:
        transcript = self._transcript(*self._summary_records(summary, turn_id))
        pre = self._event("PreCompact", transcript, trigger="auto", turn_id=turn_id)
        self.assertEqual(self._run(pre), (0, "", ""))
        post = self._event("PostCompact", None, trigger="auto", turn_id=turn_id)
        self.assertEqual(self._run(post), (0, "", ""))
        return transcript

    def test_reverse_tail_ignores_malformed_prefix_and_preserves_literal_u2028(self) -> None:
        path = Path(self.temp_dir.name) / "prefix.jsonl"
        summary = "## Current Objective\nkeep\u2028going"
        context, marker, assistant = self._summary_records(summary)
        path.write_bytes(
            b"{malformed stale prefix}\n"
            + json.dumps(context, ensure_ascii=False).encode()
            + b"\n"
            + json.dumps(marker, ensure_ascii=False).encode()
            + b"\n"
            + json.dumps(assistant, ensure_ascii=False).encode()
            + b"\n"
        )
        event = self._event("PreCompact", str(path), trigger="auto")
        self.assertEqual(continuity._find_summary_candidate(event), summary)

    def test_reverse_tail_uses_bounded_reads_for_sparse_117gb_equivalent_prefix(self) -> None:
        path = Path(self.temp_dir.name) / "sparse.jsonl"
        context, marker, assistant = self._summary_records("bounded")
        with path.open("wb") as stream:
            stream.seek(1_170_000_000)
            stream.write(b"\n")
            for record in (context, marker, assistant):
                stream.write(json.dumps(record).encode() + b"\n")
        event = self._event("PreCompact", str(path), trigger="auto")
        with mock.patch.object(continuity, "TRANSCRIPT_CHUNK_BYTES", 64 * 1024):
            self.assertEqual(continuity._find_summary_candidate(event), "bounded")

    def test_reverse_tail_handles_crlf_and_utf8_chunk_boundary(self) -> None:
        path = Path(self.temp_dir.name) / "boundary.jsonl"
        context, marker, assistant = self._summary_records("한글 요약")
        filler = {
            "type": "session_meta",
            "payload": {"note": "가" * 30_000},
        }
        with path.open("wb") as stream:
            for record in (filler, context, marker, assistant):
                stream.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                stream.write(b"\r\n")
        event = self._event("PreCompact", str(path), trigger="auto")
        self.assertEqual(continuity._find_summary_candidate(event), "한글 요약")

    def test_incomplete_trailing_line_is_retried(self) -> None:
        path = Path(self.temp_dir.name) / "incomplete.jsonl"
        context, marker, assistant = self._summary_records("eventually complete")
        path.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in (context, marker))
            + json.dumps(assistant).encode()
        )

        def finish_line(_delay: float) -> None:
            with path.open("ab") as stream:
                stream.write(b"\n")

        event = self._event("PreCompact", str(path), trigger="auto")
        with mock.patch.object(continuity.time, "sleep", side_effect=finish_line):
            self.assertEqual(
                continuity._find_summary_candidate(event, wait_seconds=1.0),
                "eventually complete",
            )

    def test_partial_newer_final_beats_complete_draft_after_retry(self) -> None:
        path = Path(self.temp_dir.name) / "partial-final.jsonl"
        context, marker, draft = self._summary_records("complete draft", phase="commentary")
        final = self._summary_records("new final answer", phase="final_answer")[2]
        path.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in (context, marker, draft))
            + json.dumps(final).encode()
        )

        def finish_line(_delay: float) -> None:
            with path.open("ab") as stream:
                stream.write(b"\n")

        event = self._event("PreCompact", str(path), trigger="auto")
        with mock.patch.object(continuity.time, "sleep", side_effect=finish_line):
            self.assertEqual(
                continuity._find_summary_candidate(event, wait_seconds=1.0),
                "new final answer",
            )

    def test_marker_without_metadata_requires_matching_in_range_turn_boundary(self) -> None:
        _context, marker, assistant = self._summary_records("unverified")
        path = self._transcript(marker, assistant)
        event = self._event("PreCompact", path, trigger="auto")
        with self.assertRaisesRegex(
            continuity._TranscriptError, "matching turn boundary"
        ):
            continuity._find_summary_candidate(event)

    def test_relevant_malformed_tail_fails_diagnostically(self) -> None:
        context, marker, _assistant = self._summary_records("unused")
        path = Path(self.temp_dir.name) / "relevant-malformed.jsonl"
        path.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in (context, marker))
            + b"{malformed relevant record}\n"
        )
        event = self._event("PreCompact", str(path), trigger="auto")
        with self.assertRaises(continuity._TranscriptError):
            continuity._find_summary_candidate(event)

    def test_final_answer_is_preferred_after_latest_marker(self) -> None:
        context, marker, _ = self._summary_records("draft", phase="commentary")
        final = self._summary_records("final", phase="final_answer")[2]
        path = self._transcript(context, marker, {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"text": "another draft"}],
            },
        }, final)
        event = self._event("PreCompact", path, trigger="auto")
        self.assertEqual(continuity._find_summary_candidate(event), "final")

    def test_clean_first_rollover_establishes_four_hash_chain(self) -> None:
        self._rollover("first checkpoint")
        state = self._state()
        active = state["active_rollover"]
        self.assertEqual(state["version"], 2)
        self.assertEqual(active["phase"], "committed")
        self.assertEqual(active["generated_sha256"], active["pending_sha256"])
        self.assertEqual(active["pending_sha256"], active["current_sha256"])
        start = self._event("SessionStart", None, source="compact")
        start.pop("turn_id")
        result, output, errors = self._run(start)
        self.assertEqual((result, errors), (0, ""))
        self.assertIn("first checkpoint", json.loads(output)["hookSpecificOutput"]["additionalContext"])
        active = self._state()["active_rollover"]
        self.assertEqual(active["phase"], "injected")
        hashes = {
            active["generated_sha256"],
            active["pending_sha256"],
            active["current_sha256"],
            active["injected_sha256"],
        }
        self.assertEqual(len(hashes), 1)
        observations = json.loads((self._session_dir() / "observations.json").read_text())
        self.assertEqual(observations["last_verified_turn_id"], active["turn_id"])
        self.assertEqual(observations["rollovers_verified"], 1)

    def test_multiple_exact_rollovers_do_not_reuse_previous_turn(self) -> None:
        self._rollover("checkpoint one", "turn-1")
        first_start = self._run(self._event("SessionStart", None, source="compact"))
        self.assertIn("checkpoint one", first_start[1])
        transcript = self._transcript(*self._summary_records("checkpoint two", "turn-2"))
        self.assertEqual(
            self._run(self._event("PreCompact", transcript, trigger="manual", turn_id="turn-2")),
            (0, "", ""),
        )
        self.assertEqual(
            self._run(self._event("PostCompact", None, trigger="manual", turn_id="turn-2")),
            (0, "", ""),
        )
        result, output, _errors = self._run(
            self._event("SessionStart", None, source="compact", turn_id="turn-2")
        )
        self.assertEqual(result, 0)
        self.assertIn("checkpoint two", output)
        self.assertEqual(self._state()["window_count"], 2)

    def test_stale_pending_wrong_turn_is_not_reused(self) -> None:
        transcript = self._transcript(*self._summary_records("turn one", "turn-1"))
        self.assertEqual(
            self._run(self._event("PreCompact", transcript, trigger="auto")),
            (0, "", ""),
        )
        result, output, errors = self._run(
            self._event("PreCompact", None, trigger="auto", turn_id="turn-2")
        )
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output)["continue"])
        self.assertIn("transcript_path is required", errors)

    def test_failed_new_compact_invalidates_older_committed_checkpoint(self) -> None:
        self._rollover("older committed", "turn-1")
        result, output, errors = self._run(
            self._event("PreCompact", None, trigger="manual", turn_id="turn-2")
        )
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output)["continue"])
        self.assertIn("transcript_path is required", errors)
        state = self._state()
        self.assertNotIn("active_rollover", state)
        self.assertEqual(state["window_count"], 1)

        start = self._event("SessionStart", None, source="compact")
        start.pop("turn_id")
        result, output, errors = self._run(start)
        self.assertEqual(result, 0)
        self.assertIn("verification failed", output)
        self.assertIn("committed v2", errors)

    def test_tampered_pending_and_current_remain_ineligible(self) -> None:
        self._rollover("trusted", "turn-1")
        session_dir = self._session_dir()
        # 다음 pending을 만든 뒤 tamper하면 PostCompact가 current를 갱신하지 않는다.
        transcript = self._transcript(*self._summary_records("new", "turn-2"))
        self.assertEqual(
            self._run(self._event("PreCompact", transcript, trigger="auto", turn_id="turn-2")),
            (0, "", ""),
        )
        (session_dir / "pending.md").write_text("tampered", encoding="utf-8")
        self.assertEqual(
            self._run(self._event("PostCompact", None, trigger="auto", turn_id="turn-2"))[0],
            0,
        )
        self.assertEqual(self._state()["window_count"], 1)
        self.assertEqual((session_dir / "current.md").read_text(encoding="utf-8").strip(), "trusted")
        result, output, _errors = self._run(
            self._event("SessionStart", None, source="compact", turn_id="turn-2")
        )
        self.assertEqual(result, 0)
        self.assertIn("verification failed", output)

        # committed current를 바꿔도 stale 값은 주입하지 않는다.
        self._rollover("fresh", "turn-3")
        (session_dir / "current.md").write_text("tampered current", encoding="utf-8")
        result, output, _errors = self._run(
            self._event("SessionStart", None, source="compact", turn_id="turn-3")
        )
        self.assertEqual(result, 0)
        self.assertIn("verification failed", output)

    def test_duplicate_session_start_is_empty_and_does_not_increment(self) -> None:
        self._rollover("once")
        start = self._event("SessionStart", None, source="compact")
        self.assertEqual(self._run(start)[1].count("long_task_continuity_checkpoint"), 2)
        before = json.loads((self._session_dir() / "observations.json").read_text())
        self.assertEqual(self._run(start), (0, "", ""))
        after = json.loads((self._session_dir() / "observations.json").read_text())
        self.assertEqual(after["rollovers_verified"], before["rollovers_verified"])

    def test_v1_observation_sequence_counts_reset_in_v2(self) -> None:
        session_dir = self._session_dir()
        session_dir.mkdir(parents=True)
        (session_dir / "observations.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "rollover_phase": 2,
                    "rollovers_verified": 99,
                    "events": {},
                }
            ),
            encoding="utf-8",
        )
        self._run(self._event("PreCompact", None, trigger="other"))
        continuity._record_observation(self._event("PreCompact", None, trigger="auto"), "PreCompact(auto)")
        observations = json.loads((session_dir / "observations.json").read_text())
        self.assertEqual(observations["version"], 2)
        self.assertEqual(observations["rollovers_verified"], 0)
        self.assertNotIn("rollover_phase", observations)

    def test_v1_state_cannot_authorize_session_start_injection(self) -> None:
        session_dir = self._session_dir()
        session_dir.mkdir(parents=True)
        (session_dir / "current.md").write_text("legacy", encoding="utf-8")
        (session_dir / "state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "window_count": 4,
                    "active_rollover": {
                        "turn_id": "turn-1",
                        "phase": "committed",
                        "generated_sha256": continuity._checkpoint_sha256("legacy"),
                        "pending_sha256": continuity._checkpoint_sha256("legacy"),
                        "current_sha256": continuity._checkpoint_sha256("legacy"),
                    },
                }
            ),
            encoding="utf-8",
        )
        result, output, errors = self._run(
            self._event("SessionStart", None, source="compact")
        )
        self.assertEqual(result, 0)
        self.assertIn("verification failed", output)
        self.assertIn("v2", errors)

    def test_pre_tool_use_saves_and_verifies_pending_chain(self) -> None:
        transcript = self._transcript(*self._summary_records("new context summary"))
        result, output, errors = self._run(
            self._event("PreToolUse", transcript, tool_name="new_context")
        )
        self.assertEqual((result, output, errors), (0, "", ""))
        state = self._state()
        active = state["active_rollover"]
        self.assertEqual(active["phase"], "pending")
        self.assertEqual(active["generated_sha256"], active["pending_sha256"])
        self.assertEqual(
            continuity._read_checkpoint(self._session_dir() / "pending.md")[1],
            active["pending_sha256"],
        )

    def test_exact_additional_context_bytes_accept_and_reject_multibyte_content(self) -> None:
        base_bytes = len(continuity._build_additional_context("").encode("utf-8"))
        room = continuity.MAX_ADDITIONAL_CONTEXT_BYTES - base_bytes
        korean_count, ascii_count = divmod(room, 3)
        accepted = "가" * korean_count + "x" * ascii_count
        self.assertIsNone(continuity._summary_problem(accepted))
        self.assertEqual(
            len(continuity._build_additional_context(accepted).encode("utf-8")),
            continuity.MAX_ADDITIONAL_CONTEXT_BYTES,
        )
        rejected = accepted + "x"
        problem = continuity._summary_problem(rejected)
        self.assertIsNotNone(problem)
        self.assertIn("10,001", problem or "")

    def test_pre_tool_use_requires_turn_and_denies_discovery_failure(self) -> None:
        event = self._event("PreToolUse", None, tool_name="new_context", turn_id="")
        result, output, _errors = self._run(event)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_session_start_missing_current_is_a_verification_failure(self) -> None:
        result, output, errors = self._run(
            self._event("SessionStart", None, source="compact")
        )
        self.assertEqual(result, 0)
        self.assertIn("verification failed", output)
        self.assertIn("current checkpoint file is missing", errors)


if __name__ == "__main__":
    unittest.main()
