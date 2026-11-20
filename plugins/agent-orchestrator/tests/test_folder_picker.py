"""Native selection is an external boundary; HTTP authentication and path handling run for real."""
import subprocess
from pathlib import Path
from unittest.mock import patch

from test_panel_actions import ActionBase, dashboard
import native_folders


class FolderPickerTests(ActionBase):
    def test_selected_directory_and_cancellation_are_returned_without_creating_projects(self):
        before = dashboard.project_registry()
        with patch.object(native_folders, 'pick_folder', return_value=str(self.workspace)) as picker:
            code, result = self.post('/api/folders/pick', {'mode': 'existing', 'initial': str(self.workspace)})
            self.assertEqual((code, result), (200, {'cancelled': False, 'path': str(self.workspace)}))
            self.assertEqual(picker.call_args.args[0], self.workspace)
        with patch.object(native_folders, 'pick_folder', return_value=None):
            self.assertEqual(self.post('/api/folders/pick', {'mode': 'new'})[1], {'cancelled': True, 'path': None})
        self.assertEqual(dashboard.project_registry(), before)

    def test_invalid_requests_and_unauthenticated_requests_never_open_the_desktop(self):
        with patch.object(native_folders, 'pick_folder') as picker:
            for body in ({'mode': 'invalid'}, {'mode': 'new', 'initial': 'relative/path'}, {'mode': 'new', 'command': 'anything'}):
                self.assertEqual(self.post('/api/folders/pick', body)[0], 400)
            self.assertEqual(self.post('/api/folders/pick', {'mode': 'new'}, authenticated=False)[0], 401)
            self.assertEqual(self.post('/api/folders/pick', {'mode': 'new'}, headers={'Origin': 'https://outside.invalid'})[0], 403)
            picker.assert_not_called()

    def test_only_one_native_window_opens_at_a_time_and_errors_release_the_lock(self):
        with dashboard.FOLDER_PICKER_LOCK:
            with patch.object(native_folders, 'pick_folder') as picker:
                self.assertEqual(self.post('/api/folders/pick', {'mode': 'existing'})[0], 409)
                picker.assert_not_called()
        with patch.object(native_folders, 'pick_folder', side_effect=native_folders.FolderPickerError('No desktop')):
            code, result = self.post('/api/folders/pick', {'mode': 'new'})
            self.assertEqual((code, result['code']), (503, 'project_picker_unavailable'))
        self.assertTrue(dashboard.FOLDER_PICKER_LOCK.acquire(blocking=False))
        dashboard.FOLDER_PICKER_LOCK.release()

    def test_helper_preserves_spaces_and_shell_characters_as_one_argument(self):
        selected = self.workspace.parent / 'folder with spaces $(anything)'
        selected.mkdir()
        result = subprocess.CompletedProcess([], 0, '{"path": ' + __import__('json').dumps(str(selected)) + '}', '')
        with patch.object(native_folders.subprocess, 'run', return_value=result) as run:
            self.assertEqual(native_folders.pick_folder(selected, 'Select'), str(selected))
            self.assertIn(str(selected), run.call_args.args[0])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_helper_rejects_non_directory_results_and_handles_cancellation(self):
        for value in ('{"path":"relative"}', '{"path":"/not-a-real-ao-directory"}', 'not JSON', '[]'):
            with patch.object(native_folders.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, value, '')):
                with self.assertRaises(native_folders.FolderPickerError):
                    native_folders.pick_folder(self.workspace, 'Select')
        with patch.object(native_folders.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"cancelled":true}', '')):
            self.assertIsNone(native_folders.pick_folder(self.workspace, 'Select'))
