"""Read local harness declarations; never load a plugin or connect to its endpoint."""
from collections import Counter
from contextlib import closing
import csv
from itertools import islice
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 can still read JSON and scalar YAML sources.
    tomllib = None

KINDS = ('model', 'memory', 'vector', 'mcp', 'tool', 'skill', 'ttsr', 'hook', 'extension', 'lsp', 'lint', 'automation', 'runtime')
NAME = re.compile(r"[\w@][\w@. :+·&(),'{}\[\]\-]{0,119}\Z")
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_@.+:/\-]{0,143}\Z')
LABEL = re.compile(r'[\w][\w ,().+:\-]{0,87}\Z')
PACKAGE = re.compile(r'(?:@[a-z0-9][a-z0-9_.-]*/)?[a-z0-9][a-z0-9_.-]*\Z')
JSONC = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.S)
JSONC_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')
MAX_FILE = 2 * 1024 * 1024
MAX_NODES = 2400
MAX_EDGES = 3600
RULE = re.compile(r'^(?:ttsr(?:\.[\w-]+)?|condition):', re.M)
SKILL_DISABLED = re.compile(r'^enabled:\s*false\s*(?:#.*)?$', re.M)
NO_OMP_CACHE = frozenset(('copilot', 'desktop', 'pi'))
REGISTRATION = re.compile(r'''\.registerTool\(\s*\{\s*name\s*:\s*['"]([\w.-]{1,80})['"]''', re.ASCII)
EVENT = re.compile(r'''\.on\(\s*['"]([\w.]{1,48})['"]''', re.ASCII)
LINT_REF = re.compile(r'\b([a-z][\w-]*-lint|lint-[a-z][\w-]*)\.(?:ps1|py|[cm]?[jt]s|sh)\b', re.I)
LINT_MARKERS = {
    'eslint': ('eslint.config.js', 'eslint.config.mjs', 'eslint.config.cjs', 'eslint.config.ts', '.eslintrc', '.eslintrc.json', '.eslintrc.yaml', '.eslintrc.yml', '.eslintrc.js', '.eslintrc.cjs'),
    'ruff': ('ruff.toml', '.ruff.toml'), 'biome': ('biome.json', 'biome.jsonc'),
    'shellcheck': ('.shellcheckrc',), 'golangci-lint': ('.golangci.yaml', '.golangci.yml', '.golangci.toml', '.golangci.json'),
    'stylelint': ('.stylelintrc', '.stylelintrc.json', 'stylelint.config.js', 'stylelint.config.mjs'),
    'PSScriptAnalyzer': ('PSScriptAnalyzerSettings.psd1',), 'yamllint': ('.yamllint', '.yamllint.yml', '.yamllint.yaml'),
    'markdownlint': ('.markdownlint.json', '.markdownlint.yaml', '.markdownlint.yml', '.markdownlint-cli2.yaml'),
    'flake8': ('.flake8',), 'pylint': ('.pylintrc', 'pylintrc'), 'oxlint': ('.oxlintrc.json',),
}
NATIVE_MEMORY = {
    'hindsight': 'Hindsight - Memory',
    'local': 'OMP Local - Memory',
    'mnemopi': 'Mnemopi - Memory',
    'sharpshooter': 'Sharpshooter - Memory',
}
QDRANT = 'Qdrant - Vector Search'
HINDSIGHT = NATIVE_MEMORY['hindsight']
KNOWLEDGE_GRAPH = 'Knowledge Graph - Memory'
MODULES = frozenset((*NATIVE_MEMORY.values(), QDRANT, KNOWLEDGE_GRAPH, 'MCP - Memory'))
CONFIG_KEYS = frozenset(('memory.backend', 'memories.enabled', 'hindsight.apiUrl', 'mcp.enableProjectConfig'))
UNSUPPORTED = object()
SETTING_LINE = re.compile(r'^( *)([A-Za-z][\w.-]*):(?:\s+(.*)|$)')
SETTING_SCALAR = re.compile(r"""(?:"([^"\\]*)"|'([^']*)'|([A-Za-z0-9][\w./:-]*))\s*(?:#.*)?\Z""")
QDRANT_PACKAGE = re.compile(r'mcp-server-qdrant(?:[=<>!~].+|:[\w.-]+)?\Z', re.I)
MEMORY_PACKAGE = re.compile(r'@modelcontextprotocol/server-memory(?:@[\w.-]+)?\Z', re.I)
MCP_PATH = re.compile(r'(.*?)/mcp(?:/[^/]+)?/?\Z')


def clean(value, *, model=False):
    pattern = MODEL if model else NAME
    if isinstance(value, str) and pattern.fullmatch(value) and value == value.strip():
        return value
    return None


def plain_file(path):
    info = path.lstat()
    # Reparse points (including junctions) and symlinks are not regular inputs.
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise OSError('Not a regular input')
    return info


def entries(path, *, limit=1024):
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return []
        return sorted(islice(path.iterdir(), limit), key=lambda item: item.name)
    except OSError:
        return []


def text(path, *, limit=MAX_FILE, prefix=False):
    info = plain_file(path)
    if not prefix and info.st_size > limit:
        raise ValueError('Input too large')
    with path.open('rb') as source:
        data = source.read(limit if prefix else limit + 1)
    if len(data) > limit:
        raise ValueError('Input too large')
    return data.decode('utf-8-sig', errors='replace' if prefix else 'strict')


def obj(path):
    raw = text(path)
    if path.suffix == '.toml':
        if tomllib is None:
            raise ValueError('TOML requires Python 3.11')
        value = tomllib.loads(raw)
    else:
        if path.suffix == '.jsonc':
            raw = JSONC.sub(lambda m: m[0] if m[0].startswith('"') else ' ', raw)
            raw = JSONC_COMMA.sub(lambda m: m[0] if m[0].startswith('"') else '', raw)
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Expected an object')
    return value


def read_obj(path):
    try:
        return obj(path)
    except (OSError, ValueError, RecursionError):
        return {}


def sequence(value):
    return value if isinstance(value, list) else []


def mapping(value):
    return value if isinstance(value, dict) else {}


def canonical(server, tool):
    def slug(value):
        return re.sub(r'[^a-z0-9]+', '_', value.lower(), flags=re.ASCII).strip('_')

    left, right = slug(server), slug(tool)
    if right.startswith(left + '_'):
        right = right[len(left) + 1:]
    # Long/colliding names need actual OMP registry evidence; do not guess a suffix.
    name = f'mcp__{left}_{right}'
    return name if left and right and len(name) <= 64 else None


def config_settings(raw):
    """Project only known scalar keys. Full YAML is delegated to the native viewer."""
    if raw.lstrip().startswith('{'):
        value = json.loads(raw)
        return {key: nested[field] for key in CONFIG_KEYS
                for group, field in (key.split('.'),)
                if field in (nested := mapping(mapping(value).get(group)))}
    groups = {key.split('.')[0] for key in CONFIG_KEYS}
    values = {}
    group, depth = None, None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line.lstrip().startswith('<<:'):
            keys = CONFIG_KEYS if line == line.lstrip() else (k for k in CONFIG_KEYS if k.startswith(f'{group}.'))
            values.update(dict.fromkeys(keys, UNSUPPORTED))
            continue
        match = SETTING_LINE.fullmatch(line.rstrip())
        if not match:
            if not line.startswith(' '):
                group = None
            continue
        indent, field, value = len(match[1]), match[2], (match[3] or '').strip()
        if indent == 0:
            group, depth = (field if field in groups else None), None
            if group and value and not value.startswith('#'):
                if value not in ('null', '~', 'Null', 'NULL', '{}'):
                    values.update(dict.fromkeys((k for k in CONFIG_KEYS if k.startswith(f'{group}.')), UNSUPPORTED))
                group = None
            continue
        if not group:
            continue
        depth = indent if depth is None else depth
        key = f'{group}.{field}'
        if indent != depth or key not in CONFIG_KEYS:
            continue
        scalar = SETTING_SCALAR.fullmatch(value)
        if not scalar:
            if value not in ('', '~') and not value.startswith('#'):
                values[key] = UNSUPPORTED
            continue
        quoted = scalar[1] if scalar[1] is not None else scalar[2]
        atom = scalar[3]
        if quoted is not None:
            values[key] = quoted
        elif atom.lower() not in ('null',):
            values[key] = {'true': True, 'false': False}.get(atom.lower(), atom)
    return values


def api_address(value, *, mcp=False):
    """Normalize an HTTP base for local equality only. Never return it in a graph."""
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value) or '${' in value:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.query or url.fragment):
            return None
        path = url.path.rstrip('/')
        if mcp:
            match = MCP_PATH.fullmatch(url.path)
            if not match:
                return None
            path = match[1]
        return url.scheme, url.hostname, url.port or (443 if url.scheme == 'https' else 80), path
    except ValueError:
        return None


def remote_url(spec):
    return spec.get('url') or spec.get('httpUrl') or spec.get('serverUrl')


def service_module(spec, tools, hindsight_api):
    """Classify declarations and cached signatures without launching a server."""
    command = spec.get('command')
    launch = [*(command[:64] if isinstance(command, list) else (command,)), *sequence(spec.get('args'))[:64]]
    names = [token.replace('\\', '/').rsplit('/', 1)[-1].lower().removesuffix('.exe')
             for token in launch if isinstance(token, str)]
    # npm keeps a scope in the package name; filesystem paths do not identify it.
    known_memory = any(isinstance(t, str) and MEMORY_PACKAGE.fullmatch(t) for t in launch)
    qdrant_keys = set(mapping(spec.get('env'))) | set(mapping(spec.get('environment')))
    qdrant_keys.update(v for v in sequence(spec.get('env_vars'))[:64] if isinstance(v, str))
    target = api_address(remote_url(spec), mcp=True)
    header_names = (*mapping(spec.get('headers')), *mapping(spec.get('http_headers')))
    if target and (target == hindsight_api or target[1] == 'api.hindsight.vectorize.io'
                   or any(k.lower() == 'x-bank-id' for k in header_names)):
        return 'memory', HINDSIGHT, 'declared', target == hindsight_api
    if known_memory:
        return 'memory', KNOWLEDGE_GRAPH, 'declared', False
    if any(QDRANT_PACKAGE.fullmatch(name) for name in names) or {'QDRANT_URL', 'QDRANT_LOCAL_PATH'} & qdrant_keys:
        return 'vector', QDRANT, 'declared', False
    advertised = {name.lower().replace('_', '-') for name in tools}
    if {'qdrant-find', 'qdrant-store'} & advertised:
        return 'vector', QDRANT, 'cached', False
    if {'read-graph', 'create-entities'} <= advertised:
        return 'memory', KNOWLEDGE_GRAPH, 'cached', False
    if {'recall', 'retain'} <= advertised:
        return 'memory', 'MCP - Memory', 'cached', False
    return None


class Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = set()
        self.coverage = {}
        self.add('runtime', 'OMP', 'Local observer', 'runtime')

    def add(self, kind, value, source, state='declared', detail=None, *, ident=None):
        name = clean(value, model=kind == 'model')
        if kind not in KINDS or name is None:
            return None
        ident = ident or f'{kind}:{name}'
        if ident not in self.nodes and len(self.nodes) < MAX_NODES:
            node = {'id': ident, 'kind': kind, 'name': name, 'state': state, 'source': source}
            if detail:
                node['detail'] = detail
            self.nodes[ident] = node
        return ident if ident in self.nodes else None

    def link(self, source, target, label):
        if source in self.nodes and target in self.nodes and source != target and len(self.edges) < MAX_EDGES:
            self.edges.add((source, target, label))

    def result(self):
        return {'nodes': sorted(self.nodes.values(), key=lambda n: (n['kind'], n['name'].casefold(), n['id'])),
                'edges': [{'source': s, 'target': t, 'label': l} for s, t, l in sorted(self.edges)],
                'coverage': self.coverage}


class Inventory:
    def __init__(self, agent, project, *, private=None, native=True, user=None, pi_agent=None):
        self.agent, self.project = agent, project
        self.private = private
        self.user = user
        self.project_agent = project / '.omp'
        self.pi = pi_agent
        self.pi_project = project / '.pi' if pi_agent is not None else None
        self.native = native
        self.command_paths = os.pathsep.join(part for part in os.get_exec_path() if os.path.isabs(part))
        self.omp = shutil.which('omp', path=self.command_paths) if native else None
        orca_cli = os.environ.get('ORCA_CLI_COMMAND') or ('orca-ide' if sys.platform.startswith('linux') else 'orca')
        self.orca = shutil.which(orca_cli, path=self.command_paths) if native else None
        self.powershell = shutil.which('powershell.exe', path=self.command_paths) if native and os.name == 'nt' else None
        self.systemctl = shutil.which('systemctl', path=self.command_paths) if native and os.name != 'nt' else None

    def command(self, program, args):
        if not program:
            return None
        try:
            env = os.environ.copy()
            # The viewer chooses the source, never connects to a paired host.
            env['PI_CODING_AGENT_DIR'] = str(self.agent)
            # A chosen nonstandard directory must not be shadowed by a different named profile.
            if self.agent.parent.parent.name != 'profiles':
                env['OMP_PROFILE'] = ''
            run = subprocess.run([program, *args], cwd=self.project, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4, check=False)
            if run.returncode == 0 and len(run.stdout) <= 8 * MAX_FILE:
                return run.stdout.decode('utf-8-sig')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None  # The adapter marks an unavailable native source; no fallback command runs.
        return None

    def native_json(self, program, args):
        raw = self.command(program, args)
        try:
            return json.loads(raw) if raw is not None else None
        except (ValueError, RecursionError):
            return None

    def setting(self, key):
        return mapping(self.native_json(self.omp, ['config', 'get', key, '--json'])).get('value')

    def plugins(self, *, project_enabled=True):
        # Read registered packages, including OMP-managed local links. Never enumerate dependencies.
        anchor = next((root / '.omp' for root in (self.project, *self.project.parents)
                       if (root / '.omp').is_dir()), self.project_agent)
        user_root = self.agent.parent
        xdg = os.environ.get('XDG_DATA_HOME') if os.name != 'nt' else None
        if xdg:
            name = Path('profiles', user_root.name) if user_root.parent.name == 'profiles' else Path()
            redirect = Path(xdg) / 'omp' / name
            if redirect.is_dir():
                user_root = redirect
        resolved = set()
        overrides = mapping(read_obj(anchor / 'plugin-overrides.json').get('plugins')) if project_enabled else {}
        for base in ((anchor, user_root) if project_enabled else (user_root,)):
            registry = base / 'plugins'
            lock = mapping(read_obj(registry / 'omp-plugins.lock.json').get('plugins'))
            packages = mapping(read_obj(registry / 'package.json').get('dependencies'))
            for name in list(dict.fromkeys((*lock, *packages)))[:128]:
                if (name in resolved or not PACKAGE.fullmatch(name)
                        or mapping(lock.get(name)).get('enabled') is False
                        or mapping(overrides.get(name)).get('enabled') is False):
                    continue
                try:
                    path = (registry / 'node_modules' / name).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue  # A removed or unresolved registration is not installed.
                if (os.name == 'nt' and path.drive.startswith('\\\\')) or not path.is_dir():
                    continue
                resolved.add(name)
                yield path

    def local_settings(self):
        # User, project, then launch overlays. These are file declarations, not session state.
        profile = self.agent / 'config.yml'
        if not profile.is_file():
            profile = self.agent / 'config.yaml'
        paths = [profile, self.project_agent / 'settings.json', self.project_agent / 'config.yml']
        paths.extend(self.project / Path(name).expanduser() for name in
                     islice(filter(None, os.environ.get('PI_CONFIG_FILES', '').split(os.pathsep)), 32))
        settings = {}
        for path in paths:
            try:
                raw = text(path)
                values = config_settings(raw)
            except FileNotFoundError:
                continue  # An absent optional layer is normal on a new installation.
            except (OSError, ValueError, RecursionError):
                values = dict.fromkeys(CONFIG_KEYS, UNSUPPORTED)
            settings.update((key, value) for key, value in values.items() if value is not None)
        return settings

    def sources(self, plugins, *, project_enabled):
        for root, origin, enabled in ((self.project_agent, 'OMP project', project_enabled),
                                      (self.agent, 'OMP profile', True)):
            if enabled:
                for filename in ('mcp.json', '.mcp.json'):
                    yield 'omp', origin, root / filename, 'mcpServers'
        for plugin in plugins:
            for filename in ('.mcp.json', 'mcp.json'):
                yield 'omp', 'Plugin manifest', plugin / filename, 'mcpServers'
        if project_enabled:
            for filename in ('mcp.json', '.mcp.json'):
                yield 'omp', 'Project fallback', self.project / filename, 'mcpServers'
        if self.pi is not None:
            yield 'pi', 'Pi project', self.pi_project / 'mcp.json', 'mcpServers'
            yield 'pi', 'Pi user', self.pi / 'mcp.json', 'mcpServers'

        project_sources = (
            ('claude', 'Claude Code', ('.claude/.mcp.json', '.claude/mcp.json'), 'mcpServers'),
            ('codex', 'Codex', ('.codex/config.toml',), 'mcp_servers'),
            ('gemini', 'Gemini CLI', ('.gemini/settings.json',), 'mcpServers'),
            ('opencode', 'OpenCode', ('.opencode/opencode.jsonc', '.opencode/opencode.json',
                                     'opencode.jsonc', 'opencode.json'), 'mcp'),
            ('cursor', 'Cursor', ('.cursor/mcp.json',), 'mcpServers'),
            ('windsurf', 'Windsurf', ('.windsurf/mcp_config.json',), 'mcpServers'),
            ('vscode', 'VS Code', ('.vscode/mcp.json',), 'servers'),
        )
        if project_enabled:
            for client, origin, names, key in project_sources:
                for name in names:
                    yield client, origin + ' project', self.project / name, key
        user = self.user
        if user is None:
            return
        claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or user / '.claude').expanduser()
        codex = Path(os.environ.get('CODEX_HOME') or user / '.codex').expanduser()
        copilot = Path(os.environ.get('COPILOT_HOME') or user / '.copilot').expanduser()
        xdg = Path(os.environ.get('XDG_CONFIG_HOME') or user / '.config').expanduser()
        for client, origin, path, key in (
            ('claude', 'Claude Code', claude / 'mcp.json', 'mcpServers'),
            ('claude', 'Claude Code', user / '.claude.json' if claude == user / '.claude' else claude / '.claude.json', 'mcpServers'),
            ('codex', 'Codex', codex / 'config.toml', 'mcp_servers'),
            ('gemini', 'Gemini CLI', user / '.gemini/settings.json', 'mcpServers'),
            ('opencode', 'OpenCode', xdg / 'opencode/opencode.jsonc', 'mcp'),
            ('opencode', 'OpenCode', xdg / 'opencode/opencode.json', 'mcp'),
            ('cursor', 'Cursor', user / '.cursor/mcp.json', 'mcpServers'),
            ('windsurf', 'Windsurf', user / '.codeium/windsurf/mcp_config.json', 'mcpServers'),
            ('copilot', 'Copilot', copilot / 'mcp-config.json', 'mcpServers'),
        ):
            yield client, origin + ' user', path, key
        if os.name == 'nt':
            app_data = Path(os.environ.get('APPDATA') or user / 'AppData/Roaming')
        elif sys.platform == 'darwin':
            app_data = user / 'Library/Application Support'
        else:
            app_data = None
        if app_data:
            yield 'desktop', 'Claude Desktop user', app_data / 'Claude/claude_desktop_config.json', 'mcpServers'

    def mcp(self, graph, plugins, hindsight_api, *, project_enabled, bases=None):
        registry = {}
        unavailable = 0
        truncated = False
        overrides = {}
        for client, origin, path, key in self.sources(plugins, project_enabled=project_enabled):
            try:
                payload = obj(path)
            except FileNotFoundError:
                continue  # No registration file is required.
            except (OSError, ValueError, RecursionError):
                unavailable += 1
                continue
            if path == self.agent / 'mcp.json':
                overrides = payload
            table = mapping(payload.get('mcp')) if client == 'vscode' else {}
            definitions = payload.get(key, table.get(key, payload if origin == 'Plugin manifest' else {}))
            if project_enabled and client == 'claude' and origin == 'Claude Code user':
                projects = mapping(payload.get('projects'))
                local = next((mapping(projects.get(name)) for name in
                              (str(self.project), self.project.as_posix()) if name in projects), {})
                definitions = {**mapping(definitions), **mapping(local.get(key))}
            definitions = mapping(definitions)
            truncated |= len(definitions) > 1024
            for name, definition in islice(definitions.items(), 1024):
                if not clean(name) or not isinstance(definition, dict):
                    continue
                scope = (client, name)
                if scope not in registry:
                    if len(registry) == MAX_NODES:
                        truncated = True
                        continue
                    registry[scope] = origin, definition
                elif client == 'opencode':
                    higher_origin, higher = registry[scope]
                    merged = {**definition, **higher}
                    for field in ('environment', 'headers'):
                        if isinstance(higher.get(field), dict) and isinstance(definition.get(field), dict):
                            merged[field] = {**definition[field], **higher[field]}
                    registry[scope] = higher_origin, merged
                elif client == 'pi':
                    higher_origin, higher = registry[scope]
                    if not higher.keys() & {'command', 'url', 'type'}:
                        overrides_only = {key: higher[key] for key in ('enabled', 'exposure', 'toolExposure')
                                          if key in higher}
                        registry[scope] = higher_origin, {**definition, **overrides_only}
        denied = {name for name in sequence(overrides.get('disabledServers')) if clean(name)}
        allowed = {name for name in sequence(overrides.get('enabledServers')) if clean(name)}
        aliases = Counter(name for client, name in registry if client not in NO_OMP_CACHE)
        tools = self.mcp_cache([name for name, count in aliases.items() if count == 1 and name not in denied])
        graph.coverage['MCP declarations'] = (
            'Registration limit reached' if truncated else
            'Some registration files unavailable' if unavailable else
            'Local registrations; client grants and activation are not inferred')
        graph.coverage['Project MCP sources'] = 'Included' if project_enabled else 'Excluded by policy or unreadable setting'
        graph.coverage['MCP tool cache'] = (
            'No readable OMP cache' if tools is None else
            'Same-name declarations left unattributed' if any(n > 1 for n in aliases.values()) else
            'Unexpired OMP metadata; not a connection check')
        owners = Counter()
        pairs = []
        for (client, server), (origin, spec) in registry.items():
            transport = spec.get('type')
            if transport not in ('stdio', 'http', 'sse'):
                transport = ('http' if remote_url(spec) or transport in ('remote', 'streamable-http')
                             else 'stdio' if spec.get('command') else 'unknown')
            disabled = (client == 'omp' and server in denied or
                        spec.get('enabled') is False and not (client == 'omp' and server in allowed))
            ident = f'mcp:{server}' if client == 'omp' else f'mcp:{client}/{server}'
            server_id = graph.add('mcp', server, origin, 'disabled' if disabled else 'declared', transport, ident=ident)
            root = 'runtime:OMP' if client == 'omp' else graph.add('runtime', origin.rsplit(' ', 1)[0],
                                                                  'MCP client declaration')
            graph.link(root, server_id, 'MCP registration')
            if disabled or server_id is None:
                continue
            server_tools = (tools or {}).get(server, ()) if client not in NO_OMP_CACHE else ()
            for tool in server_tools:
                if runtime_name := canonical(server, tool):
                    owners[runtime_name] += 1
            service = service_module(spec, server_tools, hindsight_api)
            if bases is not None and service and service[2] == 'declared':
                label = service[1]
                if label == HINDSIGHT and (base := api_address(remote_url(spec), mcp=True)):
                    bases['hindsight'].add(base)
                elif label == QDRANT:
                    env = {**mapping(spec.get('environment')), **mapping(spec.get('env'))}
                    if not env.get('QDRANT_LOCAL_PATH') and (base := api_address(env.get('QDRANT_URL'))):
                        bases['qdrant'].add(base)
            pairs.append((server, server_id, server_tools, service))
        for server, server_id, server_tools, service in pairs:
            for tool in server_tools:
                runtime_name = canonical(server, tool)
                ident = f'tool:{runtime_name}' if owners[runtime_name] == 1 else f'tool:cached:{server_id}:{tool}'
                tool_id = graph.add('tool', tool, 'OMP MCP cache', 'cached', 'Not proof of activation', ident=ident)
                if tool_id is not None and runtime_name and owners[runtime_name] == 1:
                    graph.nodes[tool_id]['owner'] = server_id
                graph.link(server_id, tool_id, 'Advertised tool')
            if not service:
                continue
            kind, label, evidence, same_api = service
            if same_api and 'memory:native:hindsight' in graph.nodes:
                graph.link(server_id, 'memory:native:hindsight', 'Same declared Hindsight API')
            else:
                node = graph.add(kind, label, 'MCP registration' if evidence == 'declared' else 'OMP MCP cache',
                                 evidence, 'Bank, collection, and connection not verified', ident=f'{kind}:{server_id}')
                action = 'vector search' if kind == 'vector' else 'memory operations'
                graph.link(server_id, node, ('Declares ' if evidence == 'declared' else 'Advertises ') + action)

    def mcp_cache(self, servers):
        path = self.agent / 'agent.db'
        try:
            plain_file(path)
            with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=0.3)) as conn:
                stamp = int(time.time())
                result = {}
                for server in servers[:512]:
                    row = conn.execute('SELECT value FROM cache WHERE key=? AND expires_at>? AND length(value)<=?',
                                       ('mcp_tools:' + server, stamp, MAX_FILE)).fetchone()
                    if row:
                        try:
                            payload = mapping(json.loads(row[0]))
                        except (ValueError, TypeError, RecursionError):
                            continue  # One invalid cache row does not invalidate other servers.
                        if payload.get('version') == 1:
                            result[server] = tuple(dict.fromkeys(name for t in sequence(payload.get('tools'))[:512]
                                                   if (name := clean(mapping(t).get('name')))))
                return result
        except (sqlite3.Error, OSError, ValueError):
            return None

    def files(self, graph, plugins):
        roots = [(self.project_agent, 'OMP project', 'runtime:OMP'), (self.agent, 'OMP profile', 'runtime:OMP')]
        roots.extend((plugin, 'Plugin files', 'runtime:OMP') for plugin in plugins)
        if self.pi is not None:
            roots.extend(((self.pi_project, 'Pi project', 'runtime:Pi'), (self.pi, 'Pi user', 'runtime:Pi')))
        for root, source, runtime in roots:
            paths = [('hook', root / 'hooks' / phase) for phase in ('pre', 'post')]
            paths.extend((('extension', root / 'extensions'), ('tool', root / 'tools')))
            for kind, folder in paths:
                for path in entries(folder):
                    if path.name.startswith('.') or path.name.endswith('.d.ts'):
                        continue
                    filename = path.stem
                    if not path.suffix:
                        filename, path = path.name, path / 'index.ts'
                    if path.suffix not in ('.ts', '.js', '.mts', '.mjs', '.cjs'):
                        continue
                    try:
                        data = text(path, limit=448 * 1024, prefix=True)
                    except OSError:
                        continue
                    phase = {'pre': 'Before tool', 'post': 'After tool', 'extensions': 'Extension'}.get(folder.name, 'Tool module')
                    events = sorted(set(EVENT.findall(data)))[:12]
                    detail = phase + (' · ' + ', '.join(events) if events else '')
                    ident = graph.add(kind, filename, source, detail=detail)
                    if runtime == 'runtime:Pi':
                        graph.add('runtime', 'Pi', 'Local declarations')
                    graph.link(runtime, ident, 'Discovered module')
                    for tool in REGISTRATION.findall(data)[:48]:
                        node = graph.add('tool', tool, 'Tool declaration')
                        graph.link(ident, node, 'Registers tool')
                    for checker in LINT_REF.findall(data)[:48]:
                        node = graph.add('lint', checker, 'Static checker reference')
                        graph.link(ident, node, 'References checker')

    def skills(self, graph, plugins):
        roots = [(self.project_agent / 'skills', 'OMP project', 'OMP'),
                 (self.agent / 'skills', 'OMP profile', 'OMP'),
                 (self.agent / 'managed-skills', 'OMP managed', 'OMP'),
                 (self.pi_project / 'skills', 'Pi project', 'Pi'),
                 (self.pi / 'skills', 'Pi user', 'Pi')]
        roots.extend((plugin / 'skills', 'Plugin files', 'OMP') for plugin in plugins)
        for index, (root, source, runtime) in enumerate(roots):
            for folder in entries(root):
                if not clean(folder.name) or folder.is_symlink() or not folder.is_dir():
                    continue
                try:
                    raw = text(folder / 'SKILL.md', limit=32 * 1024, prefix=True)
                except OSError:
                    continue
                header = raw.replace('\r\n', '\n').split('---\n', 2)
                if len(header) != 3 or header[0] or not header[1]:
                    continue
                state = 'disabled' if SKILL_DISABLED.search(header[1]) else 'declared'
                skill = graph.add('skill', folder.name, source, state, ident=f'skill:{index}/{folder.name}')
                graph.link(graph.add('runtime', runtime, 'Local declarations'), skill, 'Declares skill')
        graph.coverage['Skills'] = 'Local OMP, Pi, and discovered plug-in skill files; not active prompt metadata'

    def rules(self, graph):
        payload = self.native_json(self.omp, ['ttsr', 'list', '--json'])
        if isinstance(payload, list):
            rules = [mapping(entry).get('name') for entry in payload[:1024]]
            disabled = {r for r in sequence(self.setting('ttsr.disabledRules')) if clean(r)}
            enabled = self.setting('ttsr.enabled')
            graph.coverage['TTSR'] = 'Effective native rule list'
            for rule in rules:
                if clean(rule):
                    ident = graph.add('ttsr', rule, 'OMP rule registry',
                                      'disabled' if enabled is False or rule in disabled else 'declared')
                    graph.link('runtime:OMP', ident, 'Stream rule')
        else:
            graph.coverage['TTSR'] = 'Native list unavailable; local front matter only'
            for root in (self.project_agent, self.agent):
                for path in entries(root / 'rules'):
                    if path.suffix != '.md':
                        continue
                    try:
                        raw = text(path, limit=32 * 1024, prefix=True)
                    except OSError:
                        continue
                    if raw.startswith('---\n') or raw.startswith('---\r\n'):
                        head = raw.split('---', 2)
                        if len(head) == 3 and RULE.search(head[1]):
                            ident = graph.add('ttsr', path.stem, 'Rule front matter')
                            graph.link('runtime:OMP', ident, 'Stream rule declaration')

    def language(self, graph, plugins):
        for root in (self.project, self.project_agent, self.agent, *plugins):
            for filename in ('lsp.json', '.lsp.json'):
                source = read_obj(root / filename)
                for name, value in mapping(source.get('servers', source)).items():
                    if not isinstance(value, dict):
                        continue
                    ident = graph.add('lsp', name, 'LSP override', 'disabled' if value.get('disabled') is True else 'declared')
                    graph.link('runtime:OMP', ident, 'Language server declaration')
                    if value.get('isLinter') is True:
                        checker = graph.add('lint', name, 'LSP lint declaration')
                        graph.link(ident, checker, 'Can report diagnostics')
        for name, markers in LINT_MARKERS.items():
            if any((self.project / marker).is_file() for marker in markers):
                graph.add('lint', name, 'Project configuration')
        source = read_obj(self.project / 'package.json')
        for script in mapping(source.get('scripts')):
            if re.fullmatch(r'lint(?::[a-zA-Z0-9_.-]+)?', script):
                graph.add('lint', f'npm {script}', 'Package script')
        try:
            raw = text(self.project / 'pyproject.toml', limit=96 * 1024, prefix=True)
        except OSError:
            raw = ''
        for name in ('ruff', 'pylint', 'flake8', 'bandit'):
            if re.search(r'(?m)^\s*\[tool\.' + name + r'(?:\.|\])', raw):
                graph.add('lint', name, 'Project configuration')
        docs = self.command(self.omp, ['read', 'omp://lsp-config.md'])
        for name, role in re.findall(r'(?m)^\|\s*`([\w-]+)`\s*\|\s*([^|\n]+)\s*\|\s*`[^`\n]+`\s*\|', docs or ''):
            ident = graph.add('lsp', name, 'Installed OMP reference', 'documented')
            graph.link('runtime:OMP', ident, 'Built-in language server')
            if '(linter)' in role:
                checker = graph.add('lint', name, 'Installed OMP reference', 'documented')
                graph.link(ident, checker, 'Can report diagnostics')
        graph.coverage['Language and lint'] = 'JSON overrides, static checks and available OMP reference; not running clients'

    def memory(self, graph, settings, *, use_native):
        effective = self.setting('memory.backend') if use_native else None
        backend = effective if effective is not None else settings.get('memory.backend')
        if backend is None:
            legacy = settings.get('memories.enabled')
            backend = 'local' if legacy is True else 'off' if legacy in (False, None) else UNSUPPORTED
        if not isinstance(backend, str) or backend not in (*NATIVE_MEMORY, 'off'):
            graph.coverage['Native memory'] = 'Unsupported or unreadable local setting'
            return None
        graph.coverage['Native memory'] = (
            'Effective OMP setting; running session not queried' if effective is not None else
            'Off (declared or default)' if backend == 'off' else
            'Local file declaration; session overrides not visible')
        if backend == 'off':
            return None
        ident = graph.add('memory', NATIVE_MEMORY[backend], 'OMP memory setting', ident=f'memory:native:{backend}')
        graph.link('runtime:OMP', ident, 'Native memory backend')
        if backend != 'hindsight':
            return None
        bank = graph.add('memory', self.setting('hindsight.bankId'), 'OMP Hindsight bank setting') if use_native else None
        graph.link(ident, bank, 'Configured bank')
        recall = graph.add('memory', 'recall', 'OMP Hindsight lifecycle', 'documented',
                           'First-turn context injection; not a server receipt')
        retain = graph.add('memory', 'Transcript retention', 'OMP Hindsight lifecycle', 'documented',
                           'Completed turns are queued on the configured cadence')
        outbox = graph.add('memory', 'Explicit retain outbox', 'OMP Hindsight lifecycle', 'documented',
                           'Queued writes can retry; server acceptance is not completed extraction')
        graph.link(ident, recall, 'Can inject first-turn context')
        graph.link(ident, retain, 'Can schedule transcript retention')
        graph.link(ident, outbox, 'Durable explicit writes')
        graph.link(recall, bank, 'Reads scoped memory')
        graph.link(retain, bank, 'Queues completed turns')
        graph.link(outbox, bank, 'Delivers explicit writes')
        api = self.setting('hindsight.apiUrl') if use_native else None
        return api_address(os.environ.get('HINDSIGHT_API_URL') or api or
                           settings.get('hindsight.apiUrl', 'http://localhost:8888'))

    def native_runtime(self, graph):
        docs = self.command(self.omp, ['read', 'omp://'])
        graph.coverage['Tool documentation'] = 'Installed OMP reference' if docs else 'OMP reference unavailable'
        for name in re.findall(r'\(omp://tools/([\w-]{1,80})\.md\)', docs or ''):
            graph.add('tool', name.replace('-', '_'), 'Installed OMP reference', 'documented')
        otlp = any(key in os.environ for key in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'))
        if otlp:
            ident = graph.add('runtime', 'OpenTelemetry', 'Process export settings')
            graph.link('runtime:OMP', ident, 'Configured exporter')

    def schedules(self, graph):
        payload = self.native_json(self.orca, ['automations', 'list', '--environment', 'local', '--json'])
        if isinstance(payload, dict):
            payload = mapping(payload.get('result', payload)).get('automations')
        graph.coverage['Orca automations'] = 'Local schedule definitions' if isinstance(payload, list) else 'Orca registry unavailable'
        for item in sequence(payload)[:512]:
            entry = mapping(item)
            name = entry.get('title') or entry.get('name')
            enabled = entry.get('enabled')
            rule = entry.get('rrule')
            freq = re.search(r'(?:^|[:;])FREQ=(MINUTELY|HOURLY|DAILY|WEEKLY|MONTHLY|YEARLY)(?:$|;)', rule) if isinstance(rule, str) else None
            node = graph.add('automation', name, 'Orca automation', 'disabled' if enabled is False else 'declared',
                             detail=f'{freq[1].title()} recurrence' if freq else None)
            scheduler = graph.add('runtime', 'Orca', 'Local automation registry')
            graph.link(scheduler, node, 'Schedules')
        if self.powershell:
            script = ("Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -match '^(OMP|Orca)[_. -]' "
                      "-or $_.TaskPath -match '^\\\\(OMP|Orca)\\\\' } | Select-Object TaskName,State | ConvertTo-Csv -NoTypeInformation")
            raw = self.command(self.powershell, ['-NoProfile', '-NonInteractive', '-Command', script])
            graph.coverage['Host automation'] = 'OMP and Orca Task Scheduler definitions' if raw is not None else 'Task Scheduler unavailable'
            for row in islice(csv.DictReader((raw or '').splitlines()), 512):
                graph.add('automation', row.get('TaskName'), 'Windows Task Scheduler',
                          'disabled' if row.get('State') == 'Disabled' else 'declared')
        elif self.systemctl:
            raw = self.command(self.systemctl, ['--user', 'list-unit-files', '--type=timer', '--no-legend', '--no-pager'])
            graph.coverage['Host automation'] = 'User OMP and Orca timer definitions' if raw is not None else 'User timer registry unavailable'
            for line in (raw or '').splitlines()[:512]:
                columns = line.split()
                if len(columns) > 1 and re.match(r'^(omp|orca)[_.-]', columns[0], re.I):
                    graph.add('automation', columns[0], 'User systemd timer', 'disabled' if columns[1] in ('masked', 'disabled') else 'declared')
        else:
            graph.coverage['Host automation'] = 'No supported scheduler available'

    def inserts(self, graph):
        edges = []
        rejected = 0
        for path in entries(self.private, limit=64) if self.private else ():
            if path.suffix != '.json' or path.name.startswith('.'):
                continue
            value = read_obj(path)
            if value.get('schema') != 'harness-neural-insert/1':
                rejected += 1
                continue
            for row in sequence(value.get('nodes'))[:512]:
                item = mapping(row)
                kind = item.get('kind')
                if not isinstance(kind, str) or kind not in KINDS:
                    continue
                node = graph.add(kind, item.get('name'), 'Private local insert')
                if node and clean(item.get('state')) == 'disabled' and graph.nodes[node]['source'] == 'Private local insert':
                    graph.nodes[node]['state'] = 'disabled'
            edges.extend(sequence(value.get('edges'))[:512])
        for row in edges:
            edge = mapping(row)
            source, target, label = (edge.get(key) for key in ('source', 'target', 'label'))
            if all(isinstance(value, str) for value in (source, target, label)) and LABEL.fullmatch(label):
                graph.link(source, target, label)
        graph.coverage['Private inserts'] = 'Some inserts unreadable or unsupported' if rejected else 'Local metadata only; never bundled'

    def _discover(self, graph, *, use_native, include_inventory=False, bases=None):
        settings = self.local_settings()
        policy = self.setting('mcp.enableProjectConfig') if use_native else None
        if policy is None:
            policy = settings.get('mcp.enableProjectConfig')
        project_enabled = policy is None or policy is True
        plugins = tuple(self.plugins(project_enabled=project_enabled))
        api = self.memory(graph, settings, use_native=use_native)
        if bases is not None and api:
            bases['hindsight'].add(api)
        self.mcp(graph, plugins, api, project_enabled=project_enabled, bases=bases)
        return tuple(self.plugins()) if include_inventory and not project_enabled else plugins

    def api_candidates(self):
        """Keep potential API addresses in-process; caches do not establish an address."""
        graph = Graph()
        labels = {'hindsight': HINDSIGHT, 'qdrant': QDRANT}
        bases = {module: set() for module in labels}
        for module in labels:
            if base := api_address(os.environ.get(module.upper() + ('_API_URL' if module == 'hindsight' else '_URL'))):
                bases[module].add(base)
        self._discover(graph, use_native=False, bases=bases)
        declared = {node['name'] for node in graph.nodes.values() if node['state'] == 'declared'}
        return {module: (bool(bases[module]) or label in declared, bases[module]) for module, label in labels.items()}

    def summary(self):
        """A fixed vocabulary for MCP consumers; never read journals or private inserts."""
        graph = Graph()
        self._discover(graph, use_native=False)
        servers = Counter(n['state'] for n in graph.nodes.values() if n['kind'] == 'mcp')
        matches = Counter((n['name'], n['state']) for n in graph.nodes.values()
                          if n['kind'] in ('memory', 'vector') and n['name'] in MODULES)
        return {'schema': 'harness-discovery/1',
                'modules': [{'module': name, 'evidence': evidence, 'count': count}
                            for (name, evidence), count in sorted(matches.items())],
                'mcp': {'declared': servers['declared'], 'disabled': servers['disabled']},
                'coverage': graph.coverage}

    def scan(self):
        graph = Graph()
        plugins = self._discover(graph, use_native=self.native, include_inventory=True)
        self.files(graph, plugins)
        if self.pi is not None:
            self.skills(graph, plugins)
        self.rules(graph)
        self.language(graph, plugins)
        self.native_runtime(graph)
        self.schedules(graph)
        self.inserts(graph)
        return graph.result()
