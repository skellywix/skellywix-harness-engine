from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from skellywix.adapter import Session
from skellywix.supervisor import Supervisor


REPO = Path(__file__).resolve().parents[1]
SCHEMA = 'skellywix-supervisor/1'


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.user = self.root / 'user'
        self.agent = self.user / '.omp' / 'agent'
        self.pi = self.user / '.pi' / 'agent'
        self.project = self.root / 'project'
        self.project.mkdir()
        self.policy = self.root / 'approvals' / 'supervisor.json'
        self.env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
        self.env.update(HOME=str(self.user), USERPROFILE=str(self.user), PI_CONFIG_DIR=str(self.agent.parent),
                        PI_CODING_AGENT_DIR=str(self.agent))
        self.save(self.policy, {'schema': SCHEMA})

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8', newline='\n')

    def exchange(self, calls, *, policy=True):
        def message(i, method, params):
            return {'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}

        frames = [message(1, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                            'clientInfo': {'name': 'supervisor-test', 'version': '1'}}),
                  {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, message(2, 'tools/list', {})]
        frames.extend(message(i, 'tools/call', {'name': name, 'arguments': args})
                      for i, (name, args) in enumerate(calls, 3))
        invocation = [sys.executable, '-B', '-m', 'skellywix', '--home', str(self.root / 'engine'), 'mcp',
                      '--project', str(self.project)]
        if policy:
            invocation += ['--supervisor-policy', str(self.policy)]
        return subprocess.run(invocation, input='\n'.join(map(json.dumps, frames)) + '\n', text=True,
                              encoding='utf-8', capture_output=True, timeout=18, cwd=REPO, env=self.env)

    def call(self, *calls, policy=True):
        run = self.exchange(calls, policy=policy)
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = {entry['id']: entry for entry in map(json.loads, run.stdout.splitlines())}
        self.assertEqual(rows[1]['result']['serverInfo']['name'], 'skellywix')
        return rows[2]['result']['tools'], [rows[i] for i in range(3, 3 + len(calls))]

    def value(self, response):
        result = response['result']
        self.assertFalse(result['isError'], result['content'])
        return json.loads(result['content'][0]['text'])

    def test_default_mcp_cannot_call_supervisor_tools(self):
        tools, (reply,) = self.call(('harness_inventory', {}), policy=False)
        self.assertEqual([tool['name'] for tool in tools], ['resolve_guide', 'harness_discovery'])
        self.assertEqual(reply['error']['code'], -32602)

    def test_inventory_pages_configured_surfaces_without_leaking_content(self):
        self.save(self.agent / 'skills/trace-review/SKILL.md',
                  '---\nname: trace-review\ndescription: SECRET-DESCRIPTION\n---\nprivate skill body\n')
        self.save(self.pi / 'skills/pi-formatter/SKILL.md', '---\nname: pi-formatter\ndescription: Format\n---\n')
        self.save(self.pi / 'mcp.json', {'mcpServers': {'pi-private': {'url': 'https://private.example.invalid/mcp',
                  'headers': {'Authorization': 'SECRET-TOKEN'}}}})
        self.save(self.project / '.pi/mcp.json', {'mcpServers': {'pi-private': {'enabled': False}}})
        self.save(self.agent / 'rules/check-review.md', '---\nttsr: cite\n---\nprivate rule body\n')
        self.save(self.agent / 'tools/check-state.ts', 'kit.registerTool({name: "check_state"});')
        self.save(self.project / '.omp/lsp.json', {'servers': {'fixture-lsp': {'command': 'must-not-launch'}}})
        requests = [('harness_inventory', {'kind': 'skill', 'limit': 1}),
                    ('harness_inventory', {'kind': 'skill', 'limit': 1, 'cursor': 1}),
                    ('harness_inventory', {})]
        tools, rows = self.call(*requests)
        self.assertIn('harness_inventory', [tool['name'] for tool in tools])
        first, second, full = map(self.value, rows)
        self.assertEqual([first['nextCursor'], second['nextCursor']], [1, None])
        self.assertEqual([page['nodes'][0]['name'] for page in (first, second)], ['pi-formatter', 'trace-review'])
        records = {(n['kind'], n['name']): n['state'] for n in full['nodes']}
        self.assertEqual(records['mcp', 'pi-private'], 'disabled')
        self.assertEqual(records['tool', 'check_state'], 'declared')
        self.assertEqual(records['ttsr', 'check-review'], 'declared')
        self.assertEqual(records['lsp', 'fixture-lsp'], 'declared')
        self.assertNotIn('connected', records.values())
        wire = json.dumps(full)
        for private in ('SECRET-', 'private skill body', 'private rule body', 'must-not-launch',
                        'https://private.example.invalid', str(self.root)):
            self.assertNotIn(json.dumps(private)[1:-1], wire)

    def endpoint(self, respond):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.command, self.path, self.headers.get('Authorization'),
                                 self.headers.get('api-key'), self.headers.get('X-Native-Only')))
                status, value, headers = respond(self.path)
                body = json.dumps(value).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                for name, item in headers.items():
                    self.send_header(name, item)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.timeout = 1
        worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        worker.start()

        def stop():
            server.shutdown()
            server.server_close()
            worker.join()

        self.addCleanup(stop)
        return f'http://127.0.0.1:{server.server_port}', received

    @staticmethod
    def results(page):
        return {row['module']: (row['state'], row.get('reason')) for row in page['connections']}

    def test_exact_startup_probes_reconnect_without_using_native_credentials(self):
        bank, bank_calls = self.endpoint(lambda _: (200, {'bank_id': 'fixture-bank', 'total_nodes': 0}, {}))
        vector, vector_calls = self.endpoint(lambda _: (200, {'status': 'ok', 'result': {
            'status': 'yellow', 'segments_count': 1, 'config': {}}}, {}))
        bank += '/memory'
        self.save(self.agent / 'config.yml', f'memory:\n  backend: hindsight\nhindsight:\n  apiUrl: {bank}\n')
        self.save(self.agent / 'mcp.json', {'mcpServers': {
            'vector': {'command': 'mcp-server-qdrant', 'env': {'QDRANT_URL': vector,
                        'QDRANT_API_KEY': 'NATIVE-VECTOR'}, 'headers': {'X-Native-Only': 'NATIVE-HEADER'}}}})
        self.env.update(FIXTURE_BANK_KEY='FIXTURE-BANK-KEY', FIXTURE_VECTOR_KEY='FIXTURE-VECTOR-KEY')
        self.save(self.policy, {'schema': SCHEMA, 'connections': [
            {'module': 'hindsight', 'baseUrl': bank, 'scope': 'fixture-bank', 'credentialEnv': 'FIXTURE_BANK_KEY'},
            {'module': 'qdrant', 'baseUrl': vector, 'scope': 'fixture-vector', 'credentialEnv': 'FIXTURE_VECTOR_KEY'}]})

        self.call()  # Connection checks run even when the client only lists tools.
        _, (response,) = self.call(('harness_inventory', {}))
        page = self.value(response)
        self.assertEqual(self.results(page), {'hindsight': ('connected', None), 'qdrant': ('connected', None)})
        self.assertEqual(bank_calls, [('GET', '/memory/v1/default/banks/fixture-bank/stats',
                                      'Bearer FIXTURE-BANK-KEY', None, None)] * 2)
        self.assertEqual(vector_calls, [('GET', '/collections/fixture-vector', None, 'FIXTURE-VECTOR-KEY', None)] * 2)
        for private in (bank, vector, 'FIXTURE-', 'NATIVE-', 'fixture-bank', 'fixture-vector'):
            self.assertNotIn(private, json.dumps(page))

    def test_redirects_and_auth_errors_never_count_as_connected(self):
        unapproved, unapproved_calls = self.endpoint(lambda _: (200, {'bank_id': 'test', 'total_nodes': 0}, {}))
        bank, bank_calls = self.endpoint(lambda _: (302, {}, {'Location': unapproved}))
        vector, vector_calls = self.endpoint(lambda _: (403, {'error': 'PRIVATE-AUTH-RESPONSE'}, {}))
        self.env.update(HINDSIGHT_API_URL=bank, QDRANT_URL=vector)
        self.save(self.policy, {'schema': SCHEMA, 'connections': [
            {'module': 'hindsight', 'baseUrl': bank, 'scope': 'test'},
            {'module': 'qdrant', 'baseUrl': vector, 'scope': 'test'}]})

        _, (response,) = self.call(('harness_inventory', {}))
        self.assertEqual(self.results(self.value(response)), {
            'hindsight': ('error', 'redirect_blocked'), 'qdrant': ('error', 'denied')})
        self.assertEqual(len(bank_calls), 1)
        self.assertEqual(len(vector_calls), 1)
        self.assertEqual(unapproved_calls, [])
        self.assertNotIn('PRIVATE-AUTH-RESPONSE', json.dumps(response))

    def test_stale_or_unapproved_declarations_do_not_make_requests(self):
        stale, stale_calls = self.endpoint(lambda _: (200, {}, {}))
        found, found_calls = self.endpoint(lambda _: (200, {}, {}))
        self.save(self.agent / 'mcp.json', {'mcpServers': {
            'stale-vector': {'command': 'mcp-server-qdrant', 'env': {'QDRANT_URL': stale}},
            'current-vector': {'command': 'mcp-server-qdrant', 'env': {'QDRANT_URL': found}}},
            'disabledServers': ['stale-vector']})
        self.save(self.policy, {'schema': SCHEMA, 'connections': [
            {'module': 'qdrant', 'baseUrl': stale, 'scope': 'test'}]})
        _, (response,) = self.call(('harness_inventory', {}))
        self.assertEqual(self.results(self.value(response))['qdrant'], ('error', 'declaration_missing'))
        self.assertEqual(stale_calls + found_calls, [])

    def test_ok_response_for_other_scope_or_bad_schema_is_not_connected(self):
        bank, bank_calls = self.endpoint(lambda _: (200, {'bank_id': 'another-bank', 'total_nodes': 1}, {}))
        vector, vector_calls = self.endpoint(lambda _: (200, {'status': 'ok', 'result': {'status': 'green'}}, {}))
        self.env.update(HINDSIGHT_API_URL=bank, QDRANT_URL=vector)
        self.save(self.policy, {'schema': SCHEMA, 'connections': [
            {'module': 'hindsight', 'baseUrl': bank, 'scope': 'test'},
            {'module': 'qdrant', 'baseUrl': vector, 'scope': 'test'}]})
        _, (response,) = self.call(('harness_inventory', {}))
        self.assertEqual(self.results(self.value(response)), {
            'hindsight': ('error', 'unexpected_response'), 'qdrant': ('error', 'unexpected_response')})
        self.assertEqual([len(bank_calls), len(vector_calls)], [1, 1])

    def test_invalid_connection_policy_fails_before_any_probe(self):
        allowed, received = self.endpoint(lambda _: (200, {'bank_id': 'test', 'total_nodes': 0}, {}))
        self.env['HINDSIGHT_API_URL'] = allowed
        for base in ('http://example.invalid', 'https://example.invalid/@bad', allowed + '/%2e%2e', allowed + '?'):
            with self.subTest(base=base):
                self.save(self.policy, {'schema': SCHEMA, 'connections': [
                    {'module': 'hindsight', 'baseUrl': allowed, 'scope': 'test'},
                    {'module': 'qdrant', 'baseUrl': base, 'scope': 'test'}]})
                run = self.exchange([])
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertEqual(run.stdout, '')
                self.assertEqual(received, [])

    @staticmethod
    def approval(name, path, **operation):
        return {'id': name, 'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(), **operation}

    def approve(self, *rows):
        self.save(self.policy, {'schema': SCHEMA, 'repairs': list(rows)})

    def repair_session(self):
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        supervisor = Supervisor(self.policy, self.agent, self.project, self.user, pi_agent=self.pi)
        session = Session(self.root / 'engine', self.project, supervisor=supervisor)
        session.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                   'clientInfo': {'name': 'supervisor-test', 'version': '1'}}})
        session.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

        def call(tool, **arguments):
            _, response = session.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                          'params': {'name': tool, 'arguments': arguments}})
            return response

        return call

    def repairs(self, call):
        return {item['id']: item for item in self.value(call('harness_repairs'))['repairs']}

    def test_approved_automatic_preference_and_explicit_update_undo(self):
        preference = self.pi / 'settings.json'
        original = {'quietStartup': False, 'hideThinkingBlock': True}
        skill = self.agent / 'skills' / 'fixture-skill' / 'SKILL.md'
        self.save(preference, original)
        before = b'---\r\nname: fixture-skill\r\ndescription: PRIVATE-BEFORE\r\n---\r\nReady.\r\n'
        skill.parent.mkdir(parents=True)
        skill.write_bytes(before)
        after = '---\nname: fixture-skill\ndescription: PRIVATE-AFTER\n---\nReviewed.\n'
        self.approve(self.approval('quiet', preference, set={'field': ['quietStartup'], 'value': True}, automatic=True),
                     self.approval('skill', skill, replacement=after))
        call = self.repair_session()
        self.assertEqual(json.loads(preference.read_bytes()), {**original, 'quietStartup': True})
        self.assertEqual(skill.read_bytes(), before)
        items = self.repairs(call)
        self.assertEqual(items['quiet']['state'], 'applied')
        self.assertEqual(items['skill']['state'], 'ready')
        self.assertEqual(items['quiet']['automatic'], True)
        denied = call('harness_apply_repair', id='skill', revision=items['skill']['revision'], replacement='arbitrary')
        self.assertTrue(denied['result']['isError'])
        self.assertEqual(skill.read_bytes(), before)

        applied = self.value(call('harness_apply_repair', id='skill', revision=items['skill']['revision']))
        self.assertEqual(applied['decision'], 'applied')
        self.assertEqual(skill.read_bytes(), after.encode('utf-8'))
        undone = self.value(call('harness_undo_repair', id='skill', revision=applied['revision']))
        self.assertEqual(undone['decision'], 'undone')
        self.assertEqual(skill.read_bytes(), before)
        self.assertEqual(self.repairs(call)['skill']['state'], 'undone')
        revision = self.repairs(call)['skill']['revision']
        for _ in range(2):
            result = self.value(call('harness_apply_repair', id='skill', revision=revision))
            self.assertEqual((result['decision'], result['reason']), ('blocked', 'repair_consumed'))
        self.assertEqual(skill.read_bytes(), before)
        wire = json.dumps([items, applied, undone])
        for hidden in (str(self.root), 'PRIVATE-', sha256(before).hexdigest()):
            self.assertNotIn(json.dumps(hidden)[1:-1], wire)

    def test_approved_updates_across_config_and_module_surfaces(self):
        profile = self.agent.parent / 'profiles' / 'fixture' / 'agent'
        config = (
            ('mcp', self.agent / 'mcp.json', {'mcpServers': {'fixture': {'command': 'do-not-run',
             'headers': {'X-Private': 'SECRET-PRESERVED'}}}}, ['mcpServers', 'fixture', 'timeout'], 7000),
            ('lsp', self.project / '.omp/lsp.json', {'servers': {'fixture': {'command': 'do-not-run'}}},
             ['servers', 'fixture', 'disabled'], True),
            ('binding', self.pi / 'keybindings.json', {'tui.editor.deleteWordBackward': 'alt+backspace'},
             ['tui.editor.deleteWordBackward'], 'ctrl+w'),
        )
        patches = []
        for name, path, raw, field, value in config:
            self.save(path, raw)
            patches.append(self.approval(name, path, set={'field': field, 'value': value}))
        model = profile / 'models.yml'
        self.save(model, 'providers:\n  demo:\n    modelOverrides:\n      lab:\n        maxTokens: 256\n')
        patches.append(self.approval('model', model, set={'field': ['providers', 'demo', 'modelOverrides', 'lab',
                                                                   'maxTokens'], 'value': 512}))
        modules = (
            ('ttsr', self.agent / 'rules/cite.md', '---\nttsr: citation\n---\nCheck the source.\n'),
            ('hook', self.agent / 'hooks/pre/notice.ts', 'export const notice = \"reviewed\";\n'),
            ('tool', self.agent / 'tools/check.ts', 'export const check = \"reviewed\";\n'),
            ('extension', self.pi / 'extensions/help.ts', 'export const title = \"reviewed\";\n'),
        )
        for name, path, replacement in modules:
            self.save(path, replacement.replace('reviewed', 'original').replace('Check', 'Read'))
            patches.append(self.approval(name, path, replacement=replacement))
        self.approve(*patches)
        call = self.repair_session()
        approvals = self.repairs(call)
        for row in patches:
            with self.subTest(id=row['id']):
                receipt = self.value(call('harness_apply_repair', id=row['id'], revision=approvals[row['id']]['revision']))
                self.assertEqual(receipt['decision'], 'applied', receipt)
        for name, path, _, field, value in config:
            with self.subTest(id=name):
                current = json.loads(path.read_bytes())
                for key in field:
                    current = current[key]
                self.assertEqual(current, value)
        self.assertIn('SECRET-PRESERVED', (self.agent / 'mcp.json').read_text(encoding='utf-8'))
        self.assertIn('maxTokens: 512', model.read_text(encoding='utf-8'))
        for name, path, replacement in modules:
            with self.subTest(id=name):
                self.assertEqual(path.read_bytes(), replacement.encode('utf-8'))

    def test_repair_refuses_changed_preimage_linked_file_and_stale_undo(self):
        edited = self.agent / 'tools/edited.ts'
        linked = self.agent / 'tools/linked.ts'
        undo = self.agent / 'tools/undo.ts'
        outside = self.root / 'untouched.ts'
        for target in (edited, linked, undo, outside):
            self.save(target, '// original\n')
        self.approve(*(self.approval(p.stem, p, replacement='// approved\n') for p in (edited, linked, undo)))
        linked.unlink()
        os.link(outside, linked)
        call = self.repair_session()
        items = self.repairs(call)
        self.assertEqual(items['linked']['reason'], 'target_unavailable')
        self.assertEqual(self.value(call('harness_apply_repair', id='unknown', revision=items['edited']['revision']))
                         ['decision'], 'blocked')

        self.save(edited, '// newer edit\n')
        result = self.value(call('harness_apply_repair', id='edited', revision=items['edited']['revision']))
        self.assertEqual(result['decision'], 'blocked')
        self.assertEqual(self.repairs(call)['edited']['reason'], 'preimage_mismatch')
        self.assertEqual(edited.read_bytes(), b'// newer edit\n')
        self.assertEqual(outside.read_bytes(), b'// original\n')

        applied = self.value(call('harness_apply_repair', id='undo', revision=items['undo']['revision']))
        self.assertEqual(applied['decision'], 'applied')
        self.save(undo, '// after apply\n')
        result = self.value(call('harness_undo_repair', id='undo', revision=applied['revision']))
        self.assertEqual(result['decision'], 'blocked')
        self.assertEqual(undo.read_bytes(), b'// after apply\n')

    def test_out_of_scope_and_invalid_approvals_never_write(self):
        preference = self.pi / 'settings.json'
        project_settings = self.project / '.pi/settings.json'
        tool = self.agent / 'tools/module.ts'
        auth = self.agent / 'auth.json'
        rule = self.agent / 'rules/review.md'
        for path, value in ((preference, {'quietStartup': False}), (auth, {}), (tool, '// original\n'),
                            (rule, '---\nttsr: review\n---\nRead the diff.\n')):
            self.save(path, value)
        self.save(project_settings, {'quietStartup': False})
        safe = self.approval('safe', preference, set={'field': ['quietStartup'], 'value': True}, automatic=True)
        invalid = (
            self.approval('auth', auth, replacement='{}'),
            self.approval('tool', tool, replacement='// newer\n', automatic=True),
            self.approval('bad-rule', rule, replacement='missing front matter'),
            self.approval('trust', project_settings, set={'field': ['defaultProjectTrust'], 'value': 'always'}),
            self.approval('type', project_settings, set={'field': ['quietStartup'], 'value': 'true'}),
            {**self.approval('reshape', tool, replacement='// newer\n'), 'path': str(tool.parent / '..' / 'tools' / tool.name)},
        )
        for invalid_row in invalid:
            with self.subTest(id=invalid_row['id']):
                self.approve(safe, invalid_row)
                run = self.exchange([])
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn('owner-provided policy', run.stderr)
                self.assertEqual(json.loads(preference.read_bytes()), {'quietStartup': False})
        self.assertEqual(tool.read_bytes(), b'// original\n')

    def test_revoking_launch_policy_blocks_an_old_revision(self):
        tool = self.agent / 'tools/module.ts'
        self.save(tool, '// original\n')
        self.approve(self.approval('tool', tool, replacement='// approved\n'))
        call = self.repair_session()
        revision = self.repairs(call)['tool']['revision']
        self.approve()
        receipt = self.value(call('harness_apply_repair', id='tool', revision=revision))
        self.assertEqual((receipt['decision'], receipt['reason']), ('blocked', 'policy_changed'))
        self.assertEqual(tool.read_bytes(), b'// original\n')
