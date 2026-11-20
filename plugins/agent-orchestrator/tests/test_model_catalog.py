import json
import subprocess
from unittest.mock import patch
from test_panel_actions import ActionBase
import model_catalog


class ModelCatalogTests(ActionBase):
    def setUp(self):
        super().setUp()
        model_catalog.CACHE.clear()

    def test_antigravity_ids_and_labels_are_preserved(self):
        models = model_catalog.parse_models('gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n')
        self.assertEqual(models[0]['id'], 'gemini-3.8-flash-high')
        self.assertEqual(models[1]['name'], 'Claude Sonnet 4.6 (Thinking)')

    def test_opencode_verbose_metadata_identifies_free_and_paid_models(self):
        output = 'opencode/tidepool-alpha-free\n' + json.dumps({'name': 'Tidepool Alpha Free', 'cost': {'input': 0, 'output': 0}}, indent=2)
        output += '\nopencode/paid-model\n' + json.dumps({'name': 'Paid model', 'cost': {'input': 1, 'output': 2}}, indent=2)
        models = model_catalog.parse_models(output)
        self.assertTrue(models[0]['free']); self.assertFalse(models[1]['free'])
        self.assertEqual(models[0]['name'], 'Tidepool Alpha Free')

    def test_configured_aliases_and_authentication_boundaries(self):
        code, data = self.request('/api/executors/claude-code/models')
        self.assertEqual(code, 200, data)
        self.assertIn('sonnet', [model['id'] for model in data['models']])
        self.assertEqual(self.request('/api/executors/claude-code/models', authenticated=False)[0], 401)
        self.assertEqual(self.request('/api/executors/unknown/models')[0], 404)
        self.assertNotIn(self.temp.name, json.dumps(data))

    def test_live_cache_refresh_and_diagnostics_are_sanitized(self):
        profile = {'argv': ['fake', '{prompt_file}'], 'models': [], 'discover_models_argv': ['fake', 'models']}
        with patch.object(model_catalog.jobs, 'load_profiles', return_value={'sample': profile}), patch.object(model_catalog.jobs, 'resolve_profile_executable', return_value=('fake', '/private/fake')):
            with patch.object(model_catalog.subprocess, 'run', return_value=subprocess.CompletedProcess(['fake'], 0, 'model-one\tModel One\n', '')) as run:
                first = model_catalog.catalog('sample')
                self.assertEqual(first['models'][0]['name'], 'Model One')
                model_catalog.catalog('sample'); self.assertEqual(run.call_count, 1)
                model_catalog.catalog('sample', refresh=True); self.assertEqual(run.call_count, 2)
            with patch.object(model_catalog.subprocess, 'run', return_value=subprocess.CompletedProcess(['fake'], 1, '', 'SECRET_TOKEN /private/fake')):
                failed = model_catalog.catalog('sample', refresh=True)
                self.assertNotIn('SECRET_TOKEN', json.dumps(failed))
                self.assertNotIn('/private/fake', json.dumps(failed))

    def test_restricted_capability_is_explicit_for_every_cli(self):
        code, data = self.request('/api/executors')
        self.assertEqual(code, 200)
        by_cli = {item['cli']: item for item in data['harnesses']}
        self.assertTrue(by_cli['claude-code']['restricted_supported'])
        self.assertFalse(by_cli['antigravity']['restricted_supported'])
        self.assertFalse(by_cli['opencode']['restricted_supported'])
