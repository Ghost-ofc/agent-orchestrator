#!/usr/bin/env python3
"""Read-only extraction of an agent conversation from a job's stdout log.

The dashboard never executes or renders this content as markup; it only returns bounded plain text.
Supported shapes: Claude-style ``stream-json`` (assistant / result lines), Codex ``--json`` events
(``item.completed``), and plain text. Anything else is counted as unrecognized, never guessed.
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
TOOL_KEYS = ("file_path", "notebook_path", "path", "command", "pattern", "url", "query", "description")


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
        if not isinstance(value, dict) or not (self.feed_claude(value) or self.feed_codex(value)):
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
    dropped = max(0, len(transcript.messages) - MAX_MESSAGES)
    messages = transcript.messages[-MAX_MESSAGES:]
    if "claude-stream-json" in transcript.formats and "codex-jsonl" in transcript.formats:
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
