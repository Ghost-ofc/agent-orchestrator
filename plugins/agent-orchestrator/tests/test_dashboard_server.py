from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cli_agent_job as jobs
import dashboard_server as dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ, {"AGENT_ORCHESTRATOR_HOME": str(self.root / "state")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.workspaces = [
            self.root / name for name in ("Toki OS", "Relay", "Decisions", "Plan only")
        ]
        for workspace in self.workspaces:
            workspace.mkdir()
        self.job("alpha", self.workspaces[0])
        self.job("beta", self.workspaces[1], complete=True)
        self.question("alpha", "question-a")
        self.question("beta", "question-b", legacy=True)
        self.feedback = jobs.create_feedback(
            self.workspaces[2],
            "Choose the release scope?",
            context="Only local changes.",
            feedback_id="feedback-a",
            notify=False,
        )
        jobs.write_json(
            jobs.plans_dir() / "plan-only" / "plan.json",
            {
                "plan_id": "plan-only",
                "workspace": str(self.workspaces[3]),
                "title": "Plan only",
                "created_at": jobs.utc_now(),
                "counts": {"pending": 1},
            },
        )
        self.server = dashboard.DashboardServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.pid = dashboard.project_id(str(self.workspaces[0]))
        self.other_pid = dashboard.project_id(str(self.workspaces[1]))
        self.feedback_pid = dashboard.project_id(str(self.workspaces[2]))
        self.answer_url = (
            f"/api/projects/{self.pid}/jobs/alpha/questions/question-a/answer"
        )
        self.feedback_url = (
            f"/api/projects/{self.feedback_pid}/feedback/feedback-a/answer"
        )

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def job(self, job_id, workspace, complete=False):
        path = jobs.job_dir(job_id, must_exist=False)
        jobs.write_json(
            path / "meta.json",
            {
                "job_id": job_id,
                "workspace": str(workspace),
                "worker_pid": os.getpid(),
                "state": "running",
                "created_at": jobs.utc_now(),
                "cli": "codex-cli",
                "model": "gpt-6-astra",
                "role": "Kernel engineer",
                "coordinator_model": "current-codex-task",
                "git_baseline": {},
                "command": ["PRIVATE_COMMAND"],
                "task_stats": {"private": "PRIVATE_TASK"},
            },
        )
        (path / "channel").mkdir(mode=0o700)
        jobs.add_event(path, "agent_started")
        jobs.add_event(
            path / "channel",
            "worker_progress",
            phase="implementation",
            message="Implementing the driver",
        )
        (path / "stdout.log").write_text(
            "PRIVATE_LOG\n"
            + json.dumps({"usage": {"input_tokens": 12, "output_tokens": 3}})
            + "\n"
        )
        if complete:
            jobs.write_json(
                path / "result.json",
                {"state": "succeeded", "finished_at": jobs.utc_now()},
            )
        return path

    def question(self, job_id, question_id, legacy=False):
        path = jobs.job_dir(job_id)
        if not legacy:
            path /= "channel"
        jobs.write_json(
            path / "questions" / f"{question_id}.json",
            {
                "id": question_id,
                "state": "pending",
                "question": "Which mode?",
                "created_at": jobs.utc_now(),
            },
        )

    def request(self, path, method="GET", body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        request_headers = (
            {"X-Orchestrator-Token": self.server.token} if authenticated else {}
        )
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, response_headers, data

    def test_overview_aggregates_jobs_feedback_and_plans_without_private_metadata(self):
        status, _, raw = self.request("/api/overview")
        self.assertEqual(status, 200)
        overview = json.loads(raw)
        self.assertEqual(
            {p["name"] for p in overview["projects"]},
            {"Toki OS", "Relay", "Decisions", "Plan only"},
        )
        self.assertEqual(len(overview["pending_questions"]), 3)
        for private in (
            "PRIVATE_LOG",
            "PRIVATE_COMMAND",
            "PRIVATE_TASK",
            str(self.root),
            "workspace",
        ):
            self.assertNotIn(private, raw.decode())
        row = next(p for p in overview["projects"] if p["id"] == self.pid)["jobs"][0]
        self.assertEqual(row["current_work"], "Implementing the driver")
        self.assertEqual(row["phase"], "implementation")
        self.assertEqual(row["usage"]["input_tokens"], 12)
        self.assertIsNone(row["changed_files"])
        self.assertTrue(row["attention_required"])

    def test_transcript_route_returns_bounded_conversation_with_etag(self):
        path = jobs.job_dir("alpha")
        (path / "stdout.log").write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Hola desde el agente"},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/x.txt"}},
            ]}})
            + "\n"
        )
        url = f"/api/projects/{self.pid}/jobs/alpha/transcript"
        status, _, raw = self.request(url)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["messages"][0]["text"], "Hola desde el agente")
        self.assertEqual(data["files"], [{"path": "/w/x.txt", "tool": "Write"}])
        self.assertFalse(data["unchanged"])
        self.assertNotIn(str(self.root), raw.decode())
        # A matching validator returns no body to re-render; an invalid one is ignored.
        status, _, raw = self.request(f"{url}?etag={data['etag']}")
        self.assertEqual(json.loads(raw), {"etag": data["etag"], "unchanged": True})
        status, _, raw = self.request(f"{url}?etag=../../x")
        self.assertFalse(json.loads(raw)["unchanged"])
        # Growing the log invalidates the cache.
        with (path / "stdout.log").open("a") as handle:
            handle.write(json.dumps({"type": "result", "result": "fin"}) + "\n")
        self.assertFalse(json.loads(self.request(f"{url}?etag={data['etag']}")[2])["unchanged"])

    def test_transcript_route_keeps_authentication_and_project_scope(self):
        url = f"/api/projects/{self.pid}/jobs/alpha/transcript"
        self.assertEqual(self.request(url, authenticated=False)[0], 401)
        wrong_project = f"/api/projects/{self.other_pid}/jobs/alpha/transcript"
        self.assertEqual(self.request(wrong_project)[0], 404)
        self.assertEqual(self.request(f"/api/projects/{self.pid}/jobs/nope/transcript")[0], 404)
        # Query strings stay rejected everywhere except the transcript validator.
        self.assertEqual(self.request("/api/overview?x=1")[0], 404)
        self.assertEqual(self.request(f"/api/projects/{self.pid}/jobs/alpha?x=1")[0], 404)
        status, _, raw = self.request(url)
        self.assertEqual(json.loads(raw)["format"], "plain")

    def test_selected_detail_has_only_short_local_logs_and_review_evidence(self):
        path = jobs.job_dir("alpha")
        (path / "stdout.log").write_text("s" * 20000 + "\x1b[31mTAIL\x1b[0m")
        jobs.write_json(
            path / "review.json",
            {
                "verdict": "accepted",
                "tests": ["unit tests: passed"],
                "reviewer": "Codex",
            },
        )
        status, _, raw = self.request(f"/api/projects/{self.pid}/jobs/alpha")
        self.assertEqual(status, 200)
        detail = json.loads(raw)
        self.assertLessEqual(len(detail["logs"]["stdout"].encode()), 8192)
        self.assertTrue(detail["logs"]["stdout"].endswith("TAIL"))
        self.assertIn("Local / private", detail["logs"]["label"])
        self.assertEqual(detail["review"]["tests"], ["unit tests: passed"])
        self.assertNotIn("command", detail)
        self.assertNotIn(str(self.root), raw.decode())
        self.assertNotIn("workspace", detail)

    def test_git_main_checkout_and_linked_worktree_share_one_project_and_ownership(
        self,
    ):
        repository = self.root / "Dory"
        linked = self.root / "Dory-p2-11-inline-cmov-tlb"
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Dashboard Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "config",
                "user.email",
                "dashboard@example.test",
            ],
            check=True,
        )
        (repository / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(repository), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "seed"], check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "worktree",
                "add",
                "-q",
                "-b",
                "agent-worktree",
                str(linked),
            ],
            check=True,
        )
        self.job("dory-main", repository)
        self.job("dory-worker", linked)
        self.question("dory-worker", "dory-question")
        jobs.create_feedback(
            linked,
            "Keep the worktree scope?",
            feedback_id="dory-feedback",
            notify=False,
        )

        canonical_pid = dashboard.project_id(str(repository))
        self.assertEqual(dashboard.project_id(str(linked)), canonical_pid)
        status, _, raw = self.request("/api/overview")
        self.assertEqual(status, 200)
        overview = json.loads(raw)
        project = next(
            item for item in overview["projects"] if item["id"] == canonical_pid
        )
        self.assertEqual(project["name"], "Dory")
        self.assertEqual(
            {row["job_id"] for row in project["jobs"]}, {"dory-main", "dory-worker"}
        )
        worker = next(row for row in project["jobs"] if row["job_id"] == "dory-worker")
        self.assertEqual(worker["pending_questions"][0]["project_id"], canonical_pid)
        self.assertEqual(worker["pending_questions"][0]["project_name"], "Dory")
        self.assertEqual(project["pending_feedback"][0]["project_id"], canonical_pid)
        self.assertEqual(project["pending_feedback"][0]["project_name"], "Dory")
        self.assertNotIn(linked.name, {item["name"] for item in overview["projects"]})
        self.assertNotIn(str(self.root), raw.decode())
        self.assertNotIn('"workspace"', raw.decode())

        detail_status, _, detail_raw = self.request(
            f"/api/projects/{canonical_pid}/jobs/dory-worker"
        )
        self.assertEqual(detail_status, 200)
        self.assertNotIn(str(self.root), detail_raw.decode())
        answer = f"/api/projects/{canonical_pid}/jobs/dory-worker/questions/dory-question/answer"
        self.assertEqual(
            self.request(answer, "POST", {"answer": "Use the canonical project."})[0],
            200,
        )
        self.assertEqual(
            jobs.read_questions(jobs.job_dir("dory-worker"))[0]["answer"],
            "Use the canonical project.",
        )
        feedback = f"/api/projects/{canonical_pid}/feedback/dory-feedback/answer"
        self.assertEqual(
            self.request(feedback, "POST", {"answer": "Keep it narrow."})[0], 200
        )
        canonical_project = json.loads(
            self.request(f"/api/projects/{canonical_pid}")[2]
        )
        self.assertEqual(
            canonical_project["feedback_history"][0]["answer"], "Keep it narrow."
        )
        self.assertEqual(
            canonical_project["feedback_history"][0]["project_name"], "Dory"
        )
        self.assertNotIn(str(self.root), json.dumps(canonical_project))

    def test_static_allowlist_and_security_headers(self):
        for path in ("/", "/index.html", "/styles.css", "/i18n.js", "/favicon.svg", "/app.js"):
            status, headers, _ = self.request(path, authenticated=False)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in (
            "/../scripts/cli_agent_job.py",
            "/%2e%2e/README.md",
            "/README.md",
            "/app.js?file=meta.json",
            "/state/jobs/alpha/meta.json",
        ):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/overview", "OPTIONS")[0], 501)

    def test_static_sources_define_accessible_keyed_agents_navigation(self):
        web = SCRIPTS.parent / "web"
        html = (web / "index.html").read_text()
        script = (web / "app.js").read_text()
        styles = (web / "styles.css").read_text()
        self.assertIn(
            '<aside class="agents-sidebar" data-i18n-aria="agents.label">', html
        )
        self.assertIn(
            '<nav id="agent-navigation" data-i18n-aria="agents.listLabel">', html
        )
        self.assertIn('id="agent-count"', html)
        self.assertIn("reconcile($('agent-navigation'), items", script)
        self.assertIn("button.setAttribute('aria-current'", script)
        self.assertIn("t('agents.overview')", script)
        self.assertIn("selected ? [selected]", script)
        self.assertIn(".agents-sidebar", styles)
        self.assertIn(".agent-nav-button[aria-current=true]", styles)
        self.assertIn("position: sticky", styles)

    def test_interface_is_spanish_with_a_central_dictionary(self):
        web = SCRIPTS.parent / "web"
        html = (web / "index.html").read_text()
        script = (web / "app.js").read_text()
        dictionary = (web / "i18n.js").read_text()
        self.assertIn('<html lang="es">', html)
        # The i18n script must load before the app that destructures window.I18N.
        self.assertLess(html.index("/i18n.js"), html.index("/app.js"))
        defined = set(re.findall(r"^\s*'([\w.]+)':", dictionary, re.M))
        used = set(re.findall(r"\bt\('([\w.]+)'", script))
        used |= set(re.findall(r'data-i18n(?:-aria|-title)?="([\w.]+)"', html))
        plural_bases = set(re.findall(r"\btn\('([\w.]+)'", script))
        for base in plural_bases:
            used |= {f"{base}.one", f"{base}.other"}
        # Keys chosen at run time: t(`attention.${kind}`), setConnection(color, key), errorKeys.
        groups = {key.split(".")[0] for key in defined}
        used |= {
            literal
            for literal in re.findall(r"'([a-z]+\.[a-zA-Z_]+)'", script)
            if literal.split(".")[0] in groups
        }
        used |= {"attention.question", "attention.review"}
        used -= plural_bases  # Only their .one / .other forms exist.
        self.assertEqual(sorted(used - defined), [])
        # Every execution, acceptance and review state the runner can emit has a label.
        for state in (
            "starting", "running", "needs_input", "cancelling", "cancelled", "failed",
            "lost", "timed_out", "scope_violated", "succeeded", "awaiting_review",
            "repair_required", "rejected", "accepted",
        ):
            self.assertIn(f"state.{state}", defined, state)
        for review in ("required", "not_ready", "accepted", "repair_required", "rejected"):
            self.assertIn(f"review.{review}", defined, review)
        # Lifecycle events written by the runner and by workers.
        for event in (
            "job_created", "agent_started", "agent_finished", "worker_progress",
            "question_asked", "question_answered", "question_expired",
            "feedback_requested", "review_recorded", "cancellation_requested",
            "scope_violation", "timeout_reached", "terra_fallback_started",
        ):
            self.assertIn(f"event.{event}", defined, event)
        self.assertIn("state.terra_fallback_started", defined)

    def test_canvas_only_animates_compositor_properties(self):
        styles = (SCRIPTS.parent / "web" / "styles.css").read_text()
        self.assertIn("prefers-reduced-motion: reduce", styles)
        self.assertNotIn("transition: all", styles)
        self.assertNotIn("transition:all", styles)
        # Project rule: animate only transform/opacity, 200-500 ms, never linear.
        for declaration in re.findall(r"(?<![-\w])(?:transition|animation):\s*([^;]+);", styles):
            for part in declaration.split(","):
                words = part.split()
                self.assertNotIn("linear", words, declaration)
                durations = [w for w in words if re.fullmatch(r"\d+ms", w)]
                for duration in durations:
                    self.assertTrue(200 <= int(duration[:-2]) <= 500, declaration)
                if "transition" in part or words[0] in {"transform", "opacity"}:
                    self.assertIn(words[0], {"transform", "opacity"}, declaration)

    def test_authentication_host_and_origin_boundary(self):
        self.assertEqual(self.request("/api/overview", authenticated=False)[0], 401)
        self.assertEqual(
            self.request("/api/overview", headers={"X-Orchestrator-Token": "wrong"})[0],
            401,
        )
        self.assertEqual(
            self.request("/api/overview", headers={"X-Orchestrator-Token": "\u00e9"})[
                0
            ],
            401,
        )
        for header in (
            {"Host": "evil.test"},
            {"Origin": "https://evil.test"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            self.assertEqual(self.request("/api/overview", headers=header)[0], 403)
        self.assertEqual(
            self.request("/api/overview", headers={"Origin": self.server.origin})[0],
            200,
        )
        self.assertEqual(
            self.request(
                self.answer_url, "POST", {"answer": "yes"}, authenticated=False
            )[0],
            401,
        )
        self.assertEqual(
            jobs.pending_questions(jobs.job_dir("alpha"))[0]["state"], "pending"
        )

    def test_mutation_body_validation(self):
        cases = [
            ({}, 400),
            ({"answer": ""}, 400),
            ({"answer": "   "}, 400),
            ({"answer": 3}, 400),
            ({"answer": "yes", "workspace": "/tmp"}, 400),
            ([], 400),
        ]
        for body, expected in cases:
            self.assertEqual(
                self.request(self.answer_url, "POST", body)[0], expected, body
            )
        for body in ("{", '{"answer":"yes","answer":"no"}', b"\xff"):
            self.assertEqual(
                self.request(
                    self.answer_url, "POST", body, {"Content-Type": "application/json"}
                )[0],
                400,
            )
        self.assertEqual(
            self.request(self.answer_url, "POST", "{}", {"Content-Type": "text/plain"})[
                0
            ],
            415,
        )
        self.assertEqual(
            self.request(self.answer_url, "POST", {"answer": "x" * 65536})[0], 413
        )
        self.assertEqual(
            self.request(
                self.answer_url,
                "POST",
                "{}",
                {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            )[0],
            400,
        )
        self.assertEqual(
            jobs.pending_questions(jobs.job_dir("alpha"))[0]["state"], "pending"
        )

    def test_worker_answer_is_durable_audited_and_exactly_scoped(self):
        self.assertEqual(
            self.request(self.answer_url, "POST", {"answer": "Use strict mode."})[0],
            200,
        )
        question = jobs.read_questions(jobs.job_dir("alpha"))[0]
        self.assertEqual(question["answer"], "Use strict mode.")
        self.assertTrue(question["answered_at"])
        self.assertEqual(len(jobs.pending_questions(jobs.job_dir("beta"))), 1)
        events = jobs.read_events(jobs.job_dir("alpha"))
        answer_events = [e for e in events if e["type"] == "question_answered"]
        self.assertEqual(len(answer_events), 1)
        self.assertEqual(answer_events[0]["source"], "runner")
        self.assertEqual(
            self.request(self.answer_url, "POST", {"answer": "again"})[0], 409
        )
        self.assertEqual(
            jobs.read_questions(jobs.job_dir("alpha"))[0]["answer"], "Use strict mode."
        )

    def test_feedback_answer_and_selected_project_history(self):
        self.assertEqual(
            self.request(
                self.feedback_url, "POST", {"answer": "Keep the scope narrow."}
            )[0],
            200,
        )
        self.assertEqual(
            self.request(self.feedback_url, "POST", {"answer": "again"})[0], 409
        )
        project = json.loads(self.request(f"/api/projects/{self.feedback_pid}")[2])
        self.assertEqual(project["pending_feedback"], [])
        self.assertEqual(
            project["feedback_history"][0]["answer"], "Keep the scope narrow."
        )
        self.assertNotIn("workspace", project)
        self.assertNotIn(str(self.root), json.dumps(project))
        events = jobs.read_events(jobs.feedback_dir("feedback-a"))
        self.assertEqual(
            [e["type"] for e in events], ["feedback_requested", "feedback_answered"]
        )
        self.assertEqual(len(jobs.pending_questions(jobs.job_dir("alpha"))), 1)

    def test_wrong_project_unknown_and_invalid_ids_cannot_mutate(self):
        invalid = [
            self.answer_url.replace(self.pid, self.other_pid),
            self.answer_url.replace("question-a", "question-b"),
            self.answer_url.replace("/alpha/", "/missing/"),
            self.answer_url.replace("question-a", "%2e%2e"),
            self.feedback_url.replace(self.feedback_pid, self.pid),
            self.feedback_url.replace("feedback-a", "missing"),
        ]
        for route in invalid:
            self.assertEqual(
                self.request(route, "POST", {"answer": "yes"})[0], 404, route
            )
        self.assertEqual(
            self.request(f"/api/projects/{self.other_pid}/jobs/alpha")[0], 404
        )
        self.assertEqual(self.request("/api/projects/missing")[0], 404)
        self.assertEqual(len(jobs.pending_questions(jobs.job_dir("alpha"))), 1)
        self.assertEqual(jobs.read_feedback()[0]["state"], "pending")
        jobs.write_json(
            jobs.plans_dir() / "linked" / "plan.json",
            {
                "plan_id": "linked",
                "workspace": str(self.workspaces[0]),
                "items": [{"id": "item-001"}],
            },
        )
        with self.assertRaises(jobs.RunnerError):
            jobs.create_feedback(
                self.workspaces[1], "Wrong project", plan_id="linked", notify=False
            )
        with self.assertRaises(jobs.RunnerError):
            jobs.create_feedback(
                self.workspaces[0],
                "Wrong item",
                plan_id="linked",
                checklist_item="missing",
                notify=False,
            )
        linked = jobs.create_feedback(
            self.workspaces[0],
            "Valid linkage",
            plan_id="linked",
            checklist_item="item-001",
            notify=False,
        )
        self.assertEqual(linked["checklist_item"], "item-001")

    def test_concurrent_cli_helper_and_http_answer_only_one_wins(self):
        barrier = threading.Barrier(2)

        def local():
            barrier.wait()
            try:
                jobs.answer_worker_question("alpha", "question-a", "local")
                return True
            except jobs.RunnerError:
                return False

        def remote():
            barrier.wait()
            return (
                self.request(self.answer_url, "POST", {"answer": "browser"})[0] == 200
            )

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(local), pool.submit(remote)
            self.assertEqual(sum([a.result(), b.result()]), 1)
        self.assertEqual(
            len(
                [
                    e
                    for e in jobs.read_events(jobs.job_dir("alpha"))
                    if e["type"] == "question_answered"
                ]
            ),
            1,
        )

    def test_legacy_questions_and_symlink_records(self):
        url = f"/api/projects/{self.other_pid}/jobs/beta/questions/question-b/answer"
        self.assertEqual(
            self.request(url, "POST", {"answer": "Legacy still works"})[0], 200
        )
        directory = jobs.job_dir("alpha") / "channel" / "questions"
        (directory / "linked.json").symlink_to(
            jobs.feedback_dir("feedback-a") / "feedback.json"
        )
        self.assertEqual(len(jobs.read_questions(jobs.job_dir("alpha"))), 1)
        self.assertEqual(
            self.request(
                self.answer_url.replace("question-a", "linked"),
                "POST",
                {"answer": "bad"},
            )[0],
            404,
        )
        self.assertEqual(jobs.read_feedback()[0]["state"], "pending")

    def test_attention_active_recent_sorting_and_canonical_workspace_identity(self):
        self.job("same-workspace", self.workspaces[0] / ".." / "Toki OS")
        overview = self.server.state.overview()
        project = next(p for p in overview["projects"] if p["id"] == self.pid)
        self.assertEqual(len(project["jobs"]), 2)
        self.assertEqual(project["jobs"][0]["job_id"], "alpha")
        self.assertEqual(overview["projects"][-1]["name"], "Plan only")

    def test_unreadable_records_are_skipped(self):
        bad = jobs.job_dir("bad", must_exist=False)
        bad.mkdir()
        (bad / "meta.json").write_text("{")
        overview = json.loads(self.request("/api/overview")[2])
        self.assertEqual(len(overview["projects"]), 4)
        self.assertEqual(overview["unreadable_records"], 1)

    def test_server_command_reports_ephemeral_loopback_url(self):
        process = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "cli_agent_job.py"),
                "dashboard-web",
                "--no-open",
                "--port",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = process.stdout.readline()
            self.assertRegex(
                line,
                r"Agent Orchestrator dashboard: http://127\.0\.0\.1:[1-9][0-9]*/#token=[A-Za-z0-9_-]+",
            )
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
