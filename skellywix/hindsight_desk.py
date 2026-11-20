"""A private, read-only view of an explicitly selected Hindsight data plane."""
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from ipaddress import ip_address
import re
import secrets
from threading import Lock
from urllib.parse import quote, urlencode, urlsplit

from .neural_inventory import MCP_PATH, remote_url
from .settings_catalog import DeskError, masked
from .settings_desk import MAX_DOCUMENT, check_tree, read_json

BANK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z')
HOST = re.compile(r'(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z')
PREFIX = re.compile(r'(?:/[A-Za-z0-9._~-]+)*\Z')
BANK_LIMIT = 100
GRAPH_LIMIT = 160
BANKS = '/v1/default/banks'


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int | None
    path: str

    @property
    def origin(self):
        host = f'[{self.host}]' if ':' in self.host else self.host
        port = f':{self.port}' if self.port is not None else ''
        return f'{self.scheme}://{host}{port}'

    @property
    def url(self):
        return self.origin + self.path

    @property
    def loopback(self):
        try:
            return ip_address(self.host).is_loopback
        except ValueError:
            return False


def endpoint(value):
    try:
        if not isinstance(value, str) or len(value) > 1024 or any(not 33 <= ord(c) <= 126 for c in value):
            raise ValueError
        url = urlsplit(value)
        host = url.hostname
        if (url.scheme not in ('http', 'https') or not host or url.username is not None or url.password is not None or
                '?' in value or '#' in value or '\\' in value or not PREFIX.fullmatch(url.path.rstrip('/')) or
                any(part in ('.', '..') for part in url.path.split('/')) or url.netloc.endswith(':')):
            raise ValueError
        try:
            address = ip_address(host)
        except ValueError:
            if not HOST.fullmatch(host) or '..' in host:
                raise ValueError from None
        else:
            if address.is_unspecified or '%' in host:
                raise ValueError
            host = str(address)
        if url.scheme == 'http' and host == 'localhost':
            host = '127.0.0.1'
        port = url.port
        if port == 0:
            raise ValueError
        return Endpoint(url.scheme, host, port, url.path.rstrip('/'))
    except ValueError:
        raise DeskError('Enter a Hindsight API base URL without credentials, query, or fragment.') from None


def accepted_bank(value):
    return value if isinstance(value, str) and BANK_ID.fullmatch(value) else ''


def fetch(target, token, route):
    client = (HTTPSConnection if target.scheme == 'https' else HTTPConnection)(target.host, target.port, timeout=5)
    try:
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        client.request('GET', target.path + route, headers=headers)
        response = client.getresponse()
        if response.status in (401, 403):
            raise DeskError('Hindsight did not authorize this request. Check the API key and bank permissions.', 401)
        if 300 <= response.status < 400:
            raise DeskError('Hindsight returned a redirect. Confirm its final API URL directly.', 502)
        if response.status != 200:
            raise DeskError(f'Hindsight returned HTTP {response.status} for this resource.', 502)
        if response.getheader('Content-Type', '').split(';', 1)[0].lower().strip() != 'application/json':
            raise DeskError('The endpoint did not return a Hindsight JSON response.', 502)
        raw = response.read(MAX_DOCUMENT + 1)
        if len(raw) > MAX_DOCUMENT:
            raise DeskError('Hindsight returned more data than the dashboard can display.', 502)
        data = read_json(raw)
        check_tree(data)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, RecursionError) as error:
        if isinstance(error, DeskError):
            raise
        raise DeskError('Hindsight returned invalid data for this resource.', 502) from None
    except (OSError, HTTPException):
        raise DeskError('Could not reach the confirmed Hindsight API. Check its address and TLS.', 502) from None
    finally:
        client.close()


def object_value(value):
    return value if isinstance(value, dict) else {}


def clipped(value, size):
    return value[:size] if isinstance(value, str) else ''


def count(value):
    return value if type(value) is int and 0 <= value <= 2**53-1 else None


def graph_data(data, mode):
    if not isinstance(data.get('nodes'), list) or not isinstance(data.get('edges'), list):
        raise DeskError('This server did not return a supported Hindsight graph response.', 502)
    nodes = {}
    for record in data['nodes'][:GRAPH_LIMIT]:
        row = object_value(object_value(record).get('data'))
        ident = clipped(row.get('id'), 250)
        if ident and ident not in nodes:
            nodes[ident] = {'id': ident, 'label': clipped(row.get('label'), 140) or ident,
                            'text': clipped(row.get('text'), 1200), 'date': clipped(row.get('date'), 48),
                            'mentions': count(row.get('mentionCount'))}
    edges = []
    for record in data['edges'][:GRAPH_LIMIT * 4]:
        row = object_value(object_value(record).get('data'))
        source, target = clipped(row.get('source'), 250), clipped(row.get('target'), 250)
        if source in nodes and target in nodes and source != target:
            edges.append({'source': source, 'target': target, 'kind': clipped(row.get('linkType'), 64) or 'link',
                          'entity': clipped(row.get('entityName'), 140)})
    return {'nodes': list(nodes.values()), 'edges': edges,
            'total': count(data.get('total_entities' if mode == 'entities' else 'total_units'))}


class HindsightDesk:
    def __init__(self, settings, env, *, fetch=fetch, http_origin=None):
        self.settings = settings
        self.env = env
        self.fetch = fetch
        self.http_origin = None
        if http_origin:
            pin = endpoint(http_origin)
            if pin.scheme != 'http' or pin.path:
                raise DeskError('The HTTP pin must be an exact HTTP origin, without a path.')
            self.http_origin = pin.origin
        self.lock = Lock()
        self.session = None

    def discover(self):
        values = {}
        native_origin = 'OMP settings'
        candidates = []
        for source, loaded in self.settings.hints():
            if source.document == 'settings':
                values.update(hindsight for hindsight in object_value(loaded.get('hindsight')).items()
                              if hindsight[0] in ('apiUrl', 'bankId') and hindsight[1] is not None)
                memory = object_value(loaded.get('memory'))
                if 'backend' in memory:
                    values['backend'] = memory['backend']
                if 'apiUrl' in object_value(loaded.get('hindsight')):
                    native_origin = source.label
                continue
            for name, registration in object_value(loaded.get('mcpServers')).items():
                row = object_value(registration)
                if row.get('enabled') is False:
                    continue
                try:
                    target = endpoint(remote_url(row))
                except DeskError:
                    continue
                header = object_value(row.get('headers') or row.get('http_headers'))
                bank = next((accepted_bank(v) for k, v in header.items()
                             if k.lower() in ('x-bank-id', 'x-hindsight-bank-id')), '')
                match = MCP_PATH.fullmatch(target.path)
                if match and (target.host == 'api.hindsight.vectorize.io' or bank or 'hindsight' in name.casefold()):
                    base = match[1]
                    path_bank = accepted_bank(target.path[len(base) + len('/mcp/'):])
                    candidates.append({'url': target.origin + base, 'bank': path_bank or bank,
                                       'source': source.label + ' · Hindsight MCP'})
        configured = self.env.get('HINDSIGHT_API_URL') or values.get('apiUrl')
        bank = accepted_bank(self.env.get('HINDSIGHT_BANK_ID') or values.get('bankId'))
        if not configured and values.get('backend') == 'hindsight':
            configured = 'http://127.0.0.1:8888'
        if configured:
            try:
                target = endpoint(configured)
            except DeskError:
                pass  # Do not display an unsafe URL; the native settings tab can identify its source.
            else:
                candidates.insert(0, {'url': target.url, 'bank': bank,
                    'source': 'HINDSIGHT_API_URL' if self.env.get('HINDSIGHT_API_URL') else native_origin})
        known = {}
        for candidate in candidates:
            known.setdefault((candidate['url'], candidate['bank']), candidate)
        candidates = list(known.values())[:24]
        return {'candidate': candidates[0] if candidates else None, 'candidates': candidates}

    def banks(self, target, token, offset, query):
        result = self.fetch(target, token, BANKS + '?' + urlencode({'limit': BANK_LIMIT, 'offset': offset, 'q': query}))
        if not isinstance(result.get('banks'), list) or count(result.get('total')) is None:
            raise DeskError('The endpoint did not return a Hindsight bank list.', 502)
        rows = {}
        for entry in result['banks'][:BANK_LIMIT]:
            row = object_value(entry)
            ident = accepted_bank(row.get('bank_id'))
            if ident:
                rows[ident] = {'id': ident, 'label': clipped(row.get('display_alias') or row.get('name'), 140) or ident}
        return {'banks': list(rows.values()), 'total': result['total'], 'offset': offset}

    def bank(self, target, token, bank, mode):
        path = BANKS + '/' + quote(bank, safe='')
        result = {'issues': {}}
        try:
            stats = self.fetch(target, token, path + '/stats')
            keys = ('total_nodes', 'total_links', 'total_documents', 'pending_operations', 'failed_operations')
            result['stats'] = {k: count(stats.get(k)) for k in keys}
        except DeskError as error:
            result['issues']['stats'] = str(error)
        try:
            graph = self.fetch(target, token, path + ('/entities/graph' if mode == 'entities' else '/graph') +
                               '?' + urlencode({'limit': GRAPH_LIMIT}))
            result['graph'] = graph_data(graph, mode)
        except DeskError as error:
            result['issues']['graph'] = str(error)
        try:
            data = self.fetch(target, token, path + '/config')
            if not isinstance(data.get('config'), dict):
                raise DeskError('This server did not return a Hindsight configuration snapshot.', 502)
            result['config'] = masked(('config',), data['config'])
            result['overrides'] = masked(('overrides',), object_value(data.get('overrides')))
        except DeskError as error:
            result['issues']['config'] = str(error)
        return result

    def dispatch(self, body):
        action = body.get('action')
        if action == 'discover':
            return self.discover()
        if action == 'connect':
            target = endpoint(body.get('url'))
            token = body.get('token', '')
            if body.get('confirm') is not True:
                raise DeskError('Confirm the exact Hindsight API endpoint before connecting.')
            if not isinstance(token, str) or len(token) > 2048 or any(not 33 <= ord(c) <= 126 for c in token):
                raise DeskError('Enter a valid Hindsight API key without spaces.')
            if target.scheme == 'http' and not target.loopback:
                if self.http_origin != target.origin:
                    raise DeskError('Use HTTPS, a loopback address, or a locally pinned HTTP origin.', 403)
                if token:
                    raise DeskError('An API key cannot be sent over non-loopback HTTP.', 403)
            initial = self.banks(target, token, 0, '')
            connection = secrets.token_urlsafe(18)
            with self.lock:
                self.session = connection, target, token, {row['id'] for row in initial['banks']}
            return {**initial, 'connection': connection, 'url': target.url}
        with self.lock:
            session = self.session
            if not session or body.get('connection') != session[0]:
                raise DeskError('Connect to a Hindsight API first.', 409)
            _, target, token, allowed = session
            if action == 'disconnect':
                self.session = None
                return {'disconnected': True}
        if action == 'list':
            offset, query = body.get('offset', 0), body.get('query', '')
            if type(offset) is not int or not 0 <= offset <= 100000 or not isinstance(query, str) or len(query) > 120:
                raise DeskError('Invalid bank search or page.')
            result = self.banks(target, token, offset, query)
            with self.lock:
                if self.session is not session:
                    raise DeskError('The Hindsight connection changed. Reconnect or reload the tab.', 409)
                self.session = session[:3] + ({row['id'] for row in result['banks']},)
            return result
        if action == 'bank':
            bank, mode = accepted_bank(body.get('bank')), body.get('mode', 'memory')
            if bank not in allowed or mode not in ('memory', 'entities'):
                raise DeskError('Select a listed Hindsight bank and graph.', 404)
            result = self.bank(target, token, bank, mode)
            with self.lock:
                if self.session is not session:
                    raise DeskError('The Hindsight connection changed. Reconnect or reload the tab.', 409)
            return result
        raise DeskError('Select a supported Hindsight action.')
