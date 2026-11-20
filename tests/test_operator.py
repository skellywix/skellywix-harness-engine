import http.client
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from skellywix.hindsight_desk import HindsightDesk
from skellywix.neural import make_server
from skellywix.settings_catalog import DeskError, load_dependencies
from skellywix.settings_desk import SettingsDesk

try:
    load_dependencies()
    OPERATOR_AVAILABLE = True
except DeskError:
    OPERATOR_AVAILABLE = False  # The base runtime has no optional editor dependencies.


@unittest.skipUnless(OPERATOR_AVAILABLE, 'Install the [operator] extra for operator tests.')
class OperatorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.omp = self.home / '.omp/agent'
        self.pi = self.home / '.pi/agent'
        self.project = self.home / 'project'
        for path in (self.omp, self.pi, self.project / '.omp', self.project / '.pi'):
            path.mkdir(parents=True)
        self.registry = Mock()
        self.registry.validate.side_effect = lambda key, value: value
        self.registry.fields.return_value = {
            'modelRoles': {'type': 'record', 'description': 'Roles'},
            'compaction.enabled': {'type': 'boolean', 'value': True, 'description': 'Compaction'},
            'memory.backend': {'type': 'enum', 'options': ['off', 'hindsight'], 'value': 'off'},
        }
        self.settings = SettingsDesk(self.omp, self.project, self.home, {}, registry=self.registry)

    def update(self, current, key, value):
        return self.settings.dispatch({'action': 'save', 'source': current['source']['id'],
            'key': key, 'value': value, 'revision': current['revision'], 'confirm': True})

    def test_pi_loads_existing_scopes_and_changes_only_the_selected_key(self):
        source = self.pi / 'settings.json'
        source.write_text('{"theme":"fixture-dark","compaction":{"reserveTokens":900},"future":{"feature":true}}')
        project = self.project / '.pi/settings.json'
        project.write_text('{"theme":"fixture-project"}')
        current = self.settings.dispatch({'action': 'open', 'source': 'pi:user:settings'})
        fields = {row['key']: row for row in current['fields']}
        self.assertEqual(fields['theme']['value'], 'fixture-dark')
        self.assertIn('sessionShareDestination', fields)
        self.assertEqual(json.loads(source.read_bytes())['theme'], 'fixture-dark')
        with self.assertRaises(DeskError):
            self.update(current, 'compaction.reserveTokens', -1)
        saved = self.update(current, 'compaction.reserveTokens', 1200)
        self.assertEqual(json.loads(source.read_bytes()), {'theme': 'fixture-dark', 'compaction': {'reserveTokens': 1200},
                                                           'future': {'feature': True}})
        self.assertEqual(json.loads(project.read_bytes()), {'theme': 'fixture-project'})
        with self.assertRaises(DeskError) as caught:
            self.update(current, 'theme', 'stale')
        self.assertEqual(caught.exception.code, 409)
        self.settings.dispatch({'action': 'undo', 'source': saved['source']['id'], 'revision': saved['revision'], 'confirm': True})
        self.assertEqual(json.loads(source.read_bytes())['compaction']['reserveTokens'], 900)
        scoped = self.settings.dispatch({'action': 'open', 'source': 'pi:project:settings'})
        with self.assertRaises(DeskError):
            self.update(scoped, 'defaultProjectTrust', 'always')
        self.assertFalse((self.pi / 'trust.json').exists())

    def test_omp_loads_profiles_and_preserves_yaml_and_shadowing_layers(self):
        source = self.omp / 'config.yaml'
        source.write_text('# Keep this note\ncompaction:\n  enabled: true\nunknown: keep\n')
        project = self.project / '.omp/config.yml'
        project.write_text('compaction:\n  enabled: false\n')
        named = self.home / '.omp/profiles/sandbox/agent'
        named.mkdir(parents=True)
        (named / 'config.yml').write_text('memory:\n  backend: hindsight\n')
        current = self.settings.dispatch({'action': 'open', 'source': 'omp:active:settings'})
        self.assertTrue(any('sandbox' in row['label'] for row in current['sources']))
        fields = {row['key']: row for row in current['fields']}
        self.assertTrue(fields['compaction.enabled']['value'])
        self.assertIn('unknown', current['unlisted'])
        self.update(current, 'compaction.enabled', False)
        self.registry.validate.assert_called_with('compaction.enabled', False)
        self.assertIn('# Keep this note', source.read_text())
        self.assertIn('unknown: keep', source.read_text())
        self.assertIn('enabled: false', source.read_text())
        self.assertFalse((self.omp / 'config.yml').exists())
        self.assertEqual(project.read_text(), 'compaction:\n  enabled: false\n')

    def test_credentials_are_masked_and_preserved_after_noncredential_edit(self):
        models = self.pi / 'models.json'
        models.write_text('{"providers":{"fixture":{"apiKey":"example-private-canary","baseUrl":"http://localhost:3456"}}}')
        current = self.settings.dispatch({'action': 'open', 'source': 'pi:user:models'})
        self.assertNotIn('example-private-canary', json.dumps(current))
        providers = next(row['value'] for row in current['fields'] if row['key'] == 'providers')
        providers['fixture']['baseUrl'] = 'http://localhost:3457'
        self.update(current, 'providers', providers)
        provider = json.loads(models.read_bytes())['providers']['fixture']
        self.assertEqual(provider, {'apiKey': 'example-private-canary', 'baseUrl': 'http://localhost:3457'})

    def test_linked_configuration_is_not_a_discovered_edit_target(self):
        other = self.home / 'unrelated.json'
        contents = b'{"theme":"unrelated"}'
        other.write_bytes(contents)
        os.link(other, self.pi / 'settings.json')
        with self.assertRaisesRegex(DeskError, 'not a plain, local file') as caught:
            self.settings.dispatch({'action': 'open', 'source': 'pi:user:settings'})
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(other.read_bytes(), contents)

    def test_control_routes_reject_missing_credentials_cross_origin_and_unconfirmed_writes(self):
        env = {'HOME': str(self.home), 'USERPROFILE': str(self.home)}
        with patch.dict(os.environ, env, clear=True), make_server(
                0, self.omp, self.project, native=False, operator=True) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                origin = f'http://127.0.0.1:{server.server_port}'
                def post(body, *, token=None, from_origin=origin):
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    try:
                        headers = {'Origin': from_origin, 'Content-Type': 'application/json'}
                        if token:
                            headers['X-Skellywix-Operator'] = token
                        conn.request('POST', '/operator/settings', json.dumps(body), headers)
                        response = conn.getresponse()
                        return response.status, response.read()
                    finally:
                        conn.close()
                self.assertEqual(post({'action': 'open'})[0], 403)
                token = server.operator_token
                self.assertEqual(post({'action': 'open'}, token=token, from_origin='https://other.example.invalid')[0], 403)
                status, wire = post({'action': 'open', 'source': 'pi:user:settings'}, token=token)
                self.assertEqual(status, 200, wire)
                current = json.loads(wire)
                self.assertEqual(post({'action': 'save', 'source': 'pi:user:settings', 'key': 'theme', 'value': 'light',
                                       'revision': current['revision']}, token=token)[0], 400)
                self.assertFalse((self.pi / 'settings.json').exists())
            finally:
                server.shutdown()
                worker.join(timeout=3)

    def test_hindsight_suggests_without_network_and_requests_only_the_selected_bank(self):
        get = Mock(side_effect=[
            {'banks': [{'bank_id': 'fixture-bank', 'name': 'Fixture bank'}], 'total': 1},
            {'bank_id': 'fixture-bank', 'total_nodes': 2, 'total_links': 1, 'total_documents': 1},
            {'nodes': [{'data': {'id': 'memory-1', 'text': 'first fact'}},
                       {'data': {'id': 'memory-2', 'text': 'second fact'}}],
             'edges': [{'data': {'source': 'memory-1', 'target': 'memory-2', 'linkType': 'semantic'}}],
             'total_units': 2},
            {'bank_id': 'fixture-bank', 'config': {'retain_extraction_mode': 'concise', 'api_key': 'excluded'}},
        ])
        desk = HindsightDesk(self.settings, {'HINDSIGHT_API_URL': 'http://127.0.0.1:8888',
                                              'HINDSIGHT_BANK_ID': 'fixture-bank'}, fetch=get)
        suggestion = desk.dispatch({'action': 'discover'})
        self.assertEqual(suggestion['candidate']['bank'], 'fixture-bank')
        get.assert_not_called()
        with self.assertRaises(DeskError):
            desk.dispatch({'action': 'connect', 'url': suggestion['candidate']['url']})
        get.assert_not_called()
        connection = desk.dispatch({'action': 'connect', 'url': suggestion['candidate']['url'], 'confirm': True})
        with self.assertRaises(DeskError):
            desk.dispatch({'action': 'bank', 'bank': 'not-listed', 'connection': connection['connection']})
        bank = desk.dispatch({'action': 'bank', 'bank': 'fixture-bank', 'connection': connection['connection']})
        self.assertEqual(len(bank['graph']['edges']), 1)
        self.assertEqual(bank['stats']['total_nodes'], 2)
        self.assertNotIn('excluded', json.dumps(bank))
        self.assertEqual([call.args[2].split('?')[0] for call in get.call_args_list], [
            '/v1/default/banks', '/v1/default/banks/fixture-bank/stats', '/v1/default/banks/fixture-bank/graph',
            '/v1/default/banks/fixture-bank/config'])

    def test_hindsight_discovers_a_bank_scoped_mcp_without_using_its_credentials(self):
        config = self.project / '.pi/mcp.json'
        config.write_text(json.dumps({'mcpServers': {
            'Hindsight': {'url': 'https://memory.example.test/service/mcp/fixture-bank/',
                          'headers': {'X-Bank-Id': 'fallback', 'Authorization': 'excluded'}},
            'Unrelated': {'url': 'https://unrelated.example.test/mcp/other-bank/'},
        }}), encoding='utf-8')
        get = Mock()
        discovered = HindsightDesk(self.settings, {}, fetch=get).dispatch({'action': 'discover'})
        self.assertEqual(discovered['candidates'], [{
            'url': 'https://memory.example.test/service', 'bank': 'fixture-bank',
            'source': 'Pi project · MCP · Hindsight MCP',
        }])
        get.assert_not_called()
