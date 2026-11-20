from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
from time import time
import unittest

from skellywix.neural import make_server
from skellywix.neural_inventory import Inventory
from skellywix.neural_trace import TraceReader


SECRET = 'sentinel-content-that-must-not-leave-source'


class NeuralMap(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.agent = self.root / 'agent'
        self.agent.mkdir()
        self.project = self.root / 'workspace'
        self.project.mkdir()
        self.sessions = self.agent / 'sessions'
        self.journals = self.sessions / 'scope'
        self.journals.mkdir(parents=True)
        self.at = datetime.now(timezone.utc).isoformat()

    def write_json(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding='utf-8')

    def append(self, name, ident, parent, event_type, **fields):
        path = self.journals / (name + '.jsonl')
        if not path.exists():
            with path.open('wb') as stream:
                stream.write(b'{"type":"session","version":3}\n')
        row = dict(type=event_type, id=ident, parentId=parent, timestamp=self.at, **fields)
        with path.open('ab') as stream:
            stream.write(json.dumps(row).encode() + b'\n')
        return path

    def prompt(self, name, ident, parent=None):
        self.append(name, ident, parent, 'message', message={'role': 'user', 'content': SECRET})

    def test_discovers_new_and_removed_entries_without_starting_servers(self):
        self.write_json(self.agent / 'mcp.json', {
            'mcpServers': {'Acorn': {'command': 'this-command-must-not-execute', 'env': {'SECRET': SECRET}},
                           'hidden': {'url': 'http://localhost'}},
            'disabledServers': ['hidden'],
        })
        self.write_json(self.project / '.omp' / 'mcp.json', {'mcpServers': {'bolt': {'url': 'http://localhost/api'}}})
        with closing(sqlite3.connect(self.agent / 'agent.db')) as db, db:
            db.execute('CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT, expires_at INTEGER)')
            for name, tool, expiration in [('Acorn', 'read_record', time() + 600), ('bolt', 'stale', time() - 60),
                                           ('retired-server', 'retired-tool', time() + 600)]:
                db.execute('INSERT INTO cache VALUES (?, ?, ?)', (f'mcp_tools:{name}', json.dumps(
                    {'version': 1, 'tools': [{'name': tool, 'description': SECRET}]}), expiration))
        inventory = Inventory(self.agent, self.project, native=False)
        first = inventory.scan()
        by_name = {node['name']: node for node in first['nodes']}
        self.assertEqual(by_name['hidden']['state'], 'disabled')
        self.assertIn('bolt', by_name)
        self.assertEqual(by_name['read_record']['state'], 'cached')
        self.assertIn(dict(source='mcp:Acorn', target='tool:mcp__acorn_read_record', label='Advertised tool'), first['edges'])
        self.assertFalse({'stale', 'retired-tool', 'retired-server'} & set(by_name))
        self.assertNotIn(SECRET, json.dumps(first))
        self.write_json(self.project / '.omp' / 'mcp.json', {'mcpServers': {'cedar': {'url': 'http://localhost'}}})
        names = {n['name'] for n in inventory.scan()['nodes']}
        self.assertNotIn('bolt', names)
        self.assertIn('cedar', names)

    def test_discovers_local_hook_rule_lint_and_private_relationships(self):
        hook = self.agent / 'hooks' / 'post' / 'check-scripts.ts'
        hook.parent.mkdir(parents=True)
        hook.write_text('pi.on("tool_result", f); pi.registerTool({name:"fresh_tool"});\n// ' + SECRET, encoding='utf-8')
        rule = self.agent / 'rules' / 'avoid-slip.md'
        rule.parent.mkdir()
        rule.write_text('---\nttsr:\n  condition: abc\n---\n' + SECRET, encoding='utf-8')
        (self.project / 'ruff.toml').touch()
        self.write_json(self.agent / 'lsp.json', {'servers': {'crystal': {'command': SECRET}}})
        self.write_json(self.root / 'private' / 'route.json', {
            'schema': 'harness-neural-insert/1',
            'nodes': [{'kind': 'automation', 'name': 'private-clock'}, {'kind': 'memory', 'name': 'private-bank'}],
            'edges': [{'source': 'automation:private-clock', 'target': 'memory:private-bank', 'label': 'Scheduled sync'},
                      {'source': 'automation:private-clock', 'target': 'missing:external', 'label': SECRET}],
            'notes': SECRET,
        })
        result = Inventory(self.agent, self.project, private=self.root / 'private', native=False).scan()
        keys = {n['id'] for n in result['nodes']}
        self.assertTrue({'hook:check-scripts', 'tool:fresh_tool', 'ttsr:avoid-slip', 'lint:ruff', 'lsp:crystal',
                         'automation:private-clock', 'memory:private-bank'} <= keys)
        self.assertIn(dict(source='automation:private-clock', target='memory:private-bank', label='Scheduled sync'), result['edges'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_real_journal_boundaries_and_incremental_tool_lifecycle(self):
        self.prompt('one', 'p1')
        self.prompt('two', 'p2')
        self.append('one', 'm1', 'p1', 'message', message={
            'role': 'assistant', 'provider': 'example', 'model': 'model-a', 'content': [
                {'type': 'toolCall', 'id': 'shared', 'name': 'write', 'arguments': {'path': 'xd://mcp__acorn_read_record', 'content': SECRET}},
                {'type': 'toolCall', 'id': 'not-run', 'name': 'unexecuted', 'arguments': {}},
            ], 'stopReason': 'toolUse', 'providerPayload': SECRET})
        self.append('one', 't1', 'm1', 'custom', customType='tool_execution_start',
                    data={'toolCallId': 'shared', 'toolName': 'write', 'args': {'path': 'xd://mcp__acorn_read_record'}})
        self.append('two', 't2', 'p2', 'custom', customType='tool_execution_start',
                    data={'toolCallId': 'shared', 'toolName': 'other_tool'})
        reader = TraceReader(self.sessions)
        traces = reader.scan()
        by_tool = {e['name']: (t, e) for t in traces for e in t['events'] if e['kind'] == 'tool'}
        self.assertNotIn('unexecuted', by_tool)
        self.assertNotEqual(by_tool['other_tool'][0]['id'], by_tool['mcp__acorn_read_record'][0]['id'])
        self.assertEqual(by_tool['mcp__acorn_read_record'][1]['state'], 'started')
        self.append('one', 'f1', 't1', 'message', message={
            'role': 'toolResult', 'toolCallId': 'shared', 'toolName': 'write', 'isError': True, 'content': SECRET})
        traces = reader.scan()
        by_tool = {e['name']: e['state'] for t in traces for e in t['events'] if e['kind'] == 'tool'}
        self.assertEqual(by_tool, {'mcp__acorn_read_record': 'error', 'other_tool': 'started'})
        projected = json.dumps(traces)
        for withheld in (SECRET, str(self.journals).replace('\\', '\\\\'), '"shared"', '"p1"'):
            self.assertNotIn(withheld, projected)

    def test_registered_plugin_is_discovered_and_disabled_plugin_cannot_win(self):
        registry = self.agent.parent / 'plugins'
        lock = registry / 'omp-plugins.lock.json'
        mcp = registry / 'node_modules' / 'sample-plugin' / '.mcp.json'
        self.write_json(lock, {'plugins': {'sample-plugin': {'enabled': True}}})
        self.write_json(mcp, {'mcpServers': {'sample-plugin-server': {'url': 'http://localhost'}}})
        inventory = Inventory(self.agent, self.project, native=False)
        self.assertIn('mcp:sample-plugin-server', {n['id'] for n in inventory.scan()['nodes']})
        self.write_json(lock, {'plugins': {'sample-plugin': {'enabled': False}}})
        self.assertNotIn('mcp:sample-plugin-server', {n['id'] for n in inventory.scan()['nodes']})

    def test_first_turn_recall_and_sibling_result_do_not_imply_wrong_calls(self):
        self.append('one', 'recall', None, 'custom', customType='memory_recall', data={'text': SECRET})
        self.append('one', 'prelude', 'recall', 'custom', customType='other')
        self.prompt('one', 'prompt', 'prelude')
        self.append('one', 'start', 'prompt', 'custom', customType='tool_execution_start',
                    data={'toolCallId': 'lookup', 'toolName': 'read', 'args': {'path': 'xd://mcp__acorn_read_record'}})
        self.append('one', 'result', 'prompt', 'message', message={
            'role': 'toolResult', 'toolCallId': 'lookup', 'toolName': 'read', 'isError': False})
        trace, = TraceReader(self.sessions).scan()
        self.assertEqual(
            [(event['kind'], event['name'], event['state']) for event in trace['events']],
            [('prompt', 'Prompt submitted', 'recorded'), ('memory', 'recall', 'injected'),
             ('tool', 'read', 'started'), ('tool', 'read', 'ok')])

    def test_rewind_follows_parent_and_reports_only_recorded_controls(self):
        self.prompt('session', 'first')
        self.append('session', 'memory', 'first', 'custom', customType='memory_recall', data={'text': SECRET})
        self.prompt('session', 'second', 'memory')
        self.append('session', 'rule', 'second', 'ttsr_injection', rules=['rule-a'])
        self.append('session', 'hook', 'memory', 'custom', customType='harness.guard_decision',
                    data={'guard_id': 'check-scripts', 'outcome': 'blocked', 'detail': SECRET})
        traces = TraceReader(self.sessions).scan()
        names = {(event['kind'], event['name']): trace['id'] for trace in traces for event in trace['events']}
        self.assertEqual(names['memory', 'recall'], names['hook', 'check-scripts'])
        self.assertNotEqual(names['ttsr', 'rule-a'], names['hook', 'check-scripts'])
        self.assertNotIn(SECRET, json.dumps(traces))

    def test_partial_and_oversize_lines_preserve_the_next_prompt(self):
        path = self.append('session', 'p1', None, 'message', message={'role': 'user'})
        reader = TraceReader(self.sessions)
        self.assertEqual(len(reader.scan()), 1)
        raw = json.dumps(dict(type='message', id='p2', parentId='p1', timestamp=self.at, message={'role': 'user'})).encode()
        with path.open('ab') as stream:
            stream.write(raw[:15])
        self.assertEqual(len(reader.scan()), 1)
        with path.open('ab') as stream:
            stream.write(raw[15:] + b'\n' + b'x' * (3 * 1024 * 1024) + b'\n')
        self.prompt('session', 'p3', 'p2')
        self.assertEqual(len(reader.scan()), 3)

    def test_loopback_http_refuses_foreign_and_cross_site_reads(self):
        self.prompt('session', 'root')
        with make_server(0, self.agent, self.project, native=False) as http:
            self.assertEqual(http.server_address[0], '127.0.0.1')
            thread = Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                for path, headers, status in [('/', {}, 200), ('/state', {}, 403),
                        ('/state', {'X-Skellywix-Map': '1', 'Host': 'foreign.invalid'}, 403),
                        ('/state', {'X-Skellywix-Map': '1', 'Sec-Fetch-Site': 'cross-site'}, 403),
                        ('/state', {'X-Skellywix-Map': '1', 'Origin': 'https://foreign.invalid'}, 403),
                        ('/state', {'X-Skellywix-Map': '1'}, 200),
                        ('/state?file=agent.db', {'X-Skellywix-Map': '1'}, 404)]:
                    with self.subTest(path=path, headers=headers), closing(HTTPConnection('127.0.0.1', http.server_port, timeout=3)) as conn:
                        conn.request('GET', path, headers=headers)
                        reply = conn.getresponse()
                        body = reply.read()
                        self.assertEqual(reply.status, status)
                        self.assertEqual(reply.getheader('Cache-Control'), 'no-store')
                        self.assertIsNone(reply.getheader('Access-Control-Allow-Origin'))
                        if path == '/state' and status == 200:
                            self.assertEqual(len(json.loads(body)['traces']), 1)
                            self.assertNotIn(SECRET.encode(), body)
            finally:
                http.shutdown()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
