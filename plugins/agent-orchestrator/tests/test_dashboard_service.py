import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class ServiceTests(unittest.TestCase):
    def test_restart_preserves_private_capability_and_sigterm_cleans_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, 'AGENT_ORCHESTRATOR_HOME': temp}
            tokens = []
            for attempt in range(2):
                process = subprocess.Popen([sys.executable, str(SCRIPTS / 'dashboard_service.py'), '--port', '0'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    runtime = Path(temp) / 'dashboard.json'
                    for _ in range(100):
                        if runtime.exists():
                            value = json.loads(runtime.read_text())
                            if value.get('pid') == process.pid:
                                break
                        if process.poll() is not None:
                            self.fail(process.stderr.read())
                        time.sleep(.03)
                    else:
                        self.fail('Service did not become ready')
                    token = value['url'].split('#token=')[1]; tokens.append(token)
                    connection = http.client.HTTPConnection('127.0.0.1', int(value['origin'].rsplit(':', 1)[1]), timeout=3)
                    connection.request('GET', '/api/overview', headers={'X-Orchestrator-Token': token})
                    response = connection.getresponse(); response.read(); self.assertEqual(response.status, 200); connection.close()
                    self.assertEqual((Path(temp) / 'dashboard-service-token.json').stat().st_mode & 0o777, 0o600)
                finally:
                    process.terminate()
                    output, error = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, error)
                self.assertFalse(runtime.exists())
                self.assertNotIn(token, output)
            self.assertEqual(tokens[0], tokens[1])
