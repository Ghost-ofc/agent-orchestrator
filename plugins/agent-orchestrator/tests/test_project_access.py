"""Access configuration and real engine launches, with fake provider/SSH boundaries only."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest.mock import patch

from test_panel_actions import ActionBase, jobs, dashboard
import project_access
import access_broker

FLAGS = '--restricted --strict-mcp-config --tools --settings --setting-sources --no-chrome --disable-slash-commands'


class AccessTests(ActionBase):
    def setUp(self):
        super().setUp()
        fake = self.workspace.parent / 'bin' / 'claude'
        fake.write_text('#!/usr/bin/env python3\nimport sys,json\n'
                        f'if "--help" in sys.argv:\n print({FLAGS!r})\n sys.exit(0)\n'
                        'sys.stdin.read()\nprint(json.dumps({"type":"result","result":"ok"}))\n')
        self.key = self.workspace.parent / 'test_identity'
        self.key.write_text('FAKE KEY FOR TESTS ONLY')
        (self.workspace / '.mcp.json').write_text(json.dumps({'mcpServers': {
            'approved': {'command': '/bin/echo', 'args': ['TEST_SECRET']},
            'excluded': {'type': 'http', 'url': 'https://unapproved.invalid/mcp'}}}))

    def settings(self, **changes):
        return {'enabled': True, 'mcp_servers': ['approved'], 'ssh_hosts': [], **changes}

    def save(self, **changes):
        return self.post(f'/api/projects/{self.pid}/access', self.settings(**changes))

    def host(self, **changes):
        return {'name': 'staging', 'host': 'staging.example.com', 'user': 'deploy', 'port': 22,
                'identity_file': str(self.key), **changes}

    def test_policy_is_private_persistent_and_not_applied_to_other_projects(self):
        code, result = self.save(ssh_hosts=[self.host()])
        self.assertEqual(code, 200, result)
        self.assertEqual(result['policy']['mcp_servers'], ['approved'])
        self.assertNotIn('TEST_SECRET', json.dumps(result))
        saved = dashboard.project_registry()[self.pid]['access_policy']
        self.assertEqual(saved['mcp_servers']['approved']['args'], ['TEST_SECRET'])
        self.assertEqual(stat.S_IMODE(dashboard.project_registry_path().stat().st_mode), 0o600)
        self.assertTrue(project_access.policy_for(self.workspace, jobs.state_home())['enabled'])
        self.assertFalse(project_access.policy_for(self.other, jobs.state_home())['enabled'])
        overview = self.request('/api/overview')[1]
        self.assertTrue(next(p for p in overview['projects'] if p['id'] == self.pid)['access_restricted'])
        self.assertNotIn('TEST_SECRET', json.dumps(overview))

    def test_real_engine_launch_adds_strict_flags_and_only_selected_mcp_configs(self):
        self.assertEqual(self.save(ssh_hosts=[self.host()])[0], 200)
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        code, launch = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch", self.launch_body())
        self.assertEqual(code, 200, launch)
        self.wait_finished(launch['job_id'])
        path = jobs.job_dir(launch['job_id'])
        meta = jobs.read_json(path / 'meta.json')
        self.assertEqual(jobs.get_status(path)['state'], 'succeeded')
        self.assertIn('--restricted', meta['command'])
        self.assertIn('--strict-mcp-config', meta['command'])
        self.assertNotIn('Bash', meta['command'][meta['command'].index('--tools') + 1])
        self.assertEqual(set(jobs.read_json(path / 'access-mcp.json')['mcpServers']), {'approved', 'ao_control'})
        self.assertEqual(meta['access']['ssh_hosts'], ['staging'])
        self.assertEqual(stat.S_IMODE((path / 'access-manifest.json').stat().st_mode), 0o600)
        self.assertNotIn('TEST_SECRET', json.dumps(meta))
        self.assertIn('mcp__ao_control__event', (path / 'task.md').read_text())

    def test_unsupported_cli_and_custom_profile_are_rejected_before_job_creation(self):
        self.assertEqual(self.save()[0], 200)
        code, plan = self.make_plan(executors=['codex-cli=gpt-5.6-terra'])
        self.assertEqual(code, 200, plan)
        code, result = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch",
                                 self.launch_body(cli='codex-cli', model='gpt-5.6-terra'))
        self.assertEqual(code, 422, result)
        self.assertFalse(list(jobs.jobs_dir().glob('*/meta.json')))
        profile = jobs.validate_profile('claude-code', jobs.BUILTIN_PROFILES['claude-code'])
        with self.assertRaises(project_access.AccessError):
            project_access.require_supported('claude-code', {**profile, 'argv': ['/bin/echo']}, profile)

    def test_old_cli_without_required_controls_cannot_enable_strict_mode(self):
        fake = self.workspace.parent / 'bin' / 'claude'
        fake.write_text('#!/usr/bin/env python3\nprint("old CLI")\n')
        self.assertEqual(self.save()[0], 422)
        self.assertFalse(project_access.policy_for(self.workspace, jobs.state_home())['enabled'])

    def test_invalid_selections_ssh_options_and_authentication_fail_closed(self):
        cases = [self.settings(mcp_servers=['missing']), self.settings(mcp_servers=['ao_control']),
                 self.settings(ssh_hosts=[self.host(host='host -oProxyCommand=anything')]),
                 self.settings(ssh_hosts=[self.host(port=True)]), self.settings(ssh_hosts=[self.host(identity_file='/nonexistent')]),
                 self.settings(ssh_hosts=[self.host(proxy_command='anything')]), self.settings(enabled=False)]
        for body in cases:
            self.assertEqual(self.post(f'/api/projects/{self.pid}/access', body)[0], 422, body)
        self.assertEqual(self.post(f'/api/projects/{self.pid}/access', self.settings(), authenticated=False)[0], 401)
        self.assertEqual(self.post(f'/api/projects/{self.pid}/access', self.settings(), headers={'Origin': 'https://outside.invalid'})[0], 403)
        self.assertEqual(self.post('/api/projects/p-missing/access', self.settings())[0], 404)

    def test_ssh_broker_rejects_unapproved_hosts_and_cannot_receive_custom_options(self):
        manifest = {'ssh_hosts': [self.host()]}
        with patch.object(access_broker.subprocess, 'Popen') as spawn:
            for args in ({'host': 'other.example.com', 'command': 'id'}, {'host': 'staging', 'command': 'id', 'options': ['-L']},
                         {'host': 'staging', 'command': 'id', 'timeout_seconds': True}):
                with self.assertRaises(ValueError):
                    access_broker.run_ssh(manifest, args)
            spawn.assert_not_called()
        argv, timeout = access_broker.ssh_command(manifest, {'host': 'staging', 'command': 'printf "$HOME"'})
        self.assertEqual(argv[-2:], ['staging.example.com', 'printf "$HOME"'])
        self.assertIn('-oStrictHostKeyChecking=yes', argv)
        self.assertIn('-oClearAllForwardings=yes', argv)
        self.assertIn('-oForwardAgent=no', argv)
        self.assertEqual(argv[argv.index('-F') + 1], '/dev/null')
        self.assertNotIn('ssh_exec', [item['name'] for item in access_broker.available_tools({'ssh_hosts': []})])

    def test_mcp_protocol_and_coordination_channel_work_without_a_terminal(self):
        path = jobs.job_dir('restricted-test', must_exist=False)
        path.mkdir(parents=True)
        (path / 'channel').mkdir()
        manifest = {'state_home': str(jobs.state_home()), 'job_id': path.name, 'ssh_hosts': []}
        manifest_path = path / 'access-manifest.json'
        jobs.write_json(manifest_path, manifest)
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'ssh_exec', 'arguments': {'host': 'denied', 'command': 'id'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'event', 'arguments': {'phase': 'validation', 'message': 'Checking'}}},
        ]
        result = subprocess.run([sys.executable, access_broker.__file__, str(manifest_path)], input='\n'.join(map(json.dumps, requests)) + '\n', capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertTrue(responses[2]['result']['isError'])
        self.assertFalse(responses[3]['result']['isError'])
        self.assertEqual(jobs.read_events(jobs.channel_dir(path))[0]['message'], 'Checking')

    def test_restricted_child_environment_has_no_ssh_agent_or_ssh_command_override(self):
        with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/secret-agent', 'GIT_SSH_COMMAND': 'ssh -oProxyCommand=x', 'PROVIDER_TOKEN': 'retained'}):
            env = project_access.restricted_environment(None)
        self.assertNotIn('SSH_AUTH_SOCK', env)
        self.assertNotIn('GIT_SSH_COMMAND', env)
        self.assertEqual(env['PROVIDER_TOKEN'], 'retained')

    def test_changing_access_is_blocked_while_an_agent_is_active(self):
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        fake = self.workspace.parent / 'bin' / 'claude'
        fake.write_text('#!/usr/bin/env python3\nimport time,sys\nsys.stdin.read()\ntime.sleep(2)\n')
        code, launch = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch", self.launch_body())
        self.assertEqual(code, 200, launch)
        self.assertEqual(self.save()[0], 409)
        self.wait_finished(launch['job_id'])

    def test_ssh_output_is_bounded_without_connecting_to_any_server(self):
        path = jobs.job_dir('ssh-output-test', must_exist=False)
        path.mkdir(parents=True)
        (path / 'channel').mkdir()
        manifest = {'job_id': path.name, 'ssh_hosts': [self.host()]}
        with patch.object(access_broker, 'ssh_command', return_value=([sys.executable, '-c', 'print("x" * 100000)'], 5)):
            result = access_broker.run_ssh(manifest, {'host': 'staging', 'command': 'fake-output'})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['stdout']), 32768)

    def test_removing_and_reimporting_a_project_preserves_access_restrictions(self):
        self.assertEqual(self.save()[0], 200)
        self.assertEqual(self.post(f'/api/projects/{self.pid}/manage', {'action': 'delete', 'confirm': True})[0], 200)
        code, project = self.post('/api/projects', {'name': 'Restored', 'workspace': str(self.workspace), 'mode': 'existing'})
        self.assertEqual(code, 200, project)
        self.assertTrue(project['access_restricted'])

    def test_restricted_automatic_routing_uses_a_supported_executor(self):
        self.assertEqual(self.save()[0], 200)
        code, plan = self.make_plan(executors=[])
        self.assertEqual(code, 200, plan)
        self.assertEqual([entry['display'] for entry in plan['executor_policy']['allowed']], ['claude-code=sonnet'])
        code, launch = self.post(f'/api/projects/{self.pid}/flows', {
            'title': 'Restricted flow', 'goal': 'Plan', 'coordinator': 'claude-code=sonnet', 'confirm': True})
        self.assertEqual(code, 200, launch)
        self.wait_finished(launch['job_id'])
        record = jobs.read_json(jobs.job_dir(launch['job_id']) / 'flow.json')
        self.assertEqual([entry['display'] for entry in record['pool']], ['claude-code=sonnet'])

    def test_local_mcp_definitions_have_precedence_and_the_catalog_names_the_source(self):
        config = self.workspace.parent / '.claude.json'
        config.write_text(json.dumps({'mcpServers': {'approved': {'command': '/user-version'}},
            'projects': {str(self.workspace): {'mcpServers': {'approved': {'command': '/local-version'}}}}}))
        with patch.object(project_access.Path, 'home', return_value=self.workspace.parent):
            self.assertEqual(project_access.mcp_definitions(self.workspace)['approved']['command'], '/local-version')
            code, result = self.request(f'/api/projects/{self.pid}/access')
            self.assertEqual(code, 200, result)
            entry = next(item for item in result['mcp_catalog'] if item['name'] == 'approved')
            self.assertEqual(entry['source'], 'Local de Claude para este proyecto')

    def test_custom_cli_agent_cannot_inject_a_different_tool_configuration(self):
        self.assertEqual(self.save()[0], 200)
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        code, result = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch", self.launch_body(cli_agent='custom'))
        self.assertEqual(code, 422, result)
        self.assertFalse(list(jobs.jobs_dir().glob('*/meta.json')))

    def test_each_agent_receives_only_its_profile_and_unspecified_profile_denies_resources(self):
        profiles = {'backend': {'mcp_servers': ['approved'], 'ssh_hosts': ['staging']}, 'review': {'mcp_servers': [], 'ssh_hosts': []}}
        code, result = self.save(ssh_hosts=[self.host(), self.host(name='production', host='prod.example.com')], agent_profiles=profiles, per_agent=True)
        self.assertEqual(code, 200, result)
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        route = f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch"
        for selected, expected_mcp, expected_ssh in [('backend', ['approved'], ['staging']), (None, [], [])]:
            body = self.launch_body(**({'access_profile': selected} if selected else {}))
            code, launch = self.post(route, body)
            self.assertEqual(code, 200, launch)
            self.wait_finished(launch['job_id'])
            path = jobs.job_dir(launch['job_id'])
            self.assertEqual(jobs.get_status(path)['state'], 'succeeded')
            meta = jobs.read_json(path / 'meta.json')
            self.assertEqual(meta['access']['mcp_servers'], expected_mcp)
            self.assertEqual(meta['access']['ssh_hosts'], expected_ssh)
            self.assertEqual(set(jobs.read_json(path / 'access-mcp.json')['mcpServers']), set(expected_mcp + ['ao_control']))
            self.assertEqual([host['name'] for host in jobs.read_json(path / 'access-manifest.json')['ssh_hosts']], expected_ssh)
            template = self.request(f"/api/projects/{self.pid}/jobs/{launch['job_id']}/launch-template")[1]
            self.assertEqual(template['access_profile'], selected)
        code, result = self.post(route, self.launch_body(access_profile='unknown'))
        self.assertEqual(code, 422, result)

    def test_profiles_cannot_widen_project_grants_and_removing_last_profile_stays_deny_by_default(self):
        self.assertEqual(self.save(agent_profiles={'bad': {'mcp_servers': ['excluded'], 'ssh_hosts': []}}, per_agent=True)[0], 422)
        self.assertEqual(self.save(agent_profiles={'bad': {'mcp_servers': [], 'ssh_hosts': ['unapproved']}}, per_agent=True)[0], 422)
        code, result = self.save(agent_profiles={}, per_agent=True)
        self.assertEqual(code, 200, result)
        policy = project_access.policy_for(self.workspace, jobs.state_home())
        effective = project_access.for_agent(policy, None)
        self.assertEqual(effective['mcp_servers'], {})
        self.assertEqual(effective['ssh_hosts'], [])
        self.assertTrue(result['policy']['per_agent'])
        # An older client that does not know profiles cannot silently remove this boundary.
        self.assertEqual(self.save()[0], 200)
        self.assertIn('agent_profiles', project_access.policy_for(self.workspace, jobs.state_home()))

    def test_coordinator_uses_the_selected_access_profile_and_plan_lists_profiles(self):
        self.assertEqual(self.save(agent_profiles={'planner': {'mcp_servers': [], 'ssh_hosts': []}}, per_agent=True)[0], 200)
        code, launch = self.post(f'/api/projects/{self.pid}/flows', {'title': 'Plan', 'goal': 'Inspect',
            'coordinator': 'claude-code=sonnet', 'confirm': True, 'access_profile': 'planner'})
        self.assertEqual(code, 200, launch)
        self.wait_finished(launch['job_id'])
        meta = jobs.read_json(jobs.job_dir(launch['job_id']) / 'meta.json')
        self.assertEqual(meta['access']['profile'], 'planner')
        self.assertEqual(meta['access']['mcp_servers'], [])
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        detail = self.request(f"/api/projects/{self.pid}/plans/{plan['plan_id']}")[1]
        self.assertEqual(set(detail['access_policy']['agent_profiles']), {'planner'})

    def test_native_file_tools_are_denied_access_to_ssh_keys_and_authentication_files(self):
        self.assertEqual(self.save(ssh_hosts=[self.host()], agent_profiles={'review': {'mcp_servers': [], 'ssh_hosts': []}}, per_agent=True)[0], 200)
        policy = project_access.for_agent(project_access.policy_for(self.workspace, jobs.state_home()), 'review')
        path = jobs.job_dir('key-privacy-test', must_exist=False); path.mkdir(parents=True)
        project_access.prepare(policy, path, jobs.state_home(), jobs.write_json)
        settings = jobs.read_json(path / 'access-settings.json')
        self.assertIn('Read(~/.ssh/**)', settings['permissions']['deny'])
        self.assertIn('Read(/' + str(self.key) + ')', settings['permissions']['deny'])
        self.assertIn('Edit(/' + str(self.key) + ')', settings['permissions']['deny'])
        self.assertNotIn('FAKE KEY', json.dumps(settings))

    def test_worker_refuses_a_snapshot_that_does_not_match_its_recorded_profile(self):
        self.assertEqual(self.save(agent_profiles={'backend': {'mcp_servers': ['approved'], 'ssh_hosts': []}, 'review': {'mcp_servers': [], 'ssh_hosts': []}}, per_agent=True)[0], 200)
        code, plan = self.make_plan()
        self.assertEqual(code, 200, plan)
        code, launch = self.post(f"/api/projects/{self.pid}/plans/{plan['plan_id']}/items/{plan['items'][0]['id']}/launch", self.launch_body(access_profile='backend'))
        self.assertEqual(code, 200, launch)
        self.wait_finished(launch['job_id'])
        path = jobs.job_dir(launch['job_id']); meta = jobs.read_json(path / 'meta.json')
        meta['access_profile'] = 'review'; jobs.write_json(path / 'meta.json', meta)
        output = (path / 'stdout.log').read_bytes()
        result = subprocess.run([sys.executable, jobs.__file__, '_worker', '--job-dir', str(path)], capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(jobs.get_status(path)['state'], 'failed')
        self.assertEqual((path / 'stdout.log').read_bytes(), output)

    def test_inventory_labels_are_persistent_without_changing_permissions_or_ssh(self):
        import ssh_inventory
        home = self.workspace.parent / 'fake-home'
        ssh = home / '.ssh'; ssh.mkdir(parents=True)
        config = ssh / 'config'; config.write_text('Host dev\n User root\n IdentityFile ~/.ssh/key\n')
        before = config.read_bytes()
        self.assertEqual(self.save()[0], 200)
        policy_before = dashboard.project_registry_path().read_bytes()
        discover = ssh_inventory.inventory
        with patch.object(dashboard.ssh_inventory, 'inventory', side_effect=lambda: discover(home)):
            body = {'alias': 'dev', 'group': 'Cliente', 'environment': 'development'}
            self.assertEqual(self.post('/api/ssh/organization', body, authenticated=False)[0], 401)
            code, result = self.post('/api/ssh/organization', body)
            self.assertEqual(code, 200, result)
            self.assertEqual(result['hosts'][0]['group'], 'Cliente')
            self.assertEqual(result['hosts'][0]['environment'], 'development')
            self.assertEqual(self.post('/api/ssh/organization', {**body, 'alias': 'absent'})[0], 400)
            self.assertEqual(self.post('/api/ssh/organization', {**body, 'environment': 'guessed'})[0], 400)
            self.assertEqual(self.post('/api/ssh/organization', {**body, 'grant': True})[0], 400)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(dashboard.project_registry_path().read_bytes(), policy_before)
        self.assertEqual(stat.S_IMODE(ssh_inventory.organization_path().stat().st_mode), 0o600)
