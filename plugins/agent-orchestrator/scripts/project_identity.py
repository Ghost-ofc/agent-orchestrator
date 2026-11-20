"""Canonical project identity shared by CLI policy and dashboard."""
from __future__ import annotations
import hashlib
import subprocess
from pathlib import Path

def project_root(workspace: str) -> Path:
    """Resolve linked Git worktrees to their shared repository checkout."""
    literal = Path(workspace).resolve()
    if not literal.is_dir():
        return literal
    dot_git = literal / ".git"
    if dot_git.is_dir():
        return literal
    if dot_git.is_file():
        try:
            marker = dot_git.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            marker = ""
        prefix = "gitdir: "
        if marker.startswith(prefix) and "\x00" not in marker:
            candidate = Path(marker[len(prefix) :].strip())
            if not candidate.is_absolute():
                candidate = literal / candidate
            try:
                git_dir = candidate.resolve()
            except OSError:
                git_dir = candidate
            if (
                git_dir.parent.name == "worktrees"
                and git_dir.parent.parent.name == ".git"
            ):
                return git_dir.parent.parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(literal), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return literal
    lines = result.stdout.splitlines()
    if (
        result.returncode != 0
        or len(lines) != 1
        or not lines[0].strip()
        or "\x00" in lines[0]
    ):
        return literal
    common = Path(lines[0].strip())
    if not common.is_absolute():
        common = literal / common
    try:
        common = common.resolve()
    except OSError:
        return literal
    return common.parent if common.is_dir() and common != literal else literal


def project_identity(workspace: str) -> tuple[str, str]:
    root = project_root(workspace)
    pid = "p-" + hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return pid, root.name or "Workspace"


def project_id(workspace: str) -> str:
    return project_identity(workspace)[0]
