from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch

from skellywix import adapter
from skellywix.neural_inventory import Inventory


class Discovery(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.user = Path(temp.name) / 'user'
        self.agent = self.user / '.omp' / 'agent'
        self.project = Path(temp.name) / 'project'
        self.project.mkdir()
        # A nondefault alias and Hindsight host ensure lookup is not pinned to fixture names.
        self.endpoint = 'https://staging.example.invalid/prefix'
        self.save(self.agent / 'config.yml', f'memory:\n  backend: hindsight\nhindsight:\n  apiUrl: {self.endpoint}\n')
        self.save(self.agent / 'mcp.json', {'mcpServers': {
            'QuillBank': {'url': self.endpoint + '/mcp/QuillPrivateBank/', 'headers': {'Authorization': 'private-fixture-value'}},
        }})
        self.save(self.user / '.cursor/mcp.json', {'mcpServers': {
            'BirchIndex': {'command': 'uvx', 'args': ['mcp-server-qdrant'],
                           'env': {'QDRANT_URL': 'https://database.example.invalid', 'COLLECTION_NAME': 'PrivateCollection'}},
            'DisabledVector': {'command': 'mcp-server-qdrant', 'enabled': False},
            'QdrantNotes': {'url': 'https://docs.example.invalid/mcp', 'type': 'http'},
        }})

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')

    def catalog(self):
        with patch.dict(os.environ, {}, clear=True):
            return Inventory(self.agent, self.project, native=False, user=self.user).scan()

    def test_cold_catalog_connects_declared_modules_without_cache(self):
        state = self.catalog()
        nodes = {n['id']: n for n in state['nodes']}
        self.assertEqual(nodes['memory:native:hindsight']['name'], 'Hindsight - Memory')
        self.assertIn({'source': 'mcp:QuillBank', 'target': 'memory:native:hindsight',
                       'label': 'Same declared Hindsight API'}, state['edges'])
        vector, = [n for n in state['nodes'] if n['kind'] == 'vector']
        self.assertEqual((vector['name'], vector['state']), ('Qdrant - Vector Search', 'declared'))
        self.assertIn({'source': 'mcp:cursor/BirchIndex', 'target': vector['id'],
                       'label': 'Declares vector search'}, state['edges'])
        self.assertEqual(nodes['mcp:cursor/DisabledVector']['state'], 'disabled')
        self.assertEqual(state['coverage']['MCP tool cache'], 'No readable OMP cache')
        wire = json.dumps(state)
        for forbidden in (self.endpoint, 'private-fixture-value', 'database.example.invalid', 'PrivateCollection'):
            self.assertNotIn(forbidden, wire)

    def test_refresh_superimposes_project_policy_without_borrowing_cached_tools(self):
        self.save(self.project / '.omp/mcp.json', {'mcpServers': {'BirchIndex': {'command': 'differently-owned'}}})
        with closing(sqlite3.connect(self.agent / 'agent.db')) as db, db:
            db.execute('CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT, expires_at INTEGER)')
            db.execute('INSERT INTO cache VALUES (?, ?, ?)', ('mcp_tools:BirchIndex', json.dumps(
                {'version': 1, 'tools': [{'name': 'qdrant-find', 'description': 'private-fixture-value'}]}), time() + 3600))
        state = self.catalog()
        ids = {n['id'] for n in state['nodes']}
        self.assertTrue({'mcp:BirchIndex', 'mcp:cursor/BirchIndex'} <= ids)
        self.assertNotIn('tool:mcp__birchindex_qdrant_find', ids)
        self.save(self.project / '.omp/config.yml', 'mcp:\n  enableProjectConfig: false\n')
        self.save(self.user / '.cursor/mcp.json', {'mcpServers': {'SageMemory': {
            'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-memory'],
        }}})
        state = self.catalog()
        ids = {n['id'] for n in state['nodes']}
        self.assertFalse({'mcp:BirchIndex', 'mcp:cursor/BirchIndex', 'tool:mcp__birchindex_qdrant_find'} & ids)
        self.assertTrue(any(n['name'] == 'Knowledge Graph - Memory' for n in state['nodes']))

    def test_unsupported_memory_setting_does_not_fall_back_to_a_stale_backend(self):
        self.save(self.project / '.omp/config.yml', 'memory:\n  backend: [local]\n')
        state = self.catalog()
        self.assertFalse(any(n['id'].startswith('memory:native:') for n in state['nodes']))
        self.assertIn('unsupported', state['coverage']['Native memory'].lower())

    def test_mcp_discovery_refreshes_without_exposing_private_inventory(self):
        self.save(self.agent / 'neural-map.d/private.json', {'schema': 'harness-neural-insert/1',
            'nodes': [{'kind': 'memory', 'name': 'PrivateInsertName'}]})
        self.save(self.agent / 'sessions/private.jsonl', {'prompt': 'PrivateJournalContent'})
        params = {'protocolVersion': '2025-06-18', 'capabilities': {},
                  'clientInfo': {'name': 'fixture-client', 'version': '1'}}
        messages = [(1, 'initialize', params), (None, 'notifications/initialized', {}),
                    (2, 'tools/list', {}), (3, 'tools/call', {'name': 'harness_discovery'}),
                    (4, 'tools/call', {'name': 'harness_discovery', 'arguments': {'project': '../../other'}})]
        data = b''.join(json.dumps({'jsonrpc': '2.0', 'method': method, 'params': value,
                                    **({'id': key} if key is not None else {})}).encode() + b'\n'
                        for key, method, value in messages)

        def invoke():
            reply = BytesIO()
            with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.agent)}, clear=True), \
                    patch.object(Path, 'home', return_value=self.user), patch.object(Path, 'cwd', return_value=self.project):
                self.assertEqual(adapter.run(self.user / 'uninitialized', BytesIO(data), reply), 0)
            return {row['id']: row for row in map(json.loads, reply.getvalue().splitlines())}, reply.getvalue().decode()

        responses, wire = invoke()
        self.assertEqual(responses[1]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual({tool['name'] for tool in responses[2]['result']['tools']}, {'resolve_guide', 'harness_discovery'})
        self.assertTrue(responses[4]['result']['isError'])
        result = json.loads(responses[3]['result']['content'][0]['text'])
        self.assertEqual({row['module'] for row in result['modules']}, {'Hindsight - Memory', 'Qdrant - Vector Search'})
        self.assertEqual(result['mcp']['disabled'], 1)
        for forbidden in (str(self.agent), self.endpoint, 'Quill', 'Birch', 'PrivateInsert', 'PrivateJournal',
                          'database.example.invalid', 'PrivateCollection', 'private-fixture-value'):
            self.assertNotIn(forbidden, wire)
        self.assertFalse((self.user / 'uninitialized').exists())
        self.save(self.user / '.cursor/mcp.json', {'mcpServers': {}})
        responses, _ = invoke()
        result = json.loads(responses[3]['result']['content'][0]['text'])
        self.assertEqual([row['module'] for row in result['modules']], ['Hindsight - Memory'])
