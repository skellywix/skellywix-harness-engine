from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from skellywix import adapter, engine


def req(method, number, params):
    return {'jsonrpc': '2.0', 'method': method, 'id': number, 'params': params}


class AgentAdapter(unittest.TestCase):
    def test_consumer_negotiates_reads_a_real_guide_and_cannot_write(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'workspace'
            engine.initialize(home, demo=True)
            engine.run(home)
            initial = engine.status(home)
            init = {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'local-client', 'version': '1'}}
            messages = [req('tools/list', 1, {}), req('initialize', 2, init),
                        {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, req('tools/list', 3, {}),
                        req('tools/call', 4, {'name': 'resolve_guide', 'arguments': {'tags': ['flaky']}}),
                        req('tools/call', 5, {'name': 'feedback', 'arguments': {}}),
                        req('tools/call', 6, {'name': 'resolve_guide', 'arguments': {'tags': ['../other']}}),
                        req('tools/call', 7, {'name': 'resolve_guide', 'arguments': {'tags': ['test'], 'home': 'elsewhere'}})]
            source = BytesIO(b''.join(json.dumps(m).encode() + b'\n' for m in messages))
            sink = BytesIO()
            self.assertEqual(adapter.run(home, source, sink), 0)
            responses = {r['id']: r for r in map(json.loads, sink.getvalue().splitlines())}
            self.assertIn('error', responses[1])
            self.assertEqual(responses[2]['result']['capabilities'], {'tools': {}})
            self.assertEqual([t['name'] for t in responses[3]['result']['tools']], ['resolve_guide'])
            guide = json.loads(responses[4]['result']['content'][0]['text'])
            self.assertEqual((guide['revision'], guide['guide']['id']), (2, 'test-change'))
            self.assertIn('error', responses[5])
            for number in (6, 7):
                self.assertTrue(responses[number]['result']['isError'])
            self.assertEqual(engine.status(home), initial)

    def test_oversized_frame_stops_before_dispatch(self):
        sink = BytesIO()
        self.assertNotEqual(adapter.run(Path('.'), BytesIO(b'a' * 20000 + b'\n'), sink), 0)
        self.assertIn('error', json.loads(sink.getvalue()))
