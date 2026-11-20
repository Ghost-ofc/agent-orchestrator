"""Explicit coordinator choice and generated contracts, with fake CLIs and no infrastructure access."""
import json
from test_panel_actions import ActionBase, jobs, CONTRACT
from test_project_access import FLAGS


class ContractTests(ActionBase):
    def setUp(self):
        super().setUp()
        code, plan = self.make_plan(checklist=['Preparar el subdominio'], goal='Definir feature.example.invalid sin inventar el destino DNS.')
        self.assertEqual(code, 200, plan)
        self.plan_id = plan['plan_id']; self.item_id = plan['items'][0]['id']
        self.base = f'/api/projects/{self.pid}/plans/{self.plan_id}'
        self.route = self.base + f'/items/{self.item_id}/contract'
        self.reply()

    def reply(self, value=None, plain=False, fenced=False):
        if value is None:
            value = {'task': CONTRACT, 'assigned_executor': 'claude-code=sonnet'}
        response = json.dumps(value) if plain else json.dumps({'type': 'result', 'result': json.dumps(value)})
        if fenced:
            response = '```json\n' + response + '\n```'
        fake = self.workspace.parent / 'bin/claude'
        fake.write_text('#!/usr/bin/env python3\nimport sys\n'
                        f'if "--help" in sys.argv:\n print({FLAGS!r})\n sys.exit(0)\n'
                        'sys.stdin.read()\n' + 'print(' + repr(response) + ')\n')

    def plan(self):
        return self.request(self.base)[1]

    def configure(self, coordinator='claude-code=sonnet', **changes):
        return self.post(self.base + '/coordinator', {'revision': self.plan()['edit_revision'], 'coordinator': coordinator, 'confirm': True, **changes})

    def start(self, **changes):
        return self.post(self.route, {'confirm': True, 'objective': 'Preparar feature.example.invalid; descubrir el destino antes de crear un registro.', **changes})

    def ready(self):
        self.assertEqual(self.configure()[0], 200)
        code, result = self.start()
        self.assertEqual(code, 200, result)
        self.wait_finished(result['job_id'])
        return self.request(self.route)[1], result['job_id']

    def test_coordinator_is_explicit_persistent_visible_and_does_not_launch_a_job(self):
        before = jobs.read_plan(self.plan_id)[1]['executor_policy']
        self.assertIsNone(self.plan()['coordinator'])
        code, view = self.configure()
        self.assertEqual(code, 200, view)
        self.assertEqual(view['coordinator']['executor'], 'claude-code=sonnet')
        self.assertEqual(jobs.read_plan(self.plan_id)[1]['executor_policy'], before)
        self.assertFalse(list(jobs.jobs_dir().glob('*/meta.json')))
        project = next(p for p in self.request('/api/overview')[1]['projects'] if p['id'] == self.pid)
        self.assertEqual(project['plans'][0]['coordinator']['executor'], 'claude-code=sonnet')

    def test_generation_requires_coordinator_and_deliberate_action(self):
        self.assertEqual(self.start()[0], 400)
        self.assertEqual(self.configure(confirm=False)[0], 400)
        self.assertEqual(self.configure(coordinator='claude-code')[0], 422)
        self.assertFalse(list(jobs.jobs_dir().glob('*/meta.json')))
        self.assertEqual(self.configure()[0], 200)
        self.assertEqual(self.start(confirm=False)[0], 400)

    def test_draft_is_real_planning_job_and_does_not_apply_or_execute_implementation(self):
        draft, job = self.ready()
        self.assertEqual(draft['state'], 'ready', draft)
        self.assertEqual(draft['task'], CONTRACT)
        meta = jobs.read_json(jobs.job_dir(job) / 'meta.json')
        self.assertEqual(meta['role'], 'coordinator-contract')
        self.assertIsNone(meta.get('plan_id'))
        self.assertEqual(meta['scope']['max_changed_files'], 0)
        self.assertIn('CONTRACT_INPUT=', (jobs.job_dir(job) / 'task-original.md').read_text())
        self.assertFalse(self.plan()['items'][0]['contract_ready'])
        self.assertEqual(self.plan()['items'][0]['job_ids'], [])
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), 1)

    def test_accept_saves_reviewed_contract_without_launching_executor(self):
        draft, job = self.ready()
        task = draft['task'] + '\nUna precisión revisada por el usuario.\n'
        code, plan = self.post(self.route + '/accept', {'confirm': True, 'job_id': draft['job_id'], 'task': task, 'assigned_executor': draft['assigned_executor']})
        self.assertEqual(code, 200, plan)
        self.assertEqual(plan['items'][0]['task'], task.strip())
        self.assertTrue(plan['items'][0]['contract_ready'])
        self.assertEqual(plan['items'][0]['assigned_executor'], 'claude-code=sonnet')
        self.assertEqual(plan['items'][0]['job_ids'], [])
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), 1)

    def test_stale_draft_cannot_overwrite_an_edited_plan(self):
        draft, job = self.ready()
        plan = self.plan()
        payload = {'revision': plan['edit_revision'], 'title': 'Objetivo actualizado', 'goal': plan['goal']['objective'],
                   'items': [{key: item.get(key) if key != 'task' else item.get('task') or '' for key in ('id', 'title', 'task', 'assigned_executor')} for item in plan['items']]}
        self.assertEqual(self.post(self.base + '/edit', payload)[0], 200)
        self.assertEqual(self.request(self.route)[1]['state'], 'stale')
        self.assertEqual(self.post(self.route + '/accept', {'confirm': True, 'job_id': draft['job_id'], 'task': draft['task'], 'assigned_executor': draft['assigned_executor']})[0], 400)
        self.assertFalse(self.plan()['items'][0]['contract_ready'])

    def test_incomplete_or_unauthorized_proposals_cannot_be_saved(self):
        self.assertEqual(self.configure()[0], 200)
        invalid = [{'task': 'Solo un objetivo', 'assigned_executor': 'claude-code=sonnet'},
                   {'task': CONTRACT.replace('Detalle de Why.', '<<completar>>'), 'assigned_executor': 'claude-code=sonnet'},
                   {'task': CONTRACT, 'assigned_executor': 'codex-cli=gpt-6-astra'}]
        for value in invalid:
            self.reply(value)
            code, result = self.start(); self.assertEqual(code, 200, result)
            self.wait_finished(result['job_id'])
            self.assertEqual(self.request(self.route)[1]['state'], 'invalid')
            self.assertFalse(self.plan()['items'][0]['contract_ready'])

    def test_plain_output_and_code_fenced_json_are_supported(self):
        for fenced in (False, True):
            self.reply(plain=True, fenced=fenced)
            draft, job = self.ready()
            self.assertEqual(draft['state'], 'ready', draft)

    def test_selected_coordinator_profile_is_preserved_for_drafting(self):
        (self.workspace / '.mcp.json').write_text(json.dumps({'mcpServers': {'approved': {'command': '/bin/echo', 'args': ['TEST_ONLY']}}}))
        code, policy = self.post(f'/api/projects/{self.pid}/access', {'enabled': True, 'mcp_servers': ['approved'], 'ssh_hosts': [], 'per_agent': True, 'agent_profiles': {'planner': {'mcp_servers': ['approved'], 'ssh_hosts': []}}})
        self.assertEqual(code, 200, policy)
        self.assertEqual(self.configure(access_profile='planner')[0], 200)
        code, started = self.start(); self.assertEqual(code, 200, started)
        self.wait_finished(started['job_id'])
        access = jobs.read_json(jobs.job_dir(started['job_id']) / 'meta.json')['access']
        self.assertEqual(access['profile'], 'planner')
        self.assertEqual(access['mcp_servers'], ['approved'])
        self.assertEqual(access['ssh_hosts'], [])

    def test_routes_check_authentication_project_ownership_and_task_history(self):
        body = {'revision': self.plan()['edit_revision'], 'coordinator': 'claude-code=sonnet', 'confirm': True}
        self.assertEqual(self.post(self.base + '/coordinator', body, authenticated=False)[0], 401)
        self.assertEqual(self.post(self.base.replace(self.pid, self.other_pid) + '/coordinator', body)[0], 404)
        self.assertEqual(self.request(self.route.replace(self.pid, self.other_pid))[0], 404)
        self.assertEqual(self.configure()[0], 200)
        code, worker = self.post(self.base + f'/items/{self.item_id}/launch', self.launch_body())
        self.assertEqual(code, 200, worker); self.wait_finished(worker['job_id'])
        self.assertEqual(self.start()[0], 400)

    def test_drafting_receives_plan_guardrails_and_recorded_human_decisions(self):
        jobs.create_feedback(self.workspace, '¿Qué destino se confirmó?', plan_id=self.plan_id, feedback_id='target-choice', notify=False)
        jobs.answer_feedback('target-choice', 'Usar el destino previamente validado por el equipo.')
        draft, job = self.ready()
        packet = (jobs.job_dir(job) / 'task-original.md').read_text()
        self.assertIn('Constraints and guardrails', packet)
        self.assertIn('Stay within the selected workspace.', packet)
        self.assertIn('Usar el destino previamente validado por el equipo.', packet)
        self.assertEqual(draft['state'], 'ready')

    def test_an_older_generation_cannot_replace_a_newer_draft(self):
        import contract_planning
        draft, first = self.ready()
        code, second = self.start()
        self.assertEqual(code, 200, second); self.wait_finished(second['job_id'])
        self.assertEqual(self.post(self.route + '/accept', {'confirm': True, 'job_id': first, 'task': draft['task'], 'assigned_executor': draft['assigned_executor']})[0], 409)
        changes = contract_planning.edit_body(jobs.read_plan(self.plan_id)[1])
        changes['items'][0]['task'] = CONTRACT
        changes['contract_source'] = {'item_id': self.item_id, 'job_id': first}
        self.assertEqual(self.post(self.base + '/edit', changes)[0], 409)
        self.assertFalse(self.plan()['items'][0]['contract_ready'])
