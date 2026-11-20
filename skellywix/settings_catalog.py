"""Native setting metadata. No command runs against the operator's profile."""
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


class DeskError(ValueError):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


SETTING_KEY = re.compile(r'[a-zA-Z][\w-]*(?:\.[\w-]+)*\Z', re.ASCII)
SECRET_WORD = re.compile(r'(?:apikey|accesstoken|refreshtoken|idtoken|token|password|secret|credential|privatekey)\Z')
UNCHANGED = {'$skellywix': 'keep stored secret'}
MISSING = object()


def sensitive(path, value):
    name = re.sub(r'[^a-z0-9]', '', path[-1].rsplit('.', 1)[-1].lower())
    parents = {str(p).lower() for p in path[:-1]}
    if name in ('headers', 'env', 'environment', 'httpheaders', 'authorization', 'cookie', 'credentials', 'keycommand'):
        return True
    if name == 'auth' and isinstance(value, (dict, list)):
        return True
    if SECRET_WORD.search(name):
        return True
    # Commands and arguments from connection files can carry inline credentials.
    if parents & {'mcpservers', 'providers'} and name in ('command', 'args'):
        return True
    if isinstance(value, str):
        # A connection URL with userinfo or query parameters is not a safe preview.
        return value.startswith(('http://', 'https://')) and any(c in value for c in '@?#')
    return False


def masked(path, value):
    if sensitive(path, value):
        return UNCHANGED
    if isinstance(value, dict):
        return {k: masked((*path, k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked((*path, str(i)), v) for i, v in enumerate(value)]
    return value


def restore(path, draft, old):
    if draft == UNCHANGED:
        if old is not MISSING and sensitive(path, old):
            return old
        raise DeskError('A masked value must remain at its original location.')
    if isinstance(draft, dict):
        previous = old if isinstance(old, dict) else {}
        return {k: restore((*path, k), v, previous.get(k, MISSING)) for k, v in draft.items()}
    if isinstance(draft, list):
        previous = old if isinstance(old, list) else []
        # An edited array must not accidentally move an unchanged secret to a different entry.
        same_items = isinstance(old, list) and len(previous) == len(draft) and all(
            not isinstance(d, dict) or not isinstance(p, dict) or
            (d.get('id'), d.get('name')) == (p.get('id'), p.get('name')) for d, p in zip(draft, previous)
        )
        for i, v in enumerate(draft):
            draft[i] = restore((*path, str(i)), v, previous[i] if same_items else MISSING)
    return draft


@lru_cache(maxsize=1)
def pi_schemas():
    return json.loads(Path(__file__).with_name('pi_schemas.json').read_bytes())['schemas']


def schema_details(schema, definitions):
    if '$ref' in schema:
        return schema_details(definitions[schema['$ref'].rsplit('/', 1)[1]], definitions)
    branches = schema.get('anyOf') or schema.get('oneOf')
    if branches and all('const' in branch for branch in branches):
        return {'type': 'enum', 'options': [branch['const'] for branch in branches]}
    if 'enum' in schema:
        return {'type': 'enum', 'options': schema['enum']}
    kind = schema.get('type')
    return {'type': kind if isinstance(kind, str) else 'json'}


@lru_cache(maxsize=3)
def pi_fields(document):
    schema = pi_schemas()[document]
    fields = {}

    def visit(path, node, inherited=''):
        description = node.get('description', inherited)
        if document == 'settings' and node.get('properties') and not node.get('patternProperties'):
            for key, child in node['properties'].items():
                visit((*path, key), child, description)
        else:
            row = schema_details(node, schema.get('$defs', {}))
            fields['.'.join(path)] = (path, {**row, 'description': description,
                                           **({'value': node['default']} if 'default' in node else {})})

    for key, node in schema['properties'].items():
        visit((key,), node)
    return fields


def validate_pi(document, payload):
    from jsonschema import Draft202012Validator

    error = next(Draft202012Validator(pi_schemas()[document]).iter_errors(payload), None)
    if error:
        loc = '.'.join(str(item) for item in error.absolute_path)
        raise DeskError(f'The document does not match the official Pi schema at {loc or "the root"}.', 422)


class OmpRegistry:
    def __init__(self, native, env):
        # A PATH entry must be absolute. Do not run a project-local command found by the OS.
        path = [p for p in os.get_exec_path(env) if os.path.isabs(p)]
        found = shutil.which('omp.exe' if os.name == 'nt' else 'omp', path=os.pathsep.join(path)) if native and path else None
        self.binary = found if found and os.path.normcase(os.path.dirname(found)) in {
            os.path.normcase(os.path.normpath(p)) for p in path} else None
        self.env = {k: env[k] for k in ('SYSTEMROOT', 'WINDIR') if k in env}
        self._fields = None

    def _command(self, *args):
        if not self.binary:
            raise DeskError('OMP executable not available for the native setting catalog.', 503)
        try:
            with TemporaryDirectory(prefix='skellywix-omp-') as directory:
                home = Path(directory)
                env = {**self.env, **dict.fromkeys(('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TMP', 'TEMP',
                                                   'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                                                   'XDG_STATE_HOME'), directory)}
                env.update(PI_CONFIG_DIR=str(home / '.omp'), PI_CODING_AGENT_DIR=str(home / 'agent'),
                           OMP_PROFILE='default', PI_OFFLINE='1', PI_SKIP_VERSION_CHECK='1',
                           NO_COLOR='1', PATH=str(Path(self.binary).parent))
                # Use files to bound how much output is loaded into memory. These invocations never receive tokens.
                with (home / 'output').open('w+b') as output:
                    result = subprocess.run([self.binary, *args], cwd=home, env=env, timeout=8,
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                            check=False)
                    output.seek(0)
                    data = output.read(512 * 1024 + 1)
                    if len(data) > 512 * 1024:
                        raise DeskError('OMP returned an oversized settings response.', 502)
                    return result.returncode, data.decode('utf-8-sig')
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise DeskError('The isolated OMP settings command could not run.', 503) from None

    def fields(self):
        if self._fields is not None:
            return self._fields
        status, raw = self._command('config', 'list', '--json')
        try:
            fields = json.loads(raw) if not status else None
            if not isinstance(fields, dict) or not fields or not all(
                    SETTING_KEY.fullmatch(key) and isinstance(value, dict) and isinstance(value.get('type'), str)
                    for key, value in fields.items()):
                raise ValueError
        except (ValueError, RecursionError):
            raise DeskError('Installed OMP did not return a supported settings catalog.', 502) from None
        status, listing = self._command('config', 'list')
        if not status:
            for line in listing.splitlines():
                match = re.fullmatch(r'\s*([\w.-]+)\s+=.* \(([^()]+)\)', line)
                if match and fields.get(match[1], {}).get('type') == 'enum':
                    fields[match[1]]['options'] = match[2].split('|')
        self._fields = fields
        return fields

    def validate(self, key, value):
        # Credentials and records containing credentials are checked structurally in the desk;
        # they must never be placed in argv or written into the isolated native profile.
        if masked(tuple(key.split('.')), value) != value:
            return value
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        if len(raw) > 16000:
            raise DeskError('Value exceeds the native OMP command validation limit.', 422)
        status, output = self._command('config', 'set', key, raw, '--json')
        if status:
            raise DeskError('OMP rejected this value in native validation.', 422)
        try:
            response = json.loads(output)
            return response['value']
        except (TypeError, KeyError, ValueError, RecursionError):
            raise DeskError('OMP did not return its validated value. No file was changed.', 502) from None


@lru_cache(maxsize=1)
def omp_bindings():
    # Action IDs common to Pi/OMP are suggestions; OMP extensions can declare additional IDs.
    return {key: (path, {'type': 'json', 'description': 'A key chord or list of key chords.'})
            for key, (path, _) in pi_fields('keybindings').items() if key.startswith(('tui.', 'app.'))}


def load_dependencies():
    from importlib.util import find_spec

    if find_spec('jsonschema') is None or find_spec('ruamel') is None or find_spec('ruamel.yaml') is None:
        raise DeskError('Install the package with its [operator] extra to open the operator desk.')
