"""Read only the exact API scope approved at supervisor launch."""
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from ipaddress import ip_address
import os
import re
from urllib.parse import urlsplit

from .neural_inventory import api_address
from .settings_desk import read_json

MODULES = ('hindsight', 'qdrant')
DNS_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
SCOPE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z')
SEGMENT = re.compile(r'[A-Za-z0-9_~-][A-Za-z0-9._~-]*\Z')
ENV_NAME = re.compile(r'[A-Z_][A-Z0-9_]{0,63}\Z')
CREDENTIAL = re.compile(r'[!-~]{1,4096}\Z')
RESPONSE_LIMIT = 128 * 1024
IO_TIMEOUT = 2.0


def api_base(value):
    if (not isinstance(value, str) or len(value) > 512 or not value.isascii()
            or any(char in value for char in '?#@%\\') or any(not '!' <= char <= '~' for char in value)):
        raise ValueError('Invalid API base URL')
    base = api_address(value)
    if base is None:
        raise ValueError('Invalid API base URL')
    scheme, host, _, path = base
    parts = urlsplit(value)
    if parts.port == 0 or parts.netloc.endswith(':') or parts.path.endswith('//'):
        raise ValueError('Invalid API base URL')
    if path and any(not SEGMENT.fullmatch(segment) for segment in path[1:].split('/')):
        raise ValueError('Invalid API base path')
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is None and (len(host) > 253 or any(not DNS_LABEL.fullmatch(label) for label in host.split('.'))):
        raise ValueError('Invalid API hostname')
    if scheme == 'http' and host != 'localhost' and (address is None or not address.is_loopback):
        raise ValueError('Use HTTPS or a loopback address')
    return base


@dataclass(frozen=True)
class Connection:
    module: str
    base: tuple[str, str, int, str]
    scope: str
    credential_env: str | None

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or not {'module', 'baseUrl', 'scope'} <= value.keys() <= {
                'module', 'baseUrl', 'scope', 'credentialEnv'}:
            raise ValueError('Invalid connection approval')
        module, scope = value['module'], value['scope']
        credential = value.get('credentialEnv')
        if (module not in MODULES or not isinstance(scope, str) or not SCOPE.fullmatch(scope)
                or 'credentialEnv' in value and (not isinstance(credential, str) or not ENV_NAME.fullmatch(credential))):
            raise ValueError('Invalid connection approval')
        return cls(module, api_base(value['baseUrl']), scope, credential)

    def probe(self):
        scheme, host, port, path = self.base
        headers = {'Accept': 'application/json', 'Connection': 'close'}
        if self.credential_env is not None:
            credential = os.environ.get(self.credential_env, '')
            if not CREDENTIAL.fullmatch(credential):
                return 'credential_unavailable'
            if self.module == 'hindsight':
                headers['Authorization'] = f'Bearer {credential}'
            else:
                headers['api-key'] = credential
        # Never resolve plaintext localhost via DNS or a proxy.
        if scheme == 'http' and host == 'localhost':
            headers['Host'] = 'localhost' if port == 80 else f'localhost:{port}'
            host = '127.0.0.1'
        suffix = (f'/v1/default/banks/{self.scope}/stats' if self.module == 'hindsight'
                  else f'/collections/{self.scope}')
        transport = HTTPSConnection if scheme == 'https' else HTTPConnection
        try:
            with closing(transport(host, port, timeout=IO_TIMEOUT)) as connection:
                connection.request('GET', path + suffix, headers=headers)
                with connection.getresponse() as response:
                    if response.status != 200:
                        if 300 <= response.status < 400:
                            return 'redirect_blocked'
                        if response.status in (401, 403):
                            return 'denied'
                        if response.status == 404:
                            return 'scope_unavailable'
                        return 'http_error'
                    length = response.length
                    if ((length is not None and length > RESPONSE_LIMIT)
                            or response.headers.get_content_type() != 'application/json'):
                        return 'unexpected_response'
                    data = response.read(RESPONSE_LIMIT + 1)
                    if len(data) > RESPONSE_LIMIT or length is not None and len(data) != length:
                        return 'unexpected_response'
            value = read_json(data)
        except (OSError, HTTPException):
            return 'unreachable'
        except (ValueError, RecursionError):
            return 'unexpected_response'
        if not self.matches(value):
            return 'unexpected_response'
        return None

    def matches(self, value):
        def count(item):
            return type(item) is int and item >= 0

        if not isinstance(value, dict):
            return False
        if self.module == 'hindsight':
            return value.get('bank_id') == self.scope and count(value.get('total_nodes'))
        result = value.get('result')
        return (value.get('status') == 'ok' and isinstance(result, dict)
                and result.get('status') in ('green', 'yellow', 'red', 'grey')
                and count(result.get('segments_count')) and isinstance(result.get('config'), dict))


def parse_connections(entries):
    if not isinstance(entries, list) or len(entries) > len(MODULES):
        raise ValueError('Invalid connection approvals')
    result = {}
    for row in entries:
        connection = Connection.parse(row)
        if connection.module in result:
            raise ValueError('Duplicate connection approval')
        result[connection.module] = connection
    return result


def check_connections(approved, inventory, policy_current):
    rows = []
    for module, (discovered, candidates) in inventory.api_candidates().items():
        connection = approved.get(module)
        if connection is None:
            rows.append({'module': module, 'state': 'discovered' if discovered else 'not_discovered'})
            continue
        if not policy_current():
            reason = 'policy_changed'
        else:
            reason = connection.probe() if connection.base in candidates else 'declaration_missing'
        row = {'module': module, 'state': 'connected' if reason is None else 'error', 'check': 'startup'}
        if reason is not None:
            row['reason'] = reason
        rows.append(row)
    return rows
