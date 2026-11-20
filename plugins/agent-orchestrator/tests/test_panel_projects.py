"""Project lifecycle over the authenticated HTTP API, with isolated state and directories."""
from pathlib import Path
import concurrent.futures

from test_panel_actions import ActionBase, jobs, dashboard


class ProjectTests(ActionBase):
    def add(self, **changes):
        return self.post('/api/projects', {'name': 'Proyecto nuevo', 'workspace': str(self.workspace.parent / 'Nuevo proyecto'),
                                          'mode': 'new', 'init_git': True, **changes})

    def manage(self, pid, action, **values):
        return self.post(f'/api/projects/{pid}/manage', {'action': action, **values})

    def test_new_empty_project_is_visible_immediately_and_accepts_a_plan(self):
        code, project = self.add()
        self.assertEqual(code, 200, project)
        workspace = self.workspace.parent / 'Nuevo proyecto'
        self.assertTrue((workspace / '.git').is_dir())
        self.assertEqual(project['jobs'], [])
        self.assertEqual(project['plans'], [])
        self.assertIn(project['id'], [entry['id'] for entry in self.request('/api/overview')[1]['projects']])
        code, plan = self.post(f"/api/projects/{project['id']}/plans", {
            'title': 'Primera tarea', 'goal': 'Probar proyecto vacío', 'checklist': ['Implementar'],
            'executors': ['claude-code=sonnet']})
        self.assertEqual(code, 200, plan)
        self.assertEqual(dashboard.DashboardState().project(project['id'])['name'], 'Proyecto nuevo')
        self.assertNotIn('workspace', project)

    def test_existing_directory_import_does_not_change_files_or_initialize_git(self):
        marker = self.workspace / 'original.txt'
        marker.write_text('conservar')
        code, project = self.add(mode='existing', workspace=str(self.workspace))
        self.assertEqual(code, 200, project)
        self.assertEqual(marker.read_text(), 'conservar')
        self.assertFalse((self.workspace / '.git').exists())
        self.assertEqual(self.add(mode='existing', workspace=str(self.workspace))[0], 409)
        code, settings = self.request(f"/api/projects/{project['id']}/settings")
        self.assertEqual((code, settings['workspace']), (200, str(self.workspace)))

    def test_rename_archive_restore_and_remove_preserve_files_and_history(self):
        code, project = self.add(mode='existing', workspace=str(self.workspace))
        self.assertEqual(code, 200, project)
        pid = project['id']
        self.assertEqual(self.manage(pid, 'rename', name='Nuevo nombre')[0], 200)
        self.assertEqual(self.request(f'/api/projects/{pid}')[1]['name'], 'Nuevo nombre')
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(self.manage(pid, 'archive')[0], 200)
        self.assertTrue(self.request(f'/api/projects/{pid}')[1]['archived'])
        self.assertEqual(self.post(f'/api/projects/{pid}/flows', {})[0], 409)
        self.assertEqual(self.manage(pid, 'restore')[0], 200)
        self.assertFalse(self.request(f'/api/projects/{pid}')[1]['archived'])
        self.assertEqual(self.manage(pid, 'delete', confirm=False)[0], 400)
        self.assertEqual(self.manage(pid, 'delete', confirm=True)[0], 200)
        self.assertEqual(self.request(f'/api/projects/{pid}')[0], 404)
        self.assertEqual(self.request(f'/api/projects/{pid}/settings')[0], 404)
        self.assertEqual(self.post(f'/api/projects/{pid}/plans', {})[0], 404)
        self.assertNotIn(pid, [entry['id'] for entry in self.request('/api/overview')[1]['projects']])
        self.assertTrue((jobs.feedback_dir('seed') / 'feedback.json').is_file())
        self.assertTrue(self.workspace.is_dir())
        code, restored = self.add(mode='existing', workspace=str(self.workspace))
        self.assertEqual((code, restored['id']), (200, pid))
        self.assertEqual(restored['question_count'], 1)

    def test_active_project_cannot_be_archived_or_removed(self):
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        # Keep the worker active long enough to assert both lifecycle operations.
        fake = self.workspace.parent / 'bin' / 'claude'
        fake.write_text('#!/usr/bin/env python3\nimport time,sys\nsys.stdin.read()\ntime.sleep(2)\n')
        code, launched = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch", self.launch_body())
        self.assertEqual(code, 200, launched)
        self.assertEqual(self.manage(self.pid, 'archive')[0], 409)
        self.assertEqual(self.manage(self.pid, 'delete', confirm=True)[0], 409)
        self.wait_finished(launched['job_id'])
        self.assertEqual(self.manage(self.pid, 'delete', confirm=True)[0], 200)
        self.assertEqual(self.request(f"/api/projects/{self.pid}/jobs/{launched['job_id']}")[0], 404)

    def test_invalid_paths_and_shapes_do_not_create_directories(self):
        cases = [({'workspace': 'relative/folder'}, 'workspace'), ({'name': ''}, 'name'),
                 ({'mode': 'unknown'}, 'mode'), ({'init_git': 'yes'}, 'init_git'),
                 ({'workspace': str(self.workspace)}, 'workspace'),
                 ({'workspace': str(self.workspace.parent / 'missing' / 'child')}, 'workspace')]
        for overrides, field in cases:
            code, result = self.add(**overrides)
            self.assertEqual((code, result.get('field')), (400, field), result)
        self.assertFalse((self.workspace.parent / 'missing').exists())
        self.assertEqual(self.manage('p-missing', 'delete', confirm=True)[0], 404)

    def test_concurrent_additions_keep_both_records(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.add, name=name, workspace=str(self.workspace.parent / name)) for name in ('One', 'Two')]
            results = [future.result() for future in futures]
        self.assertEqual([code for code, _ in results], [200, 200], results)
        names = {entry['name'] for entry in dashboard.DashboardState().overview()['projects']}
        self.assertTrue({'One', 'Two'}.issubset(names))

    def test_project_actions_require_authentication_and_local_origin(self):
        body = {'name': 'Denied', 'workspace': str(self.workspace.parent / 'Denied'), 'mode': 'new'}
        self.assertEqual(self.post('/api/projects', body, authenticated=False)[0], 401)
        self.assertEqual(self.post('/api/projects', body, headers={'Origin': 'https://outside.invalid'})[0], 403)
        self.assertFalse((self.workspace.parent / 'Denied').exists())

    def test_nested_new_project_requires_its_own_git_repository(self):
        code, project = self.add()
        self.assertEqual(code, 200, project)
        nested = self.workspace.parent / 'Nuevo proyecto' / 'Nested'
        code, result = self.add(workspace=str(nested), init_git=False)
        self.assertEqual((code, result.get('field')), (400, 'init_git'))
        self.assertFalse(nested.exists())
