"""Loopback-only, payload-free observer. This is separate from the shareable desk."""
from hashlib import blake2s
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from threading import Event, Lock, Thread
from time import monotonic

from .neural_inventory import Graph, Inventory
from .neural_trace import TraceReader

LOOPBACK = '127.0.0.1'
ASSETS = {
    '/': ('neural.html', 'text/html; charset=utf-8'),
    '/neural.css': ('neural.css', 'text/css; charset=utf-8'),
    '/neural.js': ('neural.js', 'text/javascript; charset=utf-8'),
    '/neural_scene.js': ('neural_scene.js', 'text/javascript; charset=utf-8'),
    '/operator.js': ('operator.js', 'text/javascript; charset=utf-8'),
    '/operator.css': ('operator.css', 'text/css; charset=utf-8'),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def active_dirs():
    config = Path(os.environ.get('PI_CONFIG_DIR') or Path.home() / '.omp').expanduser()
    name = os.environ.get('OMP_PROFILE', os.environ.get('PI_PROFILE', '')).strip()
    named = name and name != 'default'
    if named and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise ValueError('Cannot resolve the active OMP profile. Select an agent directory with --agent-dir.')
    relative = Path('profiles', name) if named else Path()
    default = config / relative / 'agent'
    agent = default if named else Path(os.environ.get('PI_CODING_AGENT_DIR') or default).expanduser()
    sessions = agent / 'sessions'
    # Prefer the category-specific directory only if it already exists.
    state = os.environ.get('XDG_STATE_HOME') if os.name != 'nt' else None
    if state:
        active = Path(state).expanduser() / 'omp' / relative
        if active.is_dir() and (named or agent == default):
            sessions = active / 'sessions'
    return agent.absolute(), sessions.absolute()


class Observer:
    def __init__(self, agent, project, private, sessions, native):
        self.inventory = Inventory(agent, project, private=private, native=native, user=Path.home())
        current = os.environ.get('PI_SESSION_FILE')
        self.reader = TraceReader(sessions, preferred=Path(current).absolute() if current else None)
        self.graph = Graph().result()
        self.status = 'Scanning local sources'
        self.stopped = Event()
        self.lock = Lock()
        self.next_trace_scan = 0.0
        self.last_snapshot = None
        self.wire = None

    def observe_inventory(self):
        while not self.stopped.is_set():
            result = self.inventory.scan()
            with self.lock:
                self.graph = result
                self.status = 'Watching local registrations'
                self.next_trace_scan = 0.0
            self.stopped.wait(12)

    def snapshot(self):
        with self.lock:
            now = monotonic()
            if now >= self.next_trace_scan:
                traces = self.reader.scan()
                view = {'inventory': self.graph, 'traces': traces,
                        'inventoryStatus': self.status, 'traceCoverage': self.reader.coverage}
                if view != self.last_snapshot:
                    data = json.dumps(view, ensure_ascii=True, separators=(',', ':')).encode('ascii')
                    self.wire = data, '"' + blake2s(data, digest_size=12).hexdigest() + '"'
                    self.last_snapshot = view
                self.next_trace_scan = now + 0.75
            return self.wire


class NeuralHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, observer, controls=None):
        root = Path(__file__).parent
        self.assets = {url: (media, (root / file).read_bytes()) for url, (file, media) in ASSETS.items()}
        self.observer = observer
        self.controls = controls
        self.operator_token = secrets.token_urlsafe(32) if controls else None
        super().__init__((LOOPBACK, port), MapRequest)

    def serve_forever(self, poll_interval=0.5):
        worker = Thread(name='neural-inventory', target=self.observer.observe_inventory, daemon=True)
        worker.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.observer.stopped.set()
            worker.join(timeout=0.3)

    def server_close(self):
        self.observer.stopped.set()
        super().server_close()


class MapRequest(BaseHTTPRequestHandler):
    server_version = 'Skellywix'
    sys_version = ''

    def setup(self):
        self.request.settimeout(4)
        super().setup()

    def log_message(self, format, *args):
        # Do not save visitor headers, paths or request text to logs.
        return

    def reply(self, code, media='text/plain; charset=utf-8', body=b'', etag=None):
        self.send_response(code)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if etag:
            self.send_header('ETag', etag)
        try:
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return  # A tab closed its own connection before the reply.

    def send_error(self, code, message=None, explain=None):
        self.reply(code, body=b'Request rejected')

    def do_POST(self):
        if not self.server.controls or self.path not in self.server.controls:
            return self.reply(404, body=b'Not an operator route')
        authority = f'{LOOPBACK}:{self.server.server_port}'
        token = self.headers.get_all('X-Skellywix-Operator', [])
        if (self.headers.get_all('Host', []) != [authority]
                or self.headers.get_all('Origin', []) != [f'http://{authority}']
                or self.headers.get_all('Sec-Fetch-Site', []) not in ([], ['same-origin'])
                or len(token) != 1 or not token[0].isascii()
                or not compare_digest(token[0], self.server.operator_token)):
            return self.reply(403, body=b'Private operator session required')
        if (self.headers.get_all('Content-Type', []) != ['application/json']
                or self.headers.get_all('Transfer-Encoding')):
            return self.reply(415, body=b'A fixed-length JSON request is required')
        from .settings_desk import DeskError, MAX_DOCUMENT, check_tree, read_json

        length = self.headers.get_all('Content-Length', [])
        if (len(length) != 1 or not re.fullmatch(r'[1-9][0-9]{0,6}', length[0])
                or int(length[0]) > MAX_DOCUMENT):
            return self.reply(413, body=b'Operator request exceeds the size limit')
        try:
            raw = self.rfile.read(int(length[0]))
            if len(raw) != int(length[0]):
                raise ValueError
            body = read_json(raw)
            check_tree(body)
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, OSError, RecursionError):
            return self.reply(400, 'application/json', b'{"error":"Invalid operator request"}')
        try:
            data = self.server.controls[self.path](body)
        except DeskError as error:
            return self.reply(error.code, 'application/json', json.dumps({'error': str(error)}).encode('ascii'))
        return self.reply(200, 'application/json', json.dumps(data, ensure_ascii=True, allow_nan=False).encode('ascii'))

    def do_GET(self):
        authority = f'{LOOPBACK}:{self.server.server_port}'
        host = self.headers.get_all('Host', [])
        origin = self.headers.get_all('Origin', [])
        site = self.headers.get_all('Sec-Fetch-Site', [])
        if (host != [authority] or origin not in ([], [f'http://{authority}'])
                or site not in ([], ['same-origin'], ['none'])):
            self.reply(403, body=b'Loopback viewer only')
        elif self.path == '/state':
            if self.headers.get_all('X-Skellywix-Map', []) != ['1'] or site == ['none']:
                self.reply(403, body=b'Viewer request required')
            else:
                data, etag = self.server.observer.snapshot()
                if self.headers.get('If-None-Match') == etag:
                    self.reply(304, 'application/json', etag=etag)
                else:
                    self.reply(200, 'application/json', data, etag=etag)
        elif asset := self.server.assets.get(self.path):
            self.reply(200, *asset)
        else:
            self.reply(404, body=b'Not a viewer route')


def make_server(port, agent_dir, project, *, private=None, sessions=None, native=True,
                operator=False, pi_agent=None, hindsight_http_origin=None):
    """Bind locally. Operator settings and Hindsight are not exposed by the MCP adapter."""
    agent_dir = agent_dir.expanduser().absolute()
    project = project.expanduser().absolute()
    if not project.is_dir():
        raise ValueError('The selected workspace is not a readable directory.')
    private = (private or agent_dir / 'neural-map.d').expanduser().absolute()
    sessions = (sessions or agent_dir / 'sessions').expanduser().absolute()
    controls = None
    if operator:
        from .hindsight_desk import HindsightDesk
        from .settings_catalog import load_dependencies
        from .settings_desk import SettingsDesk

        load_dependencies()
        env = os.environ.copy()
        pi_agent = Path(os.path.abspath(pi_agent.expanduser())) if pi_agent else None
        settings = SettingsDesk(agent_dir, project, Path.home(), env, native=native, pi_agent=pi_agent)
        hindsight = HindsightDesk(settings, env, http_origin=hindsight_http_origin)
        controls = {'/operator/settings': settings.dispatch, '/operator/hindsight': hindsight.dispatch}
    # Config probes against a live profile can migrate files; the operator uses an isolated registry instead.
    return NeuralHTTP(port, Observer(agent_dir, project, private, sessions, native and not operator), controls)


def serve(port, *, agent=None, project=None, private=None, sessions=None, native=True,
          operator=False, pi_agent=None, hindsight_http_origin=None):
    default_agent, default_sessions = active_dirs() if agent is None else (agent, agent / 'sessions')
    with make_server(port, default_agent, project or Path.cwd(), private=private,
                     sessions=sessions or default_sessions, native=native, operator=operator,
                     pi_agent=pi_agent, hindsight_http_origin=hindsight_http_origin) as server:
        url = f'http://{LOOPBACK}:{server.server_port}/'
        if server.operator_token:
            print(f'Private operator desk: {url}#operator={server.operator_token}', flush=True)
        else:
            print(f'Private neural map: {url}', flush=True)
        server.serve_forever()
