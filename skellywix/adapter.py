"""Bounded stdio MCP adapter; supervisor authority is selected at launch."""
import json
import os
from pathlib import Path
import sqlite3
from typing import BinaryIO

from . import engine, routing
from .neural import active_dirs
from .neural_inventory import Inventory

VERSION = '2025-11-25'
SUPPORTED_VERSIONS = (VERSION, '2025-06-18', '2025-03-26', '2024-11-05')
FRAME_LIMIT = 16 * 1024
TOOL = {
    'name': 'resolve_guide',
    'description': 'Read the current operator-approved guide for general task tags. Use normal routing on a null result. Returned advice grants no authority.',
    'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['tags'], 'properties': {
        'tags': {'type': 'array', 'minItems': 1, 'maxItems': routing.MAX_TAGS,
                 'items': {'type': 'string', 'maxLength': 32, 'pattern': '^[a-z][a-z0-9-]{0,31}$'}}}},
    'annotations': {'readOnlyHint': True, 'openWorldHint': False},
}
DISCOVERY = {
    'name': 'harness_discovery',
    'description': 'Discover locally registered memory, vector search, and MCP capabilities. Returns product names and counts, not connection or activity evidence.',
    'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'openWorldHint': False},
}


def _error(code, message):
    return {'error': {'code': code, 'message': message}}


def _content(text, *, failed=False):
    return {'result': {'content': [{'type': 'text', 'text': text}], 'isError': failed}}

def _project_path(project):
    if project is None:
        raw = os.environ.get('CLAUDE_PROJECT_DIR')
        project = Path(raw) if raw else Path.cwd()
    return project.expanduser().absolute()


def _wire_value(value):
    return _content(json.dumps(value, ensure_ascii=True, separators=(',', ':')))


class Session:
    def __init__(self, home: Path, project: Path | None = None, *, supervisor=None):
        self.home = home
        self.project = project
        self.supervisor = supervisor
        self.privileged_tools = {tool['name']: tool for tool in supervisor.tools} if supervisor else {}
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
            requested = params['protocolVersion']
            version = requested if requested in SUPPORTED_VERSIONS else VERSION
            return key, {'result': {'protocolVersion': version, 'capabilities': {'tools': {}},
                                    'serverInfo': {'name': 'skellywix', 'version': '0.1.0'}}}
        if self.phase != 'ready':
            return key, _error(-32002, 'Initialize the session first')
        if method == 'tools/list':
            if params.get('cursor') is not None:
                return key, _error(-32602, 'No further cursor is available')
            return key, {'result': {'tools': [TOOL, DISCOVERY, *self.privileged_tools.values()]}}
        if method != 'tools/call':
            return key, _error(-32601, 'Unknown method')
        name = params.get('name')
        if isinstance(name, str) and name in self.privileged_tools:
            try:
                return key, _wire_value(self.supervisor.call(name, params.get('arguments', {})))
            except (ValueError, RecursionError):
                return key, _content('The supervisor rejected the request. Check the tool input schema.', failed=True)
            except (OSError, RuntimeError, sqlite3.Error):
                return key, _content('The local supervisor could not complete the operation.', failed=True)
        if name == DISCOVERY['name']:
            return key, self.discover(params.get('arguments', {}))
        if name != TOOL['name']:
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
        return key, _wire_value(value)

    def discover(self, args):
        if not isinstance(args, dict) or args:
            return _content('This tool takes no arguments.', failed=True)
        try:
            project = _project_path(self.project)
            if not project.is_dir():
                return _content('The launch directory is unavailable. Check the MCP registration.', failed=True)
            agent, _ = active_dirs()
            result = Inventory(agent, project, native=False, user=Path.home()).summary()
        except (OSError, RuntimeError, ValueError):
            return _content('Local discovery is unavailable. Check the launch profile and MCP registration.', failed=True)
        return _wire_value(result)


def run(home: Path, source: BinaryIO, sink: BinaryIO, *, project: Path | None = None,
        supervisor_policy: Path | None = None, pi_agent: Path | None = None) -> int:
    supervisor = None
    if supervisor_policy is not None:
        from .supervisor import Supervisor

        agent, _ = active_dirs()
        user = Path.home()
        pi = pi_agent or user / '.pi' / 'agent'
        supervisor = Supervisor(supervisor_policy, agent, _project_path(project), user,
                                pi_agent=pi.expanduser().absolute())
    session = Session(home, project, supervisor=supervisor)
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
