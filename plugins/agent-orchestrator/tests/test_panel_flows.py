"""Browser-led planning against the real HTTP boundary and engine, using a fake agent."""
import json
from unittest.mock import patch

from test_panel_actions import ActionBase, jobs, dashboard


class FlowTests(ActionBase):
    def setUp(self):
        super().setUp()
        self.task = {"title": "Añadir validación", "objective": "Validar el formulario",
                     "scope": "src/form.py", "acceptance": "Los valores vacíos se rechazan",
                     "validation": "Ejecutar unittest", "executor": "claude-code=sonnet"}
        self.reply({"tasks": [self.task]})

    def reply(self, value):
        fake = self.workspace.parent / "bin" / "claude"
        fake.write_text("#!/usr/bin/env python3\nimport json, sys\nsys.stdin.read()\nprint(" +
                        repr(json.dumps({"type": "result", "result": json.dumps(value)})) + ")\n")

    def start(self, **overrides):
        return self.post(f"/api/projects/{self.pid}/flows", {
            "title": "Validación", "goal": "Mejorar el formulario", "coordinator": "claude-code=sonnet",
            "executors": ["claude-code=sonnet"], "confirm": True, **overrides})

    def proposal(self, job):
        return self.request(f"/api/projects/{self.pid}/jobs/{job}/flow")

    def accept(self, job, **overrides):
        return self.post(f"/api/projects/{self.pid}/jobs/{job}/flow/accept", {
            "confirm": True, "assignments": ["claude-code=sonnet"], **overrides})

    def test_coordinator_proposal_becomes_a_launchable_plan_and_is_idempotent(self):
        code, started = self.start()
        self.assertEqual(code, 200, started)
        job = started["job_id"]
        self.wait_finished(job)
        code, proposal = self.proposal(job)
        self.assertEqual((code, proposal["tasks"]), (200, [self.task]))
        code, plan = self.accept(job)
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan["coordinator"]["executor"], "claude-code=sonnet")
        item = plan["items"][0]
        self.assertEqual(item["assigned_executor"], "claude-code=sonnet")
        self.assertFalse(jobs.validate_task_packet(item["task"]))
        self.assertEqual(self.accept(job)[1]["plan_id"], plan["plan_id"])
        self.assertEqual(len(list(jobs.plans_dir().glob("*/plan.json"))), 1)
        code, launched = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{item['id']}/launch",
                                   self.launch_body(task=item["task"]))
        self.assertEqual(code, 200, launched)
        self.wait_finished(launched["job_id"])

    def test_foreign_project_cannot_read_or_accept_a_proposal(self):
        code, started = self.start()
        self.assertEqual(code, 200, started)
        job = started["job_id"]
        self.wait_finished(job)
        self.assertEqual(self.request(f"/api/projects/{self.other_pid}/jobs/{job}/flow")[0], 404)
        self.assertEqual(self.post(f"/api/projects/{self.other_pid}/jobs/{job}/flow/accept",
                                   {"confirm": True, "assignments": ["claude-code=sonnet"]})[0], 404)

    def test_unapproved_or_malformed_agent_proposals_never_create_a_plan(self):
        for value in ({"tasks": [{**self.task, "executor": "codex-cli=gpt-6-astra"}]},
                      {"tasks": ["invalid"]}, {"tasks": [{**self.task, "scope": ""}]}):
            self.reply(value)
            code, started = self.start()
            self.assertEqual(code, 200, started)
            job = started["job_id"]
            self.wait_finished(job)
            self.assertIn("error", self.proposal(job)[1])
            self.assertEqual(self.accept(job)[0], 400)
        self.assertFalse(list(jobs.plans_dir().glob("*/plan.json")))

    def test_acceptance_cannot_widen_the_pool_or_omit_confirmation(self):
        code, started = self.start()
        self.assertEqual(code, 200, started)
        job = started["job_id"]
        self.wait_finished(job)
        self.assertEqual(self.accept(job, assignments=["claude-code=opus"])[0], 400)
        self.assertEqual(self.accept(job, confirm=False)[0], 400)
        self.assertFalse(list(jobs.plans_dir().glob("*/plan.json")))

    def test_automatic_pool_is_locked_before_coordinator_starts(self):
        pool = jobs.executor_policy(["claude-code=sonnet"], [])
        with patch.object(dashboard.panel_actions.jobs, "automatic_executor_policy", return_value=pool):
            code, started = self.start(executors=[])
        self.assertEqual(code, 200, started)
        job = started["job_id"]
        self.wait_finished(job)
        self.assertEqual(self.proposal(job)[1]["pool"], ["claude-code=sonnet"])
        self.assertEqual(self.accept(job)[0], 200)

    def test_start_requires_explicit_confirmation_and_valid_coordinator(self):
        for overrides, field in (({"confirm": False}, "confirm"), ({"coordinator": "foo; echo hi"}, "coordinator"),
                                 ({"allow_dirty": "yes"}, "allow_dirty")):
            code, result = self.start(**overrides)
            self.assertEqual((code, result.get("field")), (400, field))
        self.assertFalse(list(jobs.jobs_dir().glob("*/meta.json")))

    def test_explicit_task_pin_overrides_the_coordinators_suggestion(self):
        self.reply({"tasks": [{**self.task, "executor": "claude-code=haiku"}]})
        code, started = self.start(executors=["claude-code=sonnet", "claude-code=haiku"],
                                   tasks=["claude-code=sonnet | Validar el formulario"])
        self.assertEqual(code, 200, started)
        self.wait_finished(started["job_id"])
        self.assertEqual(self.proposal(started["job_id"])[1]["tasks"][0]["executor"], "claude-code=sonnet")

    def test_task_pin_cannot_expand_an_explicit_pool(self):
        code, result = self.start(tasks=["claude-code=haiku | Validar formulario"])
        self.assertEqual((code, result.get("field")), (400, "tasks"))
        self.assertFalse(list(jobs.jobs_dir().glob("*/meta.json")))

    def test_plain_output_clis_can_return_compact_or_pretty_json(self):
        for indent in (None, 2):
            raw = json.dumps({"tasks": [self.task]}, indent=indent)
            fake = self.workspace.parent / "bin" / "claude"
            fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint(" + repr(raw) + ")\n")
            code, started = self.start()
            self.assertEqual(code, 200, started)
            self.wait_finished(started["job_id"])
            self.assertEqual(self.proposal(started["job_id"])[1]["tasks"], [self.task])
