"""SSH discovery never executes config commands or reads private key contents."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ssh_inventory import inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.ssh = self.home / '.ssh'; self.ssh.mkdir()
        self.key = self.ssh / 'key with spaces'; self.key.write_text('PRIVATE_CONTENT_MUST_NEVER_BE_READ')
        self.other = self.ssh / 'id_other'; self.other.write_text('OTHER_PRIVATE_CONTENT')

    def config(self, text):
        (self.ssh / 'config').write_text(text)
        return inventory(self.home)

    def test_aliases_wildcards_negation_and_multiple_identity_files(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotIn(path, {self.key, self.other})
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            value = self.config('Host prod.example staging\n HostName %h.internal\n User deploy\n IdentityFile "~/.ssh/key with spaces"\n IdentityFile ~/.ssh/id_other\nHost * !staging\n Port = 2222\n')
        hosts = {host['name']: host for host in value['hosts']}
        self.assertEqual(hosts['prod.example']['host'], 'prod.example.internal')
        self.assertEqual(hosts['prod.example']['port'], 2222)
        self.assertEqual(hosts['staging']['port'], 22)
        self.assertEqual(len(hosts['staging']['identity_files']), 2)
        self.assertTrue(hosts['staging']['eligible'])
        self.assertNotIn('PRIVATE_CONTENT', json.dumps(value))

    def test_include_globs_and_first_value_precedence(self):
        directory = self.ssh / 'conf.d'; directory.mkdir()
        (directory / 'a.conf').write_text('Host imported\n HostName first.example\n User deploy\n IdentityFile ~/.ssh/id_other\n')
        (directory / 'b.conf').write_text('Host imported\n HostName ignored.example\n')
        value = self.config('Include conf.d/*.conf\nHost *\n HostName fallback.example\n')
        host = value['hosts'][0]
        self.assertEqual((host['name'], host['host']), ('imported', 'first.example'))
        self.assertTrue(host['eligible'])

    def test_match_exec_and_proxy_commands_are_reported_without_execution(self):
        marker = self.home / 'MUST_NOT_EXIST'
        with patch('subprocess.run') as run:
            value = self.config(f'Host direct\n HostName direct.example\n IdentityFile ~/.ssh/id_other\n ProxyCommand echo SECRET_COMMAND\nMatch exec "touch {marker}"\n User root\n')
            run.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertFalse(value['hosts'][0]['eligible'])
        self.assertNotIn('SECRET_COMMAND', json.dumps(value))
        self.assertTrue(value['warnings'])

    def test_broad_include_does_not_open_key_files_and_cycles_are_bounded(self):
        (self.ssh / 'cycle.conf').write_text('Include config\n')
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotIn(path, {self.key, self.other})
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            value = self.config('Include *\nHost safe\n IdentityFile ~/.ssh/id_other\n')
        self.assertTrue(value['warnings'])
        self.assertFalse(value['hosts'][0]['eligible'])

    def test_missing_identity_and_unreferenced_candidates_are_metadata_only(self):
        extra = self.ssh / 'extra.pem'; extra.write_text('UNREAD_PRIVATE')
        value = self.config('Host missing\n HostName missing.example\n IdentityFile ~/.ssh/absent\n')
        self.assertFalse(value['hosts'][0]['eligible'])
        self.assertFalse(value['hosts'][0]['identity_files'][0]['exists'])
        item = next(key for key in value['keys'] if key['path'] == str(extra))
        self.assertTrue(item['candidate'])
        self.assertNotIn('UNREAD_PRIVATE', json.dumps(value))

    def test_empty_home_does_not_fail_or_invent_hosts(self):
        value = inventory(self.home)
        self.assertFalse(value['config_found'])
        self.assertEqual(value['hosts'], [])

    def test_static_identity_in_wildcard_only_config_still_appears_in_inventory(self):
        value = self.config('Host *\n IdentityFile ~/.ssh/id_other\n')
        self.assertEqual(value['hosts'], [])
        key = next(item for item in value['keys'] if item['path'] == str(self.other))
        self.assertTrue(key['referenced'])
        self.assertFalse(key['candidate'])

    def test_groups_natural_order_shared_keys_and_unknown_environments(self):
        (self.ssh / 'config').write_text('Host host10 host2\n User root\n IdentityFile ~/.ssh/id_other\nHost unclassified\n IdentityFile ~/.ssh/key with spaces\n')
        value = inventory(self.home, {'hosts': {'host10': {'group': 'Cliente', 'environment': 'production'}, 'host2': {'group': 'Cliente', 'environment': 'invalid'}}})
        self.assertEqual([host['name'] for host in value['hosts']], ['host2', 'host10', 'unclassified'])
        self.assertEqual(value['hosts'][0]['environment'], 'unknown')
        self.assertTrue(value['hosts'][0]['root_account'])
        self.assertEqual(value['hosts'][2]['group'], 'Sin clasificar')
        key = next(key for key in value['keys'] if key['path'] == str(self.other))
        self.assertTrue(key['shared'])
        self.assertEqual(key['groups'], ['Cliente'])
        self.assertEqual(value['groups'][0]['root_accounts'], 2)

    def test_invalid_organization_does_not_hide_inventory(self):
        import ssh_inventory
        with patch.object(ssh_inventory, 'read_organization', side_effect=ValueError('invalid')):
            value = self.config('Host staging\n IdentityFile ~/.ssh/id_other\n')
        self.assertEqual(value['hosts'][0]['group'], 'Sin clasificar')
        self.assertTrue(value['warnings'])
