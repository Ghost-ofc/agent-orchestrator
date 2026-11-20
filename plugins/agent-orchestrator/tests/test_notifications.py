import os
from unittest.mock import patch

from test_panel_actions import ActionBase, jobs


class NotificationTests(ActionBase):
    def test_important_default_suppresses_progress_and_deduplicates_final_plan(self):
        with patch.dict(os.environ, {'AGENT_ORCHESTRATOR_NOTIFICATIONS': ''}), patch.object(jobs.shutil, 'which', return_value='/bin/notify-send'), patch.object(jobs.subprocess, 'run') as run:
            jobs.notify_user('Agent Orchestrator', 'Avance del agente')
            run.assert_not_called()
            jobs.notify_user('Agent Orchestrator', 'Plan revisado', category='complete', key='plan:test')
            jobs.notify_user('Agent Orchestrator', 'Plan completado', category='complete', key='plan:test')
            self.assertEqual(run.call_count, 1)
            jobs.notify_user('Agent Orchestrator', 'Otro cierre', category='complete', key='plan:other')
            self.assertEqual(run.call_count, 1)

    def test_off_env_silences_even_intervention_notifications(self):
        with patch.object(jobs.subprocess, 'run') as run:
            jobs.notify_user('Agent Orchestrator', 'Intervención', category='attention')
            run.assert_not_called()

    def test_panel_settings_require_authentication_and_persist_mode(self):
        self.assertEqual(self.request('/api/notifications')[1]['mode'], 'important')
        self.assertEqual(self.post('/api/notifications', {'mode': 'off'}, authenticated=False)[0], 401)
        self.assertEqual(self.post('/api/notifications', {'mode': 'off'})[0], 200)
        self.assertEqual(self.request('/api/notifications')[1]['mode'], 'off')
        self.assertEqual(self.post('/api/notifications', {'mode': 'invalid'})[0], 400)
        self.assertEqual((jobs.state_home() / 'notifications.json').stat().st_mode & 0o777, 0o600)
        with patch.dict(os.environ, {'AGENT_ORCHESTRATOR_NOTIFICATIONS': ''}), patch.object(jobs.subprocess, 'run') as run:
            jobs.notify_user('Agent Orchestrator', 'Intervención', category='attention')
            run.assert_not_called()
