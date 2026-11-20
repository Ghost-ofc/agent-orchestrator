"""Plan editing over HTTP and the actual engine, with isolated state and no desktop notifications."""
import concurrent.futures
import copy
import hashlib
import json

from test_panel_actions import ActionBase, jobs, dashboard, CONTRACT


class PlanEditTests(ActionBase):
    def setUp(self):
        super().setUp()
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        self.plan_id = plan['plan_id']
        self.route = f'/api/projects/{self.pid}/plans/{self.plan_id}'

    def payload(self):
        code, view = self.request(self.route)
        self.assertEqual(code, 200, view)
        return {'revision': view['edit_revision'], 'title': view['title'], 'goal': view['goal']['objective'],
                'items': [{key: item.get(key) if key not in {'task'} else item.get(key) or '' for key in ('id', 'title', 'task', 'assigned_executor')} for item in view['items']]}

    def save(self, body):
        return self.post(self.route + '/edit', body)

    def test_edit_pending_work_preserves_ids_pool_and_versioned_history(self):
        path, before = jobs.read_plan(self.plan_id)
        old_document = (path / 'plan.md').read_text()
        body = self.payload(); original_id = body['items'][0]['id']
        body.update(title='Plan actualizado', goal='Un objetivo más preciso.')
        body['items'][0].update(title='Tarea afinada', task=CONTRACT, assigned_executor='claude-code=sonnet')
        body['items'].pop()
        body['items'].append({'id': None, 'title': 'Tarea nueva', 'task': '', 'assigned_executor': None})
        code, view = self.save(body)
        self.assertEqual(code, 200, view)
        self.assertEqual(view['title'], body['title'])
        self.assertEqual(view['items'][0]['id'], original_id)
        self.assertNotEqual(view['items'][1]['id'], original_id)
        self.assertEqual(view['items'][1]['state'], 'pending')
        _, after = jobs.read_plan(self.plan_id)
        self.assertEqual(after['executor_policy'], before['executor_policy'])
        self.assertEqual(after['created_at'], before['created_at'])
        self.assertEqual(after['plan_sha256'], hashlib.sha256((path / 'plan.md').read_bytes()).hexdigest())
        self.assertFalse(jobs.validate_named_sections((path / 'plan.md').read_text(), jobs.REQUIRED_PLAN_SECTIONS))
        history = list((path / 'revisions').glob('*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(jobs.read_json(history[0])['plan'], before)
        self.assertEqual(jobs.read_json(history[0])['document'], old_document)
        self.assertEqual(history[0].stat().st_mode & 0o777, 0o600)
        code, launched = self.post(self.route + f"/items/{view['items'][1]['id']}/launch", self.launch_body())
        self.assertEqual(code, 200, launched)
        self.wait_finished(launched['job_id'])

    def test_concurrent_edits_reject_stale_revision(self):
        body = self.payload(); other = copy.deepcopy(body)
        body['title'] = 'Edición A'; other['title'] = 'Edición B'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.save, [body, other]))
        self.assertEqual(sorted(code for code, _ in results), [200, 409])
        conflict = next(value for code, value in results if code == 409)
        self.assertIn('El plan cambió', conflict['error'])
        self.assertEqual(len(list((jobs.plan_dir(self.plan_id) / 'revisions').glob('*.json'))), 1)

    def test_attempted_task_is_immutable_but_other_pending_work_can_change(self):
        first = self.payload()['items'][0]['id']
        code, launched = self.post(self.route + f'/items/{first}/launch', self.launch_body())
        self.assertEqual(code, 200, launched); self.wait_finished(launched['job_id'])
        body = self.payload(); bad = copy.deepcopy(body); bad['items'][0]['task'] = CONTRACT + '\nCambio de alcance.'
        self.assertEqual(self.save(bad)[0], 422)
        bad = copy.deepcopy(body); bad['items'].pop(0)
        self.assertEqual(self.save(bad)[0], 422)
        body['items'][1]['title'] = 'Segunda tarea refinada'
        code, result = self.save(body)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['items'][0]['job_ids'], [launched['job_id']])
        self.assertEqual(result['items'][0]['state'], 'in_progress')
        self.assertFalse(result['items'][0]['editable'])

    def test_active_agent_blocks_edit_without_writing_a_revision(self):
        first = self.payload()['items'][0]['id']
        code, launched = self.post(self.route + f'/items/{first}/launch', self.launch_body())
        self.assertEqual(code, 200, launched); self.wait_finished(launched['job_id'])
        body = self.payload()
        path = jobs.job_dir(launched['job_id'])
        jobs.write_json(path / 'result.json', {'state': 'running'})
        self.assertFalse(self.request(self.route)[1]['can_edit'])
        self.assertEqual(self.save(body)[0], 409)
        self.assertFalse((jobs.plan_dir(self.plan_id) / 'revisions').exists())

    def test_edit_cannot_expand_permissions_or_forge_ids_and_states(self):
        body = self.payload()
        variants = []
        invalid = copy.deepcopy(body); invalid['items'][0]['assigned_executor'] = 'codex-cli=gpt-6-astra'; variants.append(invalid)
        invalid = copy.deepcopy(body); invalid['items'][0]['id'] = 'item-unknown'; variants.append(invalid)
        invalid = copy.deepcopy(body); invalid['items'].append(copy.deepcopy(invalid['items'][0])); variants.append(invalid)
        invalid = copy.deepcopy(body); invalid['items'][0]['state'] = 'done'; variants.append(invalid)
        invalid = copy.deepcopy(body); invalid['items'][0]['task'] = 'Un contrato incompleto'; variants.append(invalid)
        invalid = copy.deepcopy(body); invalid['items'] = []; variants.append(invalid)
        before = (jobs.plan_dir(self.plan_id) / 'plan.json').read_bytes()
        for invalid in variants:
            with self.subTest(invalid=invalid):
                self.assertEqual(self.save(invalid)[0], 422)
                self.assertEqual((jobs.plan_dir(self.plan_id) / 'plan.json').read_bytes(), before)

    def test_authentication_ownership_and_archiving_are_enforced(self):
        body = self.payload()
        self.assertEqual(self.post(self.route + '/edit', body, authenticated=False)[0], 401)
        self.assertEqual(self.post(self.route.replace(self.pid, self.other_pid) + '/edit', body)[0], 404)
        code, result = self.post(f'/api/projects/{self.pid}/manage', {'action': 'archive'})
        self.assertEqual(code, 200, result)
        self.assertFalse(self.request(self.route)[1]['can_edit'])
        self.assertEqual(self.save(body)[0], 422)

    def test_adding_work_reopens_complete_plan_and_preserves_accepted_evidence(self):
        first = self.payload()['items'][0]['id']
        code, launch = self.post(self.route + f'/items/{first}/launch', self.launch_body())
        self.assertEqual(code, 200, launch); self.wait_finished(launch['job_id'])
        code, review = self.post(f"/api/projects/{self.pid}/jobs/{launch['job_id']}/review", {'verdict': 'accepted', 'notes': 'Resultado del proveedor simulado verificado.', 'tests': ['Salida determinista verificada en esta prueba.']})
        self.assertEqual(code, 200, review)
        body = self.payload(); body['items'].pop()
        self.assertEqual(self.save(body)[1]['complete'], True)
        before = jobs.read_plan(self.plan_id)[1]['items'][0]
        body = self.payload(); body['items'].append({'id': None, 'title': 'Trabajo adicional', 'task': '', 'assigned_executor': None})
        code, view = self.save(body)
        self.assertEqual(code, 200, view); self.assertFalse(view['complete'])
        self.assertEqual(jobs.read_plan(self.plan_id)[1]['items'][0], before)

    def test_tasks_linked_to_decisions_cannot_be_removed(self):
        body = self.payload(); removed = body['items'].pop()
        jobs.create_feedback(self.workspace, 'Una decisión vinculada.', plan_id=self.plan_id, checklist_item=removed['id'], notify=False)
        self.assertEqual(self.save(body)[0], 422)

    def test_a_new_goal_cannot_relabel_completed_work_as_verified(self):
        body = self.payload()
        path, plan = jobs.read_plan(self.plan_id)
        for item in plan['items']:
            item['state'] = 'done'
        jobs.save_plan(path, plan)
        body = self.payload(); body['goal'] = 'Un objetivo nuevo todavía sin verificar.'
        code, error = self.save(body)
        self.assertEqual(code, 422, error)
        self.assertIn('añade una tarea pendiente', error['error'])

    def test_edit_keeps_custom_document_sections(self):
        path = jobs.plan_dir(self.plan_id)
        with (path / 'plan.md').open('a') as handle:
            handle.write('\n## Notas del equipo\nDecisión original que debe conservarse.\n')
        body = self.payload(); body['title'] = 'Título afinado'
        code, result = self.save(body)
        self.assertEqual(code, 200, result)
        self.assertIn('## Notas del equipo\nDecisión original que debe conservarse.', (path / 'plan.md').read_text())

    def test_paused_supervision_report_reflects_edited_tasks(self):
        import supervision
        path, plan = jobs.read_plan(self.plan_id)
        state = {'enabled': False, 'operations': {}}
        state['report'] = supervision.report(plan, state)
        jobs.write_json(supervision.state_path(self.plan_id), state)
        body = self.payload(); body['items'].append({'id': None, 'title': 'Una tarea adicional', 'task': '', 'assigned_executor': None})
        code, view = self.save(body)
        self.assertEqual(code, 200, view)
        self.assertEqual(len(view['supervision']['report']['tasks']), 3)
        self.assertEqual(view['supervision']['report']['plan_revision'], view['edit_revision'])
