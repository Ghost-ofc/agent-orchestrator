#!/usr/bin/env python3
"""Read-only extraction of an agent conversation from a job's stdout log.

The dashboard never executes or renders this content as markup; it only returns bounded plain text.
Supported shapes: Claude-style ``stream-json`` (assistant / result lines), Codex ``--json`` events
(``item.completed``), Antigravity ``stream-json`` (``step_update`` / ``result``), OpenCode
``--format json`` (``text`` / ``tool_use`` / ``step_finish``), and plain text. Anything else is counted as unrecognized, never guessed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MAX_BYTES = 4 * 1024 * 1024
MAX_MESSAGES = 400
MAX_TEXT = 20000
MAX_COMMAND_OUTPUT = 2000
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TOOL_KEYS = (
    "file_path", "filePath", "notebook_path", "Path", "path", "AbsolutePath", "TargetFile",
    "command", "CommandLine", "pattern", "url", "Url", "query", "Query", "description",
)
# Tool names (Antigravity / OpenCode) that write a file named by their path argument.
WRITE_TOOL_HINTS = ("write", "edit", "replace", "patch")


def clip(value: Any, limit: int = MAX_TEXT) -> tuple[str, bool]:
    text = value if isinstance(value, str) else ""
    return text[:limit], len(text) > limit


def tool_summary(inputs: Any) -> str:
    if not isinstance(inputs, dict):
        return ""
    for key in TOOL_KEYS:
        value = inputs.get(key)
        if isinstance(value, str) and value:
            return value[:300]
    return ""


class Transcript:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.files: dict[str, str] = {}
        self.stats: dict[str, Any] = {}
        self.formats: set[str] = set()
        self.unrecognized = 0
        self.thinking_blocks = 0
        self.last_text = ""  # Unclipped, so a long final result is not shown twice.
        self._agy_text: dict[int, str] = {}  # Antigravity streams text in deltas per step.
        self._agy_tools: set[int] = set()
        self._plain: list[str] = []

    def add(self, kind: str, role: str, text: str, name: str | None = None) -> None:
        clipped_text, clipped = clip(text)
        if not clipped_text.strip() and kind != "tool":
            return
        message: dict[str, Any] = {"kind": kind, "role": role, "text": clipped_text, "clipped": clipped}
        if name:
            message["name"] = name
        self.messages.append(message)

    def touch(self, path: Any, how: str) -> None:
        if isinstance(path, str) and path:
            self.files[path[:500]] = how

    def flush_plain(self) -> None:
        if self._plain:
            self.add("output", "assistant", "\n".join(self._plain))
            self._plain = []

    def feed_claude(self, value: dict[str, Any]) -> bool:
        kind = value.get("type")
        if kind == "assistant":
            self.formats.add("claude-stream-json")
            content = (value.get("message") or {}).get("content")
            if isinstance(content, str):
                self.last_text = content
                self.add("text", "assistant", content)
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    self.last_text = block.get("text") if isinstance(block.get("text"), str) else ""
                    self.add("text", "assistant", self.last_text)
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")
                    inputs = block.get("input")
                    self.add("tool", "tool", tool_summary(inputs), name)
                    if name in WRITE_TOOLS and isinstance(inputs, dict):
                        self.touch(inputs.get("file_path") or inputs.get("notebook_path"), name)
                elif block.get("type") == "thinking":
                    self.thinking_blocks += 1
            return True
        if kind == "result":
            self.formats.add("claude-stream-json")
            text = value.get("result")
            if isinstance(text, str) and text.strip() and text.strip() != self.last_text.strip():
                self.add("error" if value.get("is_error") else "result", "assistant", text)
            for key in ("num_turns", "duration_ms", "total_cost_usd", "stop_reason", "is_error", "subtype"):
                if key in value and not isinstance(value[key], (dict, list)):
                    self.stats[key] = value[key]
            return True
        if kind in {"system", "user", "rate_limit_event"}:
            self.formats.add("claude-stream-json")
            return True
        return False

    def feed_codex(self, value: dict[str, Any]) -> bool:
        kind = value.get("type")
        if kind == "item.completed" and isinstance(value.get("item"), dict):
            self.formats.add("codex-jsonl")
            item = value["item"]
            item_type = item.get("type")
            if item_type == "agent_message":
                self.add("text", "assistant", item.get("text") or "")
            elif item_type == "command_execution":
                command = item.get("command") if isinstance(item.get("command"), str) else ""
                output, _ = clip(item.get("aggregated_output"), MAX_COMMAND_OUTPUT)
                exit_code = item.get("exit_code")
                suffix = f"\n→ exit {exit_code}" if exit_code is not None else ""
                self.add("command", "tool", f"{command}{suffix}" + (f"\n{output}" if output.strip() else ""), "shell")
            elif item_type == "file_change":
                for change in item.get("changes") if isinstance(item.get("changes"), list) else []:
                    if isinstance(change, dict):
                        self.touch(change.get("path"), str(change.get("kind") or "change"))
                        self.add("tool", "tool", str(change.get("path") or ""), str(change.get("kind") or "file_change"))
            elif item_type == "mcp_tool_call":
                self.add("tool", "tool", tool_summary(item.get("arguments")), f"{item.get('server', '')}.{item.get('tool', '')}".strip("."))
            elif item_type == "error":
                self.add("error", "system", item.get("message") or "")
            return True
        if kind in {"thread.started", "turn.started", "turn.completed", "item.started", "item.updated"}:
            self.formats.add("codex-jsonl")
            return True
        if kind in {"error", "turn.failed"}:
            self.formats.add("codex-jsonl")
            error = value.get("error")
            message = value.get("message") or (error.get("message") if isinstance(error, dict) else None)
            self.add("error", "system", message if isinstance(message, str) else "")
            return True
        return False

    def feed_antigravity(self, value: dict[str, Any]) -> bool:
        event = value.get("event")
        if event == "init":
            self.formats.add("antigravity-stream-json")
            return True
        if event == "step_update" and isinstance(value.get("step_update"), dict):
            self.formats.add("antigravity-stream-json")
            step = value["step_update"]
            index = step.get("step_index") if isinstance(step.get("step_index"), int) else -1
            kind = step.get("step_type")
            if kind == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str):
                    self._agy_text[index] = self._agy_text.get(index, "") + delta
                if step.get("state") == "DONE" and index in self._agy_text:
                    self.last_text = self._agy_text.pop(index)
                    self.add("text", "assistant", self.last_text)
            elif kind == "tool" and index not in self._agy_tools:
                self._agy_tools.add(index)
                info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
                name = str(step.get("tool_name") or info.get("name") or "tool")
                parameters = info.get("parameters")
                self.add("tool", "tool", tool_summary(parameters), name)
                if any(hint in name.lower() for hint in WRITE_TOOL_HINTS) and isinstance(parameters, dict):
                    self.touch(next((parameters[key] for key in TOOL_KEYS if isinstance(parameters.get(key), str)), None), name)
            return True
        if event == "result" and isinstance(value.get("result"), dict):
            self.formats.add("antigravity-stream-json")
            result = value["result"]
            status = result.get("status")
            response = result.get("response")
            if status not in (None, "SUCCESS"):
                self.add("error", "system", str(result.get("error") or status))
            elif isinstance(response, str) and response.strip() and response.strip() != self.last_text.strip():
                self.add("result", "assistant", response)
            for key in ("num_turns", "duration_seconds"):
                if isinstance(result.get(key), (int, float)):
                    self.stats[key] = result[key]
            if status is not None:
                self.stats["status"] = status
            return True
        return False

    def feed_opencode(self, value: dict[str, Any]) -> bool:
        kind = value.get("type")
        if "sessionID" not in value or kind not in {"step_start", "step_finish", "text", "tool_use", "error"}:
            return False
        self.formats.add("opencode-json")
        part = value.get("part") if isinstance(value.get("part"), dict) else {}
        if kind == "text":
            self.last_text = part.get("text") if isinstance(part.get("text"), str) else ""
            self.add("text", "assistant", self.last_text)
        elif kind == "tool_use":
            name = str(part.get("tool") or "tool")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inputs = state.get("input")
            self.add("tool", "tool", tool_summary(inputs) or str(state.get("title") or ""), name)
            if state.get("status") == "error":
                self.add("error", "system", str(state.get("error") or ""))
            if any(hint in name.lower() for hint in WRITE_TOOL_HINTS) and isinstance(inputs, dict):
                self.touch(next((inputs[key] for key in TOOL_KEYS if isinstance(inputs.get(key), str)), None), name)
        elif kind == "error":
            error = value.get("error") if isinstance(value.get("error"), dict) else {}
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            self.add("error", "system", str(data.get("message") or error.get("name") or ""))
        return True

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            self._plain.append(ANSI.sub("", line))
            if len(self._plain) >= 200:
                self.flush_plain()
            return
        self.flush_plain()
        if not isinstance(value, dict) or not (
            self.feed_antigravity(value)
            or self.feed_opencode(value)
            or self.feed_claude(value)
            or self.feed_codex(value)
        ):
            self.unrecognized += 1


def read_transcript(log_path: Path) -> dict[str, Any]:
    """Return a bounded, JSON-safe conversation. Missing or unreadable logs yield an empty result."""
    transcript = Transcript()
    size = 0
    truncated_log = False
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size > MAX_BYTES:
                handle.seek(size - MAX_BYTES)
                handle.readline()  # Discard the partial first line.
                truncated_log = True
            else:
                handle.seek(0)
            for raw in handle:
                transcript.feed(raw.decode("utf-8", errors="replace"))
    except OSError:
        pass
    transcript.flush_plain()
    for index in sorted(transcript._agy_text):
        transcript.add("text", "assistant", transcript._agy_text[index])
    dropped = max(0, len(transcript.messages) - MAX_MESSAGES)
    messages = transcript.messages[-MAX_MESSAGES:]
    if len(transcript.formats) > 1:
        detected = "mixed"
    else:
        detected = next(iter(sorted(transcript.formats)), "plain" if messages else "empty")
    return {
        "format": detected,
        "messages": [{"index": index + dropped, **message} for index, message in enumerate(messages)],
        "dropped_messages": dropped,
        "files": [{"path": path, "tool": how} for path, how in transcript.files.items()],
        "stats": transcript.stats,
        "thinking_blocks": transcript.thinking_blocks,
        "unrecognized_lines": transcript.unrecognized,
        "log_bytes": size,
        "log_truncated": truncated_log,
    }
