"""Real engine and HTTP supervision; fake provider runs a local deterministic check only."""
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from test_panel_actions import ActionBase, jobs, dashboard
import supervision

CHECK = 'python3 -c "assert 2 + 2 == 4"'


class SupervisionTests(ActionBase):
    def setUp(self):
        super().setUp()
        jobs.answer_feedback('seed', 'Continuar con la prueba.')
        for args in (['init'], ['config', 'user.email', 'test@example.invalid'], ['config', 'user.name', 'Test']):
            subprocess.run(['git', '-C', str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / 'app.txt').write_text('baseline\n')
        subprocess.run(['git', '-C', str(self.workspace), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-m', 'baseline'], check=True, capture_output=True)
        self.reply_file = self.workspace.parent / 'supervisor-reply.json'
        self.env_reply = patch.dict(os.environ, {'AO_TEST_SUPERVISOR_REPLY': str(self.reply_file)})
        self.env_reply.start(); self.addCleanup(self.env_reply.stop)
        self.reply()
        fake = self.workspace.parent / 'bin' / 'claude'
        fake.write_text('''#!/usr/bin/env python3
import json, sys, os, subprocess
from pathlib import Path
packet = sys.stdin.read()
if 'SUPERVISOR_INPUT=' in packet:
    data = json.loads(Path(os.environ['AO_TEST_SUPERVISOR_REPLY']).read_text())
    command = 'python3 -c "assert 2 + 2 == 4"'
    if data.pop('emit_check', True):
        proc = subprocess.run(['python3', '-c', 'assert 2 + 2 == 4'])
        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'check-1','name':'Bash','input':{'command':command}}]}}))
        print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'check-1','is_error':proc.returncode != 0,'content':'exit '+str(proc.returncode)}]}}))
    print(json.dumps({'type':'result','result':json.dumps(data)}))
else:
    print(json.dumps({'type':'result','result':'Entrega del agente: tarea finalizada.'}))
''')
        code, created = self.make_plan(checklist=['Tarea supervisada'])
        self.assertEqual(code, 200, created)
        self.plan_id = created['plan_id']; self.item_id = created['items'][0]['id']
        path, plan = jobs.read_plan(self.plan_id)
        plan['coordinator'] = {'executor': 'claude-code=sonnet', 'access_profile': None}
        jobs.save_plan(path, plan)
        self.enable()

    def reply(self, **changes):
        self.reply_file.write_text(json.dumps({'action': 'accepted', 'summary': 'Implementación revisada.',
                                              'reason': 'Se inspeccionó la entrega y se ejecutó la comprobación.', 'checks': [CHECK], **changes}))

    def enable(self, enabled=True, auto_repair=False):
        code, result = self.post(f'/api/projects/{self.pid}/plans/{self.plan_id}/supervision', {'enabled': enabled, 'confirm': True, 'auto_repair': auto_repair})
        self.assertEqual(code, 200, result)
        return result

    def worker(self, **changes):
        code, result = self.post(f'/api/projects/{self.pid}/plans/{self.plan_id}/items/{self.item_id}/launch', self.launch_body(**changes))
        self.assertEqual(code, 200, result)
        self.wait_finished(result['job_id'])
        return result['job_id']

    def process(self):
        supervision.tick(self.plan_id)
        state = supervision.read_state(self.plan_id)
        for op in state['operations'].values():
            if op['state'] == 'running':
                self.wait_finished(op['job_id'])
        supervision.tick(self.plan_id)
        return supervision.read_state(self.plan_id)

    def question(self, worker, text):
        path = jobs.job_dir(worker)
        jobs.write_json(path / 'result.json', {'state': 'running'})
        question = {'id': 'q-technical', 'state': 'pending', 'question': text, 'created_at': jobs.utc_now()}
        jobs.write_json(jobs.channel_dir(path) / 'questions' / 'q-technical.json', question)
        return question

    def test_review_accepts_only_independent_check_and_persists_final_report(self):
        worker = self.worker(); state = self.process()
        review = jobs.get_status(jobs.job_dir(worker))['review']
        self.assertEqual(review['verdict'], 'accepted')
        self.assertTrue(review['reviewer'].startswith('orchestrator:supervisor-'))
        self.assertEqual(review['tests'], [CHECK])
        self.assertEqual(state['report']['state'], 'complete')
        self.assertIn('Entrega del agente', state['report']['tasks'][0]['delivery'])
        self.assertEqual(state['report']['tasks'][0]['review'], 'Implementación revisada.\nSe inspeccionó la entrega y se ejecutó la comprobación.')
        before = len(list(jobs.jobs_dir().glob('*/meta.json')))
        supervision.tick(self.plan_id)
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), before)
        detail = self.request(f'/api/projects/{self.pid}/plans/{self.plan_id}')[1]
        self.assertEqual(detail['supervision']['report']['state'], 'complete')

    def test_claimed_checks_without_execution_escalate_once_without_acceptance(self):
        worker = self.worker(); self.reply(emit_check=False)
        state = self.process()
        self.assertIsNone(jobs.get_status(jobs.job_dir(worker))['review'])
        self.assertEqual(state['report']['state'], 'needs_user')
        count = len(jobs.read_feedback())
        supervision.tick(self.plan_id)
        self.assertEqual(len(jobs.read_feedback()), count)
        self.assertEqual(len(state['operations']), 1)

    def test_extra_claimed_check_not_run_by_reviewer_blocks_acceptance(self):
        worker = self.worker(); self.reply(checks=[CHECK, 'python3 -c "print(1)"'])
        state = self.process()
        self.assertIsNone(jobs.get_status(jobs.job_dir(worker))['review'])
        self.assertEqual(state['report']['state'], 'needs_user')

    def test_deleting_a_plan_stops_its_supervision(self):
        self.worker()
        code, result = self.post(f'/api/projects/{self.pid}/plans/{self.plan_id}/delete', {'confirm': True})
        self.assertEqual(code, 200, result)
        state = supervision.read_state(self.plan_id)
        self.assertFalse(state['enabled'])
        self.assertFalse(state['auto_advance'])
        before = len(list(jobs.jobs_dir().glob('*/meta.json')))
        supervision.tick(self.plan_id)
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), before)

    def test_technical_question_is_answered_by_coordinator_with_attribution(self):
        worker = self.worker(); self.question(worker, '¿Qué nombre debe tener la función auxiliar?')
        self.reply(action='answer', answer='Usa normalize_input, siguiendo la convención del módulo.')
        overview = self.request('/api/overview')[1]
        project = next(p for p in overview['projects'] if p['id'] == self.pid)
        self.assertEqual(project['question_count'], 0)
        self.process()
        question = jobs.read_questions(jobs.job_dir(worker))[0]
        self.assertEqual(question['state'], 'answered')
        self.assertTrue(question['answered_by'].startswith('orchestrator:'))
        self.assertIn('normalize_input', question['answer'])

    def test_sensitive_question_stays_for_user_without_another_model_call(self):
        worker = self.worker(); self.question(worker, '¿Puedo desplegar en producción y ampliar los permisos?')
        state = self.process()
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), 1)
        self.assertEqual(next(iter(state['operations'].values()))['state'], 'needs_user')
        self.assertEqual(jobs.pending_questions(jobs.job_dir(worker))[0]['state'], 'pending')
        self.assertEqual(self.request(f'/api/projects/{self.pid}')[1]['question_count'], 1)

    def test_human_answer_is_not_overwritten_by_late_coordinator(self):
        worker = self.worker(); self.question(worker, '¿Qué nombre debe tener la función auxiliar?')
        self.reply(action='answer', answer='Usa normalize_input.')
        supervision.tick(self.plan_id)
        op = next(iter(supervision.read_state(self.plan_id)['operations'].values()))
        self.wait_finished(op['job_id'])
        jobs.answer_worker_question(worker, 'q-technical', 'Usa el nombre elegido por mí.')
        supervision.tick(self.plan_id)
        self.assertEqual(jobs.read_questions(jobs.job_dir(worker))[0]['answer'], 'Usa el nombre elegido por mí.')
        self.assertEqual(next(iter(supervision.read_state(self.plan_id)['operations'].values()))['state'], 'superseded')

    def test_workspace_changes_block_acceptance_even_when_model_says_accepted(self):
        worker = self.worker(); supervision.tick(self.plan_id)
        op = next(iter(supervision.read_state(self.plan_id)['operations'].values()))
        self.wait_finished(op['job_id'])
        (self.workspace / 'app.txt').write_text('changed after snapshot\n')
        supervision.tick(self.plan_id)
        self.assertIsNone(jobs.get_status(jobs.job_dir(worker))['review'])
        self.assertEqual(supervision.read_state(self.plan_id)['report']['state'], 'needs_user')

    def test_repairs_keep_original_pool_scope_and_stop_at_three_attempts(self):
        self.enable(auto_repair=True)
        self.reply(action='repair_required', repair='Corrige únicamente la validación dentro del alcance original.')
        self.worker(allow_paths=['app.txt'], deny_paths=['secrets/*'], max_changed_files=1)
        for attempt in range(3):
            state = self.process()
            _, plan = jobs.read_plan(self.plan_id)
            last = plan['items'][0]['job_ids'][-1]
            self.wait_finished(last)
        self.assertEqual(len(plan['items'][0]['job_ids']), 3)
        self.assertEqual(state['report']['state'], 'needs_user')
        for job in plan['items'][0]['job_ids']:
            meta = jobs.read_json(jobs.job_dir(job) / 'meta.json')
            self.assertEqual(meta['model'], 'sonnet')
            self.assertEqual(meta['scope']['allowed_paths'], ['app.txt'])
            self.assertEqual(meta['scope']['denied_paths'], ['secrets/*'])
            self.assertEqual(meta['scope']['max_changed_files'], 1)

    def test_pause_and_archive_prevent_supervision_calls(self):
        self.worker(); self.enable(False)
        supervision.tick(self.plan_id)
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), 1)
        self.enable()
        path = dashboard.project_registry_path()
        jobs.write_json(path, {self.pid: {'name': 'Proyecto', 'workspace': str(self.workspace), 'archived': True}})
        supervision.tick(self.plan_id)
        self.assertEqual(len(list(jobs.jobs_dir().glob('*/meta.json'))), 1)

    def test_control_requires_authorization_and_project_ownership(self):
        body = {'enabled': True, 'confirm': True}
        route = f'/api/projects/{self.pid}/plans/{self.plan_id}/supervision'
        self.assertEqual(self.post(route, body, authenticated=False)[0], 401)
        self.assertEqual(self.post(route.replace(self.pid, self.other_pid), body)[0], 404)
        self.assertEqual(self.post(route, {**body, 'confirm': False})[0], 400)

    def test_reviewer_can_be_selected_for_a_manual_plan_and_survives_pause(self):
        path, plan = jobs.read_plan(self.plan_id)
        plan.pop('coordinator', None); jobs.save_plan(path, plan)
        body = {'enabled': True, 'confirm': True, 'reviewer': 'claude-code=sonnet'}
        route = f'/api/projects/{self.pid}/plans/{self.plan_id}/supervision'
        self.assertEqual(self.post(route, body)[0], 200)
        self.enable(False)
        self.assertEqual(supervision.read_state(self.plan_id)['coordinator']['executor'], 'claude-code=sonnet')

    def test_recorded_review_recovers_before_repair_without_duplicate_launch(self):
        self.enable(auto_repair=True)
        self.reply(action='repair_required', repair='Corrige la validación.')
        worker = self.worker(); supervision.tick(self.plan_id)
        state = supervision.read_state(self.plan_id); op = next(iter(state['operations'].values()))
        self.wait_finished(op['job_id'])
        import panel_actions
        with panel_actions.private_text_file('Corrección necesaria.') as notes:
            panel_actions.run_engine(['record-review', worker, '--verdict=repair_required', '--reviewer=orchestrator:' + op['job_id'], '--notes-file=' + str(notes), '--json'])
        supervision.tick(self.plan_id)
        after = supervision.read_state(self.plan_id); repaired = next(iter(after['operations'].values()))
        self.assertEqual(repaired['state'], 'repair_started')
        self.wait_finished(repaired['repair_job_id'])
        # Simulate a crash after repair launch but before marking its operation complete.
        after['operations'][op['key']]['state'] = 'running'
        jobs.write_json(supervision.state_path(self.plan_id), after)
        with patch.object(supervision, 'launch_decision') as launch:
            supervision.tick(self.plan_id)
            # The new repair can itself need review, but the original event is not relaunched.
            self.assertFalse(any(call.args[2]['target'] == worker for call in launch.call_args_list))
        plan = jobs.read_plan(self.plan_id)[1]
        self.assertEqual(len(plan['items'][0]['job_ids']), 2)

    def test_human_review_resolves_escalation_without_stale_attention(self):
        worker = self.worker(); self.reply(emit_check=False)
        state = self.process()
        feedback_id = next(iter(state['operations'].values()))['feedback_id']
        self.assertEqual(jobs.read_json(jobs.feedback_dir(feedback_id) / 'feedback.json')['state'], 'pending')
        code, reviewed = self.post(f'/api/projects/{self.pid}/jobs/{worker}/review', {'verdict': 'accepted', 'notes': 'Revisión humana independiente.', 'tests': ['Comprobación local ejecutada por el usuario.']})
        self.assertEqual(code, 200, reviewed)
        supervision.tick(self.plan_id)
        state = supervision.read_state(self.plan_id)
        self.assertEqual(state['report']['needs_user'], [])
        self.assertEqual(jobs.read_json(jobs.feedback_dir(feedback_id) / 'feedback.json')['answered_by'], 'orchestrator:supervision')
