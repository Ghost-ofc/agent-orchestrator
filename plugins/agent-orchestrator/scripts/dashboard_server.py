#!/usr/bin/env python3
"""Dependency-free, loopback-only view of durable Agent Orchestrator state."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cli_agent_job as jobs
import panel_actions
import native_folders
import project_access
import ssh_inventory
from project_identity import project_root, project_id, project_identity
from agent_transcript import read_transcript

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/logo.png": ("logo.png", "image/png"),
    "/actions.js": ("actions.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MAX_BODY = 65536
MAX_ACTION_BODY = 262144
ETAG_RE = re.compile(r"[0-9a-z-]{1,64}")
ACTIVE = {"starting", "running", "needs_input", "cancelling"}
FAILURE = {"failed", "lost", "scope_violated", "timed_out"}
FOLDER_PICKER_LOCK = threading.Lock()



def project_registry_path() -> Path:
    return jobs.state_home() / "projects.json"


def project_registry() -> dict:
    path = project_registry_path()
    return jobs.read_json(path) if path.exists() else {}


def visible_project(pid: str) -> dict:
    jobs.validate_identifier(pid, "project")
    entry = project_registry().get(pid, {})
    if entry.get("deleted"):
        raise jobs.RunnerError("Project removed from dashboard")
    return entry


def activity_key(item: dict) -> tuple:
    return (
        bool(item.get("attention_required")),
        bool(item.get("active")),
        item.get("updated_at") or "",
    )


def feedback_public(record: dict) -> dict:
    public = {
        key: record.get(key)
        for key in (
            "id",
            "state",
            "source",
            "question",
            "context",
            "project_name",
            "plan_id",
            "checklist_item",
            "created_at",
            "answered_at",
            "answer",
            "fallback_job_id",
            "fallback_started_at",
        )
    }
    workspace = record.get("workspace")
    if isinstance(workspace, str) and workspace:
        public["project_name"] = project_identity(workspace)[1]
    return public


class DashboardState:
    """Small observation cache; every mutation still rereads durable state under its record lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.snapshots: dict = {}
        self.transcripts: dict = {}

    def changed_files(self, workspace: str, baseline: dict) -> list[str] | None:
        with self.lock:
            canonical = str(Path(workspace).resolve())
            cached = self.snapshots.get(canonical)
            if not cached or time.monotonic() - cached[0] >= 2:
                try:
                    snapshot = jobs.git_snapshot(Path(canonical))
                except (
                    jobs.RunnerError,
                    OSError,
                    TimeoutError,
                    jobs.subprocess.TimeoutExpired,
                ):
                    snapshot = {"is_git": False}
                cached = (time.monotonic(), snapshot)
                self.snapshots[canonical] = cached
            current = cached[1]
        if not current.get("is_git"):
            return None
        return sorted(
            jobs.status_paths(current.get("status", []))
            - jobs.status_paths(baseline.get("status", []))
        )

    def job(self, path: Path) -> tuple[dict, dict]:
        status = jobs.get_status(path)
        workspace = status["workspace"]
        pid, project_name = project_identity(workspace)
        events = jobs.read_events(path, 40)
        progress = [event for event in events if event.get("type") == "worker_progress"]
        latest = events[-1] if events else {}
        files = self.changed_files(workspace, status.get("git_baseline", {}))
        state = status.get("state")
        if state == "succeeded":
            state = status.get("acceptance_state", "awaiting_review")
        questions = [
            {
                **{
                    key: question.get(key)
                    for key in ("id", "state", "question", "created_at")
                },
                "source": "worker",
                "job_id": path.name,
                "project_id": pid,
                "project_name": project_name,
            }
            for question in status.get("pending_questions", [])
        ]
        row = {
            "job_id": path.name,
            "project_id": pid,
            "source": "worker",
            "cli": status.get("cli") or status.get("agent"),
            "model": status.get("model"),
            "cli_agent": status.get("cli_agent"),
            "role": status.get("role"),
            "access": status.get("access"),
            "coordinator_model": status.get("coordinator_model")
            or "current-codex-task",
            "group": status.get("group"),
            "plan_id": status.get("plan_id"),
            "checklist_item": status.get("checklist_item"),
            "state": state,
            "execution_state": status.get("execution_state"),
            "review_state": status.get("review_state"),
            "phase": status.get("current_phase"),
            "elapsed_seconds": jobs.elapsed_seconds(status),
            "created_at": status.get("created_at"),
            "updated_at": latest.get("at")
            or status.get("finished_at")
            or status.get("created_at"),
            "recent_message": progress[-1].get("message") if progress else None,
            "current_work": (
                progress[-1].get("message") if progress else status.get("role")
            ),
            "changed_files": len(files) if files is not None else None,
            "usage": status.get("usage_summary"),
            "pending_questions": questions,
            "active": status.get("execution_state") in ACTIVE,
            "attention_required": bool(questions)
            or state in FAILURE
            or state in {"awaiting_review", "repair_required", "rejected"},
        }
        return row, {
            "status": status,
            "events": events,
            "files": files,
            "project_id": pid,
            "project_name": project_name,
        }

    def overview(self) -> dict:
        projects: dict[str, dict] = {}
        skipped = 0

        def ensure_identity(pid: str, name: str) -> dict:
            return projects.setdefault(
                pid,
                {
                    "id": pid,
                    "name": name,
                    "jobs": [],
                    "pending_feedback": [],
                    "plans": [],
                    "updated_at": "",
                },
            )

        def ensure(workspace: str) -> dict:
            return ensure_identity(*project_identity(workspace))

        for path in sorted(jobs.jobs_dir().glob("*")):
            if not path.is_dir() or not (path / "meta.json").is_file():
                continue
            try:
                jobs.validate_identifier(path.name, "job")
                row, detail = self.job(path)
                ensure_identity(detail["project_id"], detail["project_name"])[
                    "jobs"
                ].append(row)
            except (jobs.RunnerError, OSError, KeyError, ValueError, TypeError):
                skipped += 1
        for record in jobs.read_feedback():
            try:
                project = ensure(record["workspace"])
                project["updated_at"] = max(
                    project["updated_at"],
                    record.get("answered_at") or record["created_at"],
                )
                if record.get("state") == "pending":
                    project["pending_feedback"].append(
                        {**feedback_public(record), "project_id": project["id"]}
                    )
            except (KeyError, ValueError, TypeError):
                skipped += 1
        for path in sorted(jobs.plans_dir().glob("*/plan.json")):
            try:
                plan = jobs.read_json(path)
                project = ensure(plan["workspace"])
                project["plans"].append(
                    {
                        key: plan.get(key)
                        for key in ("plan_id", "title", "counts", "complete")
                    }
                )
                project["updated_at"] = max(
                    project["updated_at"],
                    plan.get("updated_at") or plan.get("created_at") or "",
                )
            except (jobs.RunnerError, KeyError, ValueError, TypeError):
                skipped += 1
        for pid, entry in project_registry().items():
            if entry.get("deleted"):
                projects.pop(pid, None)
                continue
            project = ensure_identity(pid, entry["name"])
            project["name"] = entry["name"]
            project["archived"] = bool(entry.get("archived"))
            project["access_restricted"] = bool(entry.get("access_policy", {}).get("enabled"))
            project["updated_at"] = max(project["updated_at"], entry.get("updated_at", ""))
        pending = []
        for project in projects.values():
            project.setdefault("archived", False)
            project["jobs"].sort(key=activity_key, reverse=True)
            rows = project["jobs"]
            project["active"] = sum(bool(row["active"]) for row in rows)
            project["question_count"] = len(project["pending_feedback"]) + sum(
                len(row["pending_questions"]) for row in rows
            )
            # Project-level attention is reserved for questions a user can act on now.
            # Historical failures and unreviewed runs stay visible on their agent rows
            # without turning an old project into a permanent alarm.
            project["attention_required"] = project["question_count"] > 0
            project["attention_count"] = project["question_count"]
            project["review_queue"] = sum(
                row["state"] in {"awaiting_review", "repair_required", "rejected"}
                for row in rows
            )
            project["failed"] = sum(row["state"] in FAILURE for row in rows)
            project["updated_at"] = max(
                [project["updated_at"], *[row["updated_at"] or "" for row in rows]]
            )
            project["completed"] = sum(row["state"] == "accepted" for row in rows)
            pending.extend(project["pending_feedback"])
            for row in rows:
                pending.extend(row["pending_questions"])
        return {
            "projects": sorted(projects.values(), key=activity_key, reverse=True),
            "pending_questions": pending,
            "updated_at": jobs.utc_now(),
            "unreadable_records": skipped,
        }

    def project(self, pid: str) -> dict:
        jobs.validate_identifier(pid, "project")
        project = next(
            (item for item in self.overview()["projects"] if item["id"] == pid), None
        )
        if project is None:
            raise jobs.RunnerError("Unknown project ID")
        project["feedback_history"] = []
        for record in jobs.read_feedback():
            workspace = record.get("workspace")
            if (
                isinstance(workspace, str)
                and workspace
                and project_id(workspace) == pid
                and record.get("state") != "pending"
            ):
                project["feedback_history"].append(feedback_public(record))
        return project

    def verify_job(self, pid: str, job_id: str) -> Path:
        visible_project(pid)
        path = jobs.job_dir(job_id)
        if project_id(jobs.read_json(path / "meta.json")["workspace"]) != pid:
            raise jobs.RunnerError("Unknown job in this project")
        return path

    def detail(self, pid: str, job_id: str) -> dict:
        path = self.verify_job(pid, job_id)
        row, detail = self.job(path)
        events = [
            {
                key: event.get(key)
                for key in (
                    "id",
                    "at",
                    "type",
                    "source",
                    "phase",
                    "message",
                    "question",
                    "state",
                    "verdict",
                )
            }
            for event in detail["events"]
        ]
        return {
            **row,
            "events": events,
            "has_flow": (path / "flow.json").is_file(),
            "questions": [
                {
                    key: q.get(key)
                    for key in (
                        "id",
                        "state",
                        "question",
                        "created_at",
                        "answer",
                        "answered_at",
                        "expired_at",
                    )
                }
                for q in jobs.read_questions(path)
            ],
            "files": detail["files"],
            "review": detail["status"].get("review"),
            "logs": {
                "label": "Local / private · last 8 KiB per stream",
                "stdout": local_tail(path / "stdout.log"),
                "stderr": local_tail(path / "stderr.log"),
            },
        }

    def project_workspace(self, pid: str) -> str:
        """Resolve a project ID to its registered folder or a workspace from durable history."""
        registered = visible_project(pid)
        if registered.get("workspace"):
            return registered["workspace"]
        records = [
            jobs.read_json(path)
            for pattern, root in (("*/meta.json", jobs.jobs_dir()), ("*/plan.json", jobs.plans_dir()))
            for path in sorted(root.glob(pattern))
        ] + list(jobs.read_feedback())
        for record in records:
            workspace = record.get("workspace")
            if isinstance(workspace, str) and workspace and project_id(workspace) == pid:
                return workspace
        raise jobs.RunnerError("Unknown project ID")

    def verified_plan(self, pid: str, plan_id: str) -> tuple[Path, dict]:
        visible_project(pid)
        if not jobs.JOB_ID_RE.fullmatch(plan_id):
            raise jobs.RunnerError("Invalid plan ID")
        path, plan = jobs.read_plan(plan_id)
        if project_id(plan["workspace"]) != pid:
            raise jobs.RunnerError("Unknown plan in this project")
        return path, plan

    def plan(self, pid: str, plan_id: str) -> dict:
        result = panel_actions.plan_view(self.verified_plan(pid, plan_id)[1])
        project_policy = project_access.policy_for(Path(self.project_workspace(pid)), jobs.state_home())
        result["access_policy"] = project_access.public_policy(project_policy)
        if visible_project(pid).get("archived"):
            for item in result["items"]:
                item["can_launch"] = False
        return result

    def launch_template(self, pid: str, job_id: str) -> dict:
        return panel_actions.launch_template(self.verify_job(pid, job_id))

    def choose_folder(self, body: dict) -> dict:
        body = panel_actions.only_fields(body, ("mode",), ("initial",))
        mode = panel_actions.choice(body, "mode", ("existing", "new"))
        initial = panel_actions.text(body, "initial", required=False, limit=2000, single_line=True)
        folder = Path(initial).expanduser() if initial else Path.home()
        if not folder.is_absolute():
            raise panel_actions.invalid("initial", "La ruta inicial debe ser absoluta.")
        if not folder.is_dir():
            folder = folder.parent if folder.parent.is_dir() else Path.home()
        if not FOLDER_PICKER_LOCK.acquire(blocking=False):
            raise panel_actions.ActionError(409, "project_picker_busy", "Ya hay un selector de carpetas abierto en este equipo.")
        try:
            path = native_folders.pick_folder(folder, "Elegir carpeta del proyecto" if mode == "existing" else "Elegir dónde crear el proyecto")
        except native_folders.FolderPickerError as exc:
            raise panel_actions.ActionError(503, "project_picker_unavailable", str(exc)) from exc
        finally:
            FOLDER_PICKER_LOCK.release()
        return {"cancelled": path is None, "path": path}

    def create_project(self, body: dict) -> dict:
        body = panel_actions.only_fields(body, ("name", "workspace", "mode"), ("init_git",))
        name = panel_actions.text(body, "name", limit=100, single_line=True)
        location = panel_actions.text(body, "workspace", limit=2000, single_line=True)
        mode = panel_actions.choice(body, "mode", ("existing", "new"))
        initialize = panel_actions.flag(body, "init_git")
        workspace = Path(location).expanduser()
        if not workspace.is_absolute():
            raise panel_actions.invalid("workspace", "Usa una ruta absoluta, por ejemplo ~/Proyectos/mi-app.")
        workspace = workspace.resolve()
        registry_path = project_registry_path()
        registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with jobs.record_lock(registry_path):
            records = project_registry()
            pid = project_id(str(workspace))
            if pid in records and not records[pid].get("deleted"):
                raise panel_actions.ActionError(409, "project_conflict", "Esta carpeta ya está en el panel.")
            if mode == "new":
                if workspace.exists() or not workspace.parent.is_dir():
                    raise panel_actions.invalid("workspace", "La carpeta nueva no debe existir y su carpeta padre debe existir.")
                if not initialize and (project_root(str(workspace.parent)) / ".git").exists():
                    raise panel_actions.invalid("init_git", "Activa Git para crear un proyecto independiente dentro de otro repositorio.")
                workspace.mkdir(mode=0o700)
                if initialize:
                    try:
                        result = subprocess.run(["git", "-C", str(workspace), "init"],
                                                capture_output=True, text=True, timeout=15, check=False)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise panel_actions.ActionError(422, "project_error", "La carpeta se creó, pero Git no pudo inicializarse. Puedes incorporarla como carpeta existente.") from exc
                    if result.returncode:
                        raise panel_actions.ActionError(422, "project_error", "La carpeta se creó, pero Git no pudo inicializarse. Puedes incorporarla como carpeta existente.")
            elif not workspace.is_dir():
                raise panel_actions.invalid("workspace", "La carpeta existente no está disponible.")
            # Worktrees share a single entry with their main checkout.
            workspace = project_root(str(workspace))
            pid = project_id(str(workspace))
            if pid in records and not records[pid].get("deleted"):
                raise panel_actions.ActionError(409, "project_conflict", "Este repositorio ya está en el panel.")
            records[pid] = {**records.get(pid, {}), "name": name, "workspace": str(workspace), "archived": False,
                            "deleted": False, "updated_at": jobs.utc_now()}
            jobs.write_json(registry_path, records)
        return self.project(pid)

    def organize_ssh(self, body: dict) -> dict:
        body = panel_actions.only_fields(body, ("alias", "group", "environment"))
        alias = panel_actions.text(body, "alias", limit=128, single_line=True)
        group = panel_actions.text(body, "group", limit=80, single_line=True)
        environment = panel_actions.choice(body, "environment", ssh_inventory.ENVIRONMENTS)
        if environment is None:
            raise panel_actions.invalid("environment")
        catalog = ssh_inventory.inventory()
        if not any(host["name"] == alias for host in catalog["hosts"]):
            raise panel_actions.invalid("alias", "El host ya no está en el inventario SSH.")
        path = ssh_inventory.organization_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with jobs.record_lock(path):
            organization = ssh_inventory.read_organization()
            organization["hosts"][alias] = {"group": group, "environment": environment}
            jobs.write_json(path, organization)
        return ssh_inventory.inventory()

    def access_settings(self, pid: str) -> dict:
        self.project(pid)
        workspace = Path(self.project_workspace(pid))
        try:
            definitions = project_access.mcp_definitions(workspace)
            origins = project_access.mcp_origins(workspace)
            policy = project_access.policy_for(workspace, jobs.state_home())
        except (ValueError, OSError, project_access.AccessError) as exc:
            raise panel_actions.ActionError(422, "project_access_error", "No se pudo leer la configuración MCP de este proyecto.") from exc
        return {"policy": project_access.public_policy(policy), "ssh_inventory": ssh_inventory.inventory(),
                "mcp_catalog": [{"name": name, "available": name in definitions, "source": origins.get(name, "Configuración guardada")} for name in sorted(set(definitions) | set(policy.get("mcp_servers", {})))]}

    def save_access(self, pid: str, body: dict) -> dict:
        registry_path = project_registry_path()
        registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with jobs.record_lock(registry_path):
            project = self.project(pid)
            if project["active"]:
                raise panel_actions.ActionError(409, "project_active", "Espera a que terminen los agentes antes de cambiar sus accesos.")
            workspace = Path(self.project_workspace(pid))
            try:
                policy = project_access.normalize_policy(body, workspace, project_access.policy_for(workspace, jobs.state_home()))
                if policy["enabled"]:
                    profile = jobs.validate_profile("claude-code", jobs.BUILTIN_PROFILES["claude-code"])
                    project_access.require_supported("claude-code", profile, profile)
            except (ValueError, OSError, project_access.AccessError) as exc:
                raise panel_actions.ActionError(422, "project_access_error", str(exc)) from exc
            records = project_registry()
            entry = records.get(pid) or {"name": project["name"], "workspace": str(workspace), "archived": False}
            entry["access_policy"] = policy
            entry["updated_at"] = jobs.utc_now()
            records[pid] = entry
            jobs.write_json(registry_path, records)
        return self.access_settings(pid)

    def project_settings(self, pid: str) -> dict:
        project = self.project(pid)
        return {"id": pid, "name": project["name"], "archived": project["archived"],
                "workspace": self.project_workspace(pid), "active": project["active"]}

    def update_project(self, pid: str, body: dict) -> dict:
        body = panel_actions.only_fields(body, ("action",), ("name", "confirm"))
        action = panel_actions.choice(body, "action", ("rename", "archive", "restore", "delete"))
        name = panel_actions.text(body, "name", required=action == "rename", limit=100, single_line=True)
        if action == "delete" and body.get("confirm") is not True:
            raise panel_actions.invalid("confirm", "Confirma que quieres eliminar el proyecto del panel.")
        registry_path = project_registry_path()
        registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with jobs.record_lock(registry_path):
            project = self.project(pid)
            if action in {"archive", "delete"} and project["active"]:
                raise panel_actions.ActionError(409, "project_active", "Hay agentes en ejecución. Deténlos o espera a que terminen antes de archivar o eliminar este proyecto.")
            records = project_registry()
            entry = records.get(pid) or {"workspace": self.project_workspace(pid), "name": project["name"], "archived": False}
            if action == "rename":
                entry["name"] = name
            elif action in {"archive", "restore"}:
                entry["archived"] = action == "archive"
            elif action == "delete":
                entry["deleted"] = True
            entry["updated_at"] = jobs.utc_now()
            records[pid] = entry
            jobs.write_json(registry_path, records)
        return {"id": pid, "deleted": action == "delete"}

    def act(self, parts: list[str], body: dict) -> dict:
        """Dispatch a state-changing request. The engine itself enforces every policy."""
        if parts == ["api", "ssh", "organization"]:
            return self.organize_ssh(body)
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "access":
            return self.save_access(parts[2], body)
        if parts == ["api", "folders", "pick"]:
            return self.choose_folder(body)
        if parts == ["api", "projects"]:
            return self.create_project(body)
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "manage":
            return self.update_project(parts[2], body)
        if len(parts) < 4 or parts[:2] != ["api", "projects"]:
            raise jobs.RunnerError("Unknown action route")
        registry_path = project_registry_path()
        registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with jobs.record_lock(registry_path):
            return self.act_project(parts, body)

    def act_project(self, parts: list[str], body: dict) -> dict:
        # Serialize launches against archive/delete so no panel launch can race a lifecycle change.
        pid = parts[2]
        registered = visible_project(pid)
        if registered.get("archived") and (parts[3] in {"flows", "plans"}):
            raise panel_actions.ActionError(409, "project_archived", "Restaura el proyecto antes de crear o ejecutar tareas.")
        if len(parts) == 6 and parts[3] == "jobs" and parts[5] in {"cancel", "review"}:
            self.verify_job(pid, parts[4])
            if parts[5] == "cancel":
                return panel_actions.cancel_job(parts[4], body)
            review = panel_actions.review_job(parts[4], body)
            return {key: review.get(key) for key in ("review_id", "verdict", "reviewer", "reviewed_at", "tests", "notes")}
        if len(parts) == 4 and parts[3] == "flows":
            return panel_actions.start_flow(self.project_workspace(pid), body)
        if len(parts) == 7 and parts[3] == "jobs" and parts[5:] == ["flow", "accept"]:
            path = self.verify_job(pid, parts[4])
            return panel_actions.plan_view(panel_actions.accept_flow(path, self.project_workspace(pid), body))
        if len(parts) == 4 and parts[3] == "plans":
            return panel_actions.plan_view(panel_actions.create_plan(self.project_workspace(pid), body))
        if len(parts) == 8 and parts[3] == "plans" and parts[5] == "items" and parts[7] == "launch":
            _, plan = self.verified_plan(pid, parts[4])
            if not any(item.get("id") == parts[6] for item in plan.get("items", [])):
                raise jobs.RunnerError("Unknown checklist item")
            launched = panel_actions.launch_item(plan["workspace"], parts[4], parts[6], body)
            return {
                key: launched.get(key)
                for key in ("job_id", "cli", "model", "state", "role", "group", "plan_id",
                            "checklist_item", "review_state", "executor_approval")
            }
        raise jobs.RunnerError("Unknown action route")

    def transcript(self, pid: str, job_id: str, etag: str | None) -> dict:
        """Bounded conversation extracted from stdout.log; cached by (size, mtime) so polling stays cheap."""
        log = self.verify_job(pid, job_id) / "stdout.log"
        try:
            stat = log.stat()
            key = f"{stat.st_size}-{stat.st_mtime_ns}"
        except OSError:
            key = "missing"
        if etag == key:
            return {"etag": key, "unchanged": True}
        with self.lock:
            cached = self.transcripts.get(str(log))
        if cached is None or cached[0] != key:
            cached = (key, read_transcript(log))
            with self.lock:
                if len(self.transcripts) >= 32:
                    self.transcripts.pop(next(iter(self.transcripts)))
                self.transcripts[str(log)] = cached
        return {"etag": key, "unchanged": False, **cached[1]}

    def answer(self, parts: list[str], value: str) -> dict:
        if (
            len(parts) == 8
            and parts[:2] == ["api", "projects"]
            and parts[3] == "jobs"
            and parts[5] == "questions"
            and parts[7] == "answer"
        ):
            self.verify_job(parts[2], parts[4])
            return jobs.answer_worker_question(parts[4], parts[6], value)
        if (
            len(parts) == 6
            and parts[:2] == ["api", "projects"]
            and parts[3] == "feedback"
            and parts[5] == "answer"
        ):
            visible_project(parts[2])
            path = jobs.feedback_dir(parts[4])
            if (
                project_id(jobs.read_json(path / "feedback.json")["workspace"])
                != parts[2]
            ):
                raise jobs.RunnerError("Unknown feedback in this project")
            record = jobs.answer_feedback(parts[4], value)
            return feedback_public(record)
        raise jobs.RunnerError("Unknown answer route")


def local_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 8192))
            return jobs.ANSI_RE.sub(
                "", handle.read(8192).decode("utf-8", errors="replace")
            )
    except OSError:
        return ""


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0):
        self.token = secrets.token_urlsafe(32)
        self.state = DashboardState()
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = f"{self.origin}/#token={self.token}"


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "AgentOrchestrator"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": message or "Unsupported HTTP request"})

    def log_message(self, format, *args):
        pass  # URLs and selected local state never go to access logs.

    def respond(
        self, status: int, body, content_type="application/json; charset=utf-8"
    ):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def boundary(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if hosts != [self.server.origin.removeprefix("http://")] or (
            origins and origins != [self.server.origin]
        ):
            self.respond(403, {"error": "Loopback Host / Origin required"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "Cross-site request rejected"})
            return False
        return True

    def authenticated(self) -> bool:
        tokens = self.headers.get_all("X-Orchestrator-Token", [])
        if len(tokens) != 1 or not secrets.compare_digest(
            tokens[0].encode("utf-8"), self.server.token.encode("ascii")
        ):
            self.respond(
                401,
                {
                    "error": "Open the dashboard URL printed by dashboard-web to authenticate"
                },
            )
            return False
        return True

    def do_GET(self):
        if not self.boundary():
            return
        # Exact paths only: no URL decoding, filesystem-derived routes, or wildcard serving.
        if self.path in STATIC:
            name, mime = STATIC[self.path]
            self.respond(200, (WEB_ROOT / name).read_bytes(), mime)
            return
        if not self.path.startswith("/api/"):
            self.respond(404, {"error": "Not found"})
            return
        if not self.authenticated():
            return
        # Only the transcript route accepts a query string (its cache validator); every other
        # route keeps matching the exact path.
        path, _, query = self.path.partition("?")
        parts = path.strip("/").split("/")
        is_transcript = (
            len(parts) == 6
            and parts[:2] == ["api", "projects"]
            and parts[3] == "jobs"
            and parts[5] == "transcript"
        )
        if query and not is_transcript:
            self.respond(404, {"error": "Not found"})
            return
        try:
            if is_transcript:
                etag = dict(
                    pair.split("=", 1) for pair in query.split("&") if "=" in pair
                ).get("etag")
                data = self.server.state.transcript(
                    parts[2], parts[4], etag if etag and ETAG_RE.fullmatch(etag) else None
                )
            elif self.path == "/api/overview":
                data = self.server.state.overview()
            elif len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "access":
                data = self.server.state.access_settings(parts[2])
            elif len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "settings":
                data = self.server.state.project_settings(parts[2])
            elif self.path == "/api/executors":
                data = panel_actions.executor_catalog()
            elif len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "jobs" and parts[5] == "flow":
                data = panel_actions.flow_proposal(self.server.state.verify_job(parts[2], parts[4]))
            elif (
                len(parts) == 5
                and parts[:2] == ["api", "projects"]
                and parts[3] == "plans"
            ):
                data = self.server.state.plan(parts[2], parts[4])
            elif (
                len(parts) == 6
                and parts[:2] == ["api", "projects"]
                and parts[3] == "jobs"
                and parts[5] == "launch-template"
            ):
                data = self.server.state.launch_template(parts[2], parts[4])
            elif len(parts) == 3 and parts[:2] == ["api", "projects"]:
                data = self.server.state.project(parts[2])
            elif (
                len(parts) == 5
                and parts[:2] == ["api", "projects"]
                and parts[3] == "jobs"
            ):
                data = self.server.state.detail(parts[2], parts[4])
            else:
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, data)
        except panel_actions.ActionError as exc:
            self.respond(exc.status, exc.payload())
        except (jobs.RunnerError, KeyError, ValueError, TypeError, OSError):
            self.respond(
                404,
                {
                    "error": "Local record unavailable or ID does not belong to this project"
                },
            )

    def do_POST(self):
        self.close_connection = True
        if not self.boundary() or not self.authenticated():
            return
        if (
            len(self.headers.get_all("Content-Type", [])) != 1
            or self.headers.get_content_type() != "application/json"
        ):
            self.respond(415, {"error": "Content-Type must be application/json"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding")
            or len(lengths) != 1
            or not lengths[0].isdigit()
        ):
            self.respond(400, {"error": "A single Content-Length is required"})
            return
        parts = self.path.strip("/").split("/")
        is_answer = (
            parts[:2] == ["api", "projects"]
            and (
                (len(parts) == 8 and parts[3] == "jobs" and parts[5] == "questions" and parts[7] == "answer")
                or (len(parts) == 6 and parts[3] == "feedback" and parts[5] == "answer")
            )
        )
        if "?" in self.path:
            self.respond(404, {"error": "Not found"})
            return
        length = int(lengths[0])
        limit = MAX_BODY if is_answer else MAX_ACTION_BODY
        if length > limit:
            self.respond(413, {"error": f"JSON body exceeds {limit // 1024} KiB"})
            return
        try:

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate fields")
                    result[key] = value
                return result

            body = json.loads(
                self.rfile.read(length).decode("utf-8"), object_pairs_hook=unique_object
            )
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            if is_answer and (
                set(body) != {"answer"}
                or not isinstance(body["answer"], str)
                or not body["answer"].strip()
            ):
                raise ValueError("Expected only a nonempty answer string")
        except (ValueError, UnicodeError, TimeoutError):
            if is_answer:
                self.respond(
                    400, {"error": "Expected JSON with one nonempty answer string"}
                )
            else:
                self.respond(
                    400, {"error": "Expected a JSON object", "code": "invalid_request"}
                )
            return
        try:
            if is_answer:
                result = self.server.state.answer(parts, body["answer"])
            else:
                result = self.server.state.act(parts, body)
            self.respond(200, result)
        except panel_actions.ActionError as exc:
            self.respond(exc.status, exc.payload())
        except jobs.RunnerError as exc:
            if "not pending" in str(exc):
                self.respond(
                    409,
                    {
                        "error": "This question is no longer pending. Refresh to see its answer."
                    },
                )
            else:
                self.respond(
                    404,
                    {"error": "Unknown ID or record does not belong to this project"},
                )
        except (OSError, KeyError, ValueError):
            self.respond(
                500,
                {"error": "Could not save the local record. Refresh before retrying."},
            )


def serve(port=0, open_browser=True, token=None) -> int:
    with DashboardServer(port) as server:
        if token is not None:
            server.token = token
            server.url = f"{server.origin}/#token={token}"
        runtime_path = jobs.dashboard_runtime_path()
        runtime = {
            "pid": os.getpid(),
            "origin": server.origin,
            "url": server.url,
            "started_at": jobs.utc_now(),
        }
        jobs.write_json(runtime_path, runtime)
        print(f"Agent Orchestrator dashboard: {server.origin if token is not None else server.url}", flush=True)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            return 0
        finally:
            try:
                current = jobs.read_json(runtime_path)
                if current.get("pid") == os.getpid():
                    runtime_path.unlink(missing_ok=True)
            except jobs.RunnerError:
                pass
    return 0
