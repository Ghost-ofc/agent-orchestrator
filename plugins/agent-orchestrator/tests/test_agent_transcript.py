from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import agent_transcript as transcript


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "stdout.log"

    def write(self, *lines):
        self.log.write_text(
            "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
            + "\n"
        )
        return transcript.read_transcript(self.log)

    def test_claude_stream_json_extracts_text_tools_files_and_stats(self):
        result = self.write(
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "SECRET REASONING"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Voy a escribir el archivo."},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/a.txt", "content": "x"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
            ]}},
            {"type": "result", "subtype": "success", "result": "Voy a escribir el archivo.",
             "num_turns": 2, "total_cost_usd": 0.5, "is_error": False},
        )
        self.assertEqual(result["format"], "claude-stream-json")
        self.assertEqual([m["kind"] for m in result["messages"]], ["text", "tool", "tool"])
        self.assertEqual(result["messages"][1]["name"], "Write")
        self.assertEqual(result["messages"][2]["text"], "ls -la")
        # A result identical to the last assistant message is not shown twice.
        self.assertEqual(len([m for m in result["messages"] if m["kind"] == "result"]), 0)
        self.assertEqual(result["files"], [{"path": "/w/a.txt", "tool": "Write"}])
        self.assertEqual(result["stats"]["num_turns"], 2)
        self.assertEqual(result["thinking_blocks"], 1)
        self.assertNotIn("SECRET REASONING", json.dumps(result))

    def test_long_final_result_identical_to_last_message_is_not_duplicated(self):
        long_text = "y" * (transcript.MAX_TEXT + 500)
        result = self.write(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}},
            {"type": "result", "result": long_text},
        )
        self.assertEqual([m["kind"] for m in result["messages"]], ["text"])
        self.assertTrue(result["messages"][0]["clipped"])

    def test_distinct_final_result_is_kept_and_errors_are_flagged(self):
        result = self.write(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "trabajando"}]}},
            {"type": "result", "result": "Resumen final distinto", "is_error": True},
        )
        self.assertEqual([m["kind"] for m in result["messages"]], ["text", "error"])

    def test_codex_jsonl_extracts_messages_commands_files_and_errors(self):
        result = self.write(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Listo."}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q",
                                                "aggregated_output": "1 passed", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [
                {"path": "/w/b.py", "kind": "update"}]}},
            {"type": "turn.failed", "error": {"message": "boom"}},
        )
        self.assertEqual(result["format"], "codex-jsonl")
        self.assertEqual([m["kind"] for m in result["messages"]], ["text", "command", "tool", "error"])
        self.assertIn("exit 0", result["messages"][1]["text"])
        self.assertEqual(result["files"], [{"path": "/w/b.py", "tool": "update"}])
        self.assertEqual(result["messages"][3]["text"], "boom")

    def test_plain_text_is_grouped_and_ansi_is_removed(self):
        result = self.write("\x1b[31mhola\x1b[0m", "mundo")
        self.assertEqual(result["format"], "plain")
        self.assertEqual(result["messages"][0]["kind"], "output")
        self.assertEqual(result["messages"][0]["text"], "hola\nmundo")

    def test_unknown_json_is_counted_not_guessed(self):
        result = self.write({"type": "something_new", "text": "?"}, [1, 2])
        self.assertEqual(result["unrecognized_lines"], 2)
        self.assertEqual(result["messages"], [])

    def test_antigravity_stream_json_joins_text_deltas_and_reads_tool_calls(self):
        step = lambda **kw: {"event": "step_update", "step_update": {"conversation_id": "c", **kw}}
        result = self.write(
            {"event": "init", "init": {"model": "gemini-3.8-flash-low", "tools": []}},
            step(step_index=0, state="DONE", step_type="user_input"),
            step(step_index=1, state="ACTIVE", step_type="tool", tool_name="write_file",
                 tool_info={"name": "write_file", "parameters": {"Path": "/w/hola.txt"}}),
            step(step_index=1, state="DONE", step_type="tool", tool_name="write_file",
                 tool_info={"name": "write_file", "parameters": {"Path": "/w/hola.txt"}}),
            step(step_index=2, state="ACTIVE", step_type="agent_response", text_delta="Se ha"),
            step(step_index=2, state="ACTIVE", step_type="agent_response", text_delta=" creado el archivo."),
            step(step_index=2, state="DONE", step_type="agent_response", text_delta="\n"),
            {"event": "result", "result": {"status": "SUCCESS", "response": "Se ha creado el archivo.\n",
                                           "num_turns": 1, "duration_seconds": 4.5}},
        )
        self.assertEqual(result["format"], "antigravity-stream-json")
        self.assertEqual([m["kind"] for m in result["messages"]], ["tool", "text"])
        self.assertEqual(result["messages"][0]["name"], "write_file")
        self.assertEqual(result["messages"][1]["text"].strip(), "Se ha creado el archivo.")
        self.assertEqual(result["files"], [{"path": "/w/hola.txt", "tool": "write_file"}])
        self.assertEqual((result["stats"]["num_turns"], result["stats"]["status"]), (1, "SUCCESS"))
        self.assertEqual(result["unrecognized_lines"], 0)

    def test_antigravity_shows_text_of_a_step_that_is_still_streaming_and_failures(self):
        step = lambda **kw: {"event": "step_update", "step_update": {"conversation_id": "c", **kw}}
        live = self.write(step(step_index=3, state="ACTIVE", step_type="agent_response", text_delta="Trabajando"))
        self.assertEqual([m["text"] for m in live["messages"]], ["Trabajando"])
        failed = self.write({"event": "result", "result": {"status": "ERROR", "error": "unknown model"}})
        self.assertEqual([(m["kind"], m["text"]) for m in failed["messages"]], [("error", "unknown model")])

    def test_opencode_json_reads_text_tools_errors_and_ignores_step_markers(self):
        base = {"sessionID": "ses_1"}
        result = self.write(
            {"type": "step_start", **base, "part": {"type": "step-start"}},
            {"type": "tool_use", **base, "part": {"tool": "write", "state": {
                "status": "completed", "input": {"filePath": "/w/hola.txt", "content": "x"}, "output": "ok"}}},
            {"type": "tool_use", **base, "part": {"tool": "bash", "state": {
                "status": "completed", "input": {"command": "cat /w/hola.txt"}}}},
            {"type": "text", **base, "part": {"text": "LISTO"}},
            {"type": "step_finish", **base, "part": {"reason": "stop", "tokens": {"total": 1}}},
            {"type": "error", **base, "error": {"name": "APIError", "data": {"message": "maximum number of items is 128"}}},
        )
        self.assertEqual(result["format"], "opencode-json")
        self.assertEqual([m["kind"] for m in result["messages"]], ["tool", "tool", "text", "error"])
        self.assertEqual(result["messages"][1]["text"], "cat /w/hola.txt")
        self.assertEqual(result["messages"][3]["text"], "maximum number of items is 128")
        self.assertEqual(result["files"], [{"path": "/w/hola.txt", "tool": "write"}])
        self.assertEqual(result["unrecognized_lines"], 0)

    def test_codex_error_events_are_not_mistaken_for_opencode_ones(self):
        result = self.write({"type": "error", "message": "codex boom"})
        self.assertEqual((result["format"], result["messages"][0]["text"]), ("codex-jsonl", "codex boom"))

    def test_missing_or_empty_log_is_empty(self):
        self.assertEqual(transcript.read_transcript(self.log)["format"], "empty")
        self.assertEqual(self.write()["messages"], [])

    def test_bounds_on_message_count_text_size_and_log_size(self):
        lines = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": f"m{i}"}]}}
            for i in range(transcript.MAX_MESSAGES + 5)
        ]
        result = self.write(*lines)
        self.assertEqual(len(result["messages"]), transcript.MAX_MESSAGES)
        self.assertEqual(result["dropped_messages"], 5)
        self.assertEqual(result["messages"][0]["index"], 5)
        big = self.write({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * (transcript.MAX_TEXT + 50)}]}})
        self.assertTrue(big["messages"][0]["clipped"])
        self.assertEqual(len(big["messages"][0]["text"]), transcript.MAX_TEXT)
        filler = json.dumps({"type": "system", "subtype": "thinking_tokens"}) + "\n"
        with self.log.open("w") as handle:
            for _ in range(transcript.MAX_BYTES // len(filler) + 10):
                handle.write(filler)
            handle.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "fin"}]}}) + "\n")
        tail = transcript.read_transcript(self.log)
        self.assertTrue(tail["log_truncated"])
        self.assertEqual(tail["messages"][-1]["text"], "fin")


if __name__ == "__main__":
    unittest.main()
