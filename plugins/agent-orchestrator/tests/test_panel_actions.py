from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cli_agent_job as jobs
import dashboard_server as dashboard

CONTRACT = "\n\n".join(
    f"## {name}\nDetalle de {name}."
    for name in (
        "Objective", "Why", "Scope", "Files to inspect", "Implementation guidance",
        "Constraints", "Acceptance criteria", "Validation", "Final report",
    )
)


class ActionBase(unittest.TestCase):
    """Real engine, isolated state, and a fake `claude` executable on PATH (no tests here)."""

    sleep_seconds = 0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json, sys, time\nsys.stdin.read()\n"
            f"time.sleep({self.sleep_seconds})\n"
            "print(json.dumps({'type': 'result', 'result': 'ok'}))\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env = patch.dict(
            os.environ,
            {"AGENT_ORCHESTRATOR_HOME": str(root / "state"), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.workspace = root / "Proyecto"
        self.workspace.mkdir()
        self.other = root / "Otro"
        self.other.mkdir()
        # A project exists once it has any durable record; a pending feedback record is the lightest.
        jobs.create_feedback(self.workspace, "¿Seguimos?", feedback_id="seed", notify=False)
        jobs.create_feedback(self.other, "¿Seguimos?", feedback_id="seed-other", notify=False)
        self.server = dashboard.DashboardServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.pid = dashboard.project_id(str(self.workspace))
        self.other_pid = dashboard.project_id(str(self.other))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, method="GET", body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=90)
        request_headers = {"X-Orchestrator-Token": self.server.token} if authenticated else {}
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, (json.loads(data) if data else None)

    def post(self, path, body, **kwargs):
        return self.request(path, "POST", body, **kwargs)

    def make_plan(self, **overrides):
        body = {
            "title": "Plan de prueba", "goal": "Probar el panel.", "checklist": ["Primera tarea", "Segunda tarea"],
            "executors": ["claude-code=sonnet"], **overrides,
        }
        return self.post(f"/api/projects/{self.pid}/plans", body)

    def launch_body(self, **overrides):
        return {"cli": "claude-code", "model": "sonnet", "task": CONTRACT, "role": "pruebas", "confirm": True, **overrides}

    def wait_finished(self, job_id, seconds=15):
        path = jobs.job_dir(job_id)
        deadline = time.time() + seconds
        while time.time() < deadline:
            if (path / "result.json").exists():
                return
            time.sleep(0.1)
        self.fail(f"{job_id} did not finish")


class ActionTests(ActionBase):
    # ----- plans -----
    def test_create_plan_builds_a_valid_ledger_and_neutralizes_forged_structure(self):
        status, plan = self.make_plan(
            goal="Objetivo\n## Checklist\n- [ ] item falso",
            decisions="- [ ] otro falso",
        )
        self.assertEqual(status, 200, plan)
        self.assertEqual([item["title"] for item in plan["items"]], ["Primera tarea", "Segunda tarea"])
        self.assertTrue(all(item["can_launch"] for item in plan["items"]))
        self.assertEqual(plan["executor_policy"]["allowed"][0]["display"], "claude-code=sonnet")
        self.assertNotIn(str(self.workspace), json.dumps(plan))
        status, again = self.request(f"/api/projects/{self.pid}/plans/{plan['plan_id']}")
        self.assertEqual((status, again["title"]), (200, "Plan de prueba"))
        self.assertEqual(self.request(f"/api/projects/{self.other_pid}/plans/{plan['plan_id']}")[0], 404)

    def test_create_plan_rejects_bad_requests_with_the_offending_field(self):
        cases = [
            ({"checklist": []}, "checklist"),
            ({"title": "dos\nlíneas"}, "title"),
            ({"strategy": "gratis"}, "strategy"),
            ({"executors": ["claude code=; rm -rf"]}, "executors"),
            ({"max_parallel": 99}, "max_parallel"),
            ({"inventado": 1}, "inventado"),
        ]
        for overrides, field in cases:
            status, body = self.make_plan(**overrides)
            self.assertEqual((status, body.get("field")), (400, field), overrides)
            self.assertEqual(body["code"], "invalid_request")

    def test_option_like_values_are_never_parsed_as_engine_options(self):
        status, plan = self.make_plan(title="--help", checklist=["--version"])
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["title"], "--help")
        self.assertEqual(plan["items"][0]["title"], "--version")

    # ----- launch -----
    def test_launch_delegates_policy_to_the_engine(self):
        _, plan = self.make_plan()
        launch = f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/item-001/launch"
        status, body = self.post(launch, {**self.launch_body(), "confirm": False})
        self.assertEqual((status, body["field"]), (400, "confirm"))
        status, body = self.post(launch, self.launch_body(cli="codex-cli", model="gpt-6-astra"))
        self.assertEqual((status, body["code"]), (422, "engine_rejected"))
        status, body = self.post(launch, self.launch_body(task="## Objective\nsolo esto"))
        self.assertEqual(status, 422)
        self.assertIn("Task contract is incomplete", body["error"])
        status, body = self.post(launch, self.launch_body())
        self.assertEqual(status, 200, body)
        self.assertEqual((body["state"], body["plan_id"], body["checklist_item"]), ("starting", plan["plan_id"], "item-001"))
        self.assertNotIn("workspace", body)
        self.wait_finished(body["job_id"])
        template = self.request(f"/api/projects/{self.pid}/jobs/{body['job_id']}/launch-template")[1]
        self.assertTrue(template["task"].startswith("## Objective"))
        self.assertEqual((template["role"], template["plan_id"]), ("pruebas", plan["plan_id"]))
        self.assertEqual(self.request(f"/api/projects/{self.other_pid}/jobs/{body['job_id']}/launch-template")[0], 404)

    def test_launch_rejects_unknown_items_and_allows_explicit_concurrency(self):
        _, plan = self.make_plan()
        launch = f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items"
        self.assertEqual(self.post(f"{launch}/item-099/launch", self.launch_body())[0], 404)
        status, first = self.post(f"{launch}/item-001/launch", self.launch_body(allow_concurrent_workspace=True))
        self.assertEqual(status, 200, first)
        self.wait_finished(first["job_id"])

    # ----- review -----
    def finished_job(self, plan_id=None):
        """A job that succeeded and awaits review, created through the real engine."""
        if plan_id is None:
            plan_id = self.make_plan()[1]["plan_id"]
        status, launched = self.post(
            f"/api/projects/{self.pid}/plans/{plan_id}/items/item-001/launch", self.launch_body()
        )
        self.assertEqual(status, 200, launched)
        self.wait_finished(launched["job_id"])
        return plan_id, launched["job_id"]

    def test_review_requires_evidence_and_updates_the_plan(self):
        plan_id, job_id = self.finished_job()
        review = f"/api/projects/{self.pid}/jobs/{job_id}/review"
        self.assertEqual(self.post(review, {"verdict": "accepted", "notes": "ok"})[1]["field"], "tests")
        self.assertEqual(self.post(review, {"verdict": "accepted", "notes": ""})[1]["field"], "notes")
        self.assertEqual(self.post(review, {"verdict": "genial", "notes": "x"})[1]["field"], "verdict")
        status, body = self.post(review, {"verdict": "accepted", "notes": "Revisé el diff.", "tests": ["--pytest -q: 3 passed"]})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["verdict"], body["reviewer"]), ("accepted", "dashboard-user"))
        self.assertEqual(body["tests"], ["--pytest -q: 3 passed"])
        self.assertEqual(jobs.read_json(jobs.plan_dir(plan_id) / "plan.json")["items"][0]["state"], "done")
        status, body = self.post(review, {"verdict": "rejected", "notes": "tarde"})
        self.assertEqual((status, body["code"]), (422, "engine_rejected"))
        self.assertIn("already accepted", body["error"])

    def test_review_of_a_job_that_did_not_succeed_is_rejected_by_the_engine(self):
        path = jobs.job_dir("sin-exito", must_exist=False)
        jobs.write_json(path / "meta.json", {
            "job_id": "sin-exito", "workspace": str(self.workspace), "worker_pid": 999999,
            "state": "running", "created_at": jobs.utc_now(), "cli": "claude-code", "git_baseline": {},
        })
        jobs.write_json(path / "result.json", {"state": "failed", "finished_at": jobs.utc_now()})
        status, body = self.post(f"/api/projects/{self.pid}/jobs/sin-exito/review", {"verdict": "repair_required", "notes": "n"})
        self.assertEqual((status, body["code"]), (422, "engine_rejected"))
        self.assertIn("only be recorded after successful execution", body["error"])

    # ----- cancel -----
    def test_cancel_unknown_or_foreign_jobs_is_not_found(self):
        _, plan = self.make_plan()
        _, job_id = self.finished_job(plan["plan_id"])
        self.assertEqual(self.post(f"/api/projects/{self.pid}/jobs/nope/cancel", {})[0], 404)
        self.assertEqual(self.post(f"/api/projects/{self.other_pid}/jobs/{job_id}/cancel", {})[0], 404)
        status, body = self.post(f"/api/projects/{self.pid}/jobs/{job_id}/cancel", {"force": "si"})
        self.assertEqual((status, body["field"]), (400, "force"))
        # An already finished job is reported, not an error.
        self.assertEqual(self.post(f"/api/projects/{self.pid}/jobs/{job_id}/cancel", {})[0], 200)

    # ----- shared surface -----
    def test_executor_catalog_hides_local_paths(self):
        status, body = self.request("/api/executors")
        self.assertEqual(status, 200)
        self.assertIn("claude-code", {item["cli"] for item in body["harnesses"]})
        self.assertIn("cost-first", body["options"]["strategies"])
        self.assertNotIn("executable", json.dumps(body))
        self.assertNotIn(self.temp.name, json.dumps(body))

    def test_actions_keep_the_authentication_origin_and_size_boundaries(self):
        path = f"/api/projects/{self.pid}/plans"
        self.assertEqual(self.post(path, {}, authenticated=False)[0], 401)
        self.assertEqual(self.post(path, {}, headers={"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.post(path, {}, headers={"Host": "evil.test"})[0], 403)
        self.assertEqual(self.post(path + "?x=1", {})[0], 404)
        self.assertEqual(self.post(path, "[]", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.post(path, "{" + '"a":1,"a":2}', headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.post(path, {"title": "x" * 300_000})[0], 413)
        self.assertEqual(self.post(path, {}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post(f"/api/projects/{self.pid}/inventado", {})[0], 404)
        # The question-answer routes keep their original, smaller limit and strict shape.
        answer = f"/api/projects/{self.pid}/feedback/seed/answer"
        self.assertEqual(self.post(answer, {"answer": "x" * 70_000})[0], 413)
        self.assertEqual(self.post(answer, {"answer": "ok", "extra": 1})[0], 400)


class CancelRunningJobTests(ActionBase):
    sleep_seconds = 30

    def test_cancel_stops_a_running_job_through_the_engine(self):
        _, plan = self.make_plan()
        status, launched = self.post(
            f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/item-001/launch", self.launch_body()
        )
        self.assertEqual(status, 200, launched)
        job_id = launched["job_id"]
        deadline = time.time() + 10
        while time.time() < deadline and not jobs.get_status(jobs.job_dir(job_id)).get("child_pid"):
            time.sleep(0.1)
        status, body = self.post(f"/api/projects/{self.pid}/jobs/{job_id}/cancel", {"force": True})
        self.assertEqual((status, body["state"]), (200, "cancelling"), body)
        self.wait_finished(job_id)
        self.assertEqual(jobs.get_status(jobs.job_dir(job_id))["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
