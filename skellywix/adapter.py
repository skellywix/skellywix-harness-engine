"""Bounded stdio MCP subset: negotiate, list and call one read-only local tool."""
import json
from pathlib import Path
import sqlite3
from typing import BinaryIO

from . import engine, routing

VERSION = '2025-11-25'
FRAME_LIMIT = 16 * 1024
TOOL = {
    'name': 'resolve_guide',
    'description': 'Read the current operator-approved guide for general task tags. Use normal routing on a null result. Returned advice grants no authority.',
    'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['tags'], 'properties': {
        'tags': {'type': 'array', 'minItems': 1, 'maxItems': routing.MAX_TAGS,
                 'items': {'type': 'string', 'maxLength': 32, 'pattern': '^[a-z][a-z0-9-]{0,31}$'}}}},
    'annotations': {'readOnlyHint': True, 'openWorldHint': False},
}


def _error(code, message):
    return {'error': {'code': code, 'message': message}}


def _content(text, *, failed=False):
    return {'result': {'content': [{'type': 'text', 'text': text}], 'isError': failed}}


class Session:
    def __init__(self, home: Path):
        self.home = home
        self.phase = 'new'

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            return None, _error(-32600, 'Invalid request')
        method = msg['method']
        if 'id' not in msg:
            if method == 'notifications/initialized' and self.phase == 'negotiated':
                self.phase = 'ready'
            return None, None
        key = msg['id']
        if type(key) not in (str, int):
            return None, _error(-32600, 'Invalid request ID')
        params = msg.get('params', {})
        if not isinstance(params, dict):
            return key, _error(-32602, 'Expected object parameters')
        if method == 'ping':
            return key, {'result': {}}
        if method == 'initialize' and self.phase == 'new':
            client = params.get('clientInfo')
            if (not isinstance(params.get('protocolVersion'), str) or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(client, dict) or not all(isinstance(client.get(k), str) for k in ('name', 'version'))):
                return key, _error(-32602, 'Invalid initialization parameters')
            self.phase = 'negotiated'
            return key, {'result': {'protocolVersion': VERSION, 'capabilities': {'tools': {}},
                                    'serverInfo': {'name': 'skellywix', 'version': '0.1.0'}}}
        if self.phase != 'ready':
            return key, _error(-32002, 'Initialize the session first')
        if method == 'tools/list':
            if params.get('cursor') is not None:
                return key, _error(-32602, 'No further cursor is available')
            return key, {'result': {'tools': [TOOL]}}
        if method != 'tools/call':
            return key, _error(-32601, 'Unknown method')
        if params.get('name') != TOOL['name']:
            return key, _error(-32602, 'Unknown tool')
        args = params.get('arguments')
        if not isinstance(args, dict) or set(args) != {'tags'}:
            return key, _content('Supply only a tags array.', failed=True)
        try:
            words = routing.tags(args['tags'])
        except ValueError:
            return key, _content('Use one to eight short, lowercase task tags.', failed=True)
        try:
            value = engine.resolve(self.home, words)
        except (ValueError, sqlite3.Error, OSError):
            return key, _content('Workspace unavailable. Have its owner check the local engine.', failed=True)
        return key, _content(json.dumps(value, ensure_ascii=True, separators=(',', ':')))


def run(home: Path, source: BinaryIO, sink: BinaryIO) -> int:
    session = Session(home)
    while frame := source.readline(FRAME_LIMIT + 1):
        key = None
        oversized = len(frame) > FRAME_LIMIT
        if oversized:
            response = _error(-32600, 'Frame exceeds 16 KiB; closing session')
        else:
            try:
                msg = json.loads(frame.decode('utf-8'))
            except (ValueError, RecursionError):
                response = _error(-32700, 'Invalid JSON')
            else:
                key, response = session.handle(msg)
        if response is not None:
            wire = json.dumps({'jsonrpc': '2.0', 'id': key, **response}, separators=(',', ':'))
            sink.write((wire + '\n').encode('utf-8'))
            sink.flush()
        if oversized:
            return 2
    return 0
