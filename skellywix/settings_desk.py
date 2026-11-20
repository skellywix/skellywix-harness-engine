"""Discover native configuration files and save a confirmed, revision-bound field edit."""
from dataclasses import dataclass
from hashlib import blake2s
from io import StringIO
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from tempfile import NamedTemporaryFile
from threading import Lock

from .neural_inventory import JSONC, JSONC_COMMA, entries, plain_file
from .settings_catalog import (DeskError, MISSING, OmpRegistry, SETTING_KEY,
                               masked, omp_bindings, pi_fields, restore, sensitive, validate_pi)

MAX_DOCUMENT = 2 * 1024 * 1024
PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
UNSAFE_KEYS = frozenset(('__proto__', 'constructor', 'prototype'))
LSP_FILES = ('lsp.json', '.lsp.json', 'lsp.yaml', '.lsp.yaml', 'lsp.yml', '.lsp.yml')


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate mapping key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Non-finite number')


def read_json(data):
    return json.loads(data, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def check_tree(value, depth=0, *, writing=False):
    if depth > 48:
        raise ValueError('Document nested too deeply')
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or writing and k in UNSAFE_KEYS:
                raise ValueError('Unsupported mapping key')
            check_tree(v, depth + 1, writing=writing)
    elif isinstance(value, list):
        for v in value:
            check_tree(v, depth + 1, writing=writing)
    elif value is not None and (not isinstance(value, (str, bool, int, float)) or
                                isinstance(value, float) and not math.isfinite(value)):
        raise ValueError('Unsupported value')


def directory_chain(path, *, create=False):
    if not path.is_absolute() or (os.name == 'nt' and path.drive.startswith('\\\\')):
        raise DeskError('Select a local configuration directory.', 403)
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                continue
            part.mkdir(mode=0o700, exist_ok=True)
            info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise DeskError('Linked or non-directory configuration paths are not editable.', 403)


def fingerprint(info):
    # Windows path and handle stat can disagree on ctime after a rename.
    timestamp = getattr(info, 'st_birthtime_ns', None) if os.name == 'nt' else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, timestamp,
            info.st_uid, info.st_nlink)


def read_file(path):
    try:
        directory_chain(path.parent)
        try:
            info = plain_file(path)
        except FileNotFoundError:
            return None, None
        if info.st_size > MAX_DOCUMENT:
            raise DeskError('Configuration file exceeds the 2 MiB editor limit.', 413)
        identity = fingerprint(info)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if fingerprint(opened) != identity:
                raise DeskError('The file changed while it was being opened. Reload the source.', 409)
            data = stream.read(MAX_DOCUMENT + 1)
            after = os.fstat(stream.fileno())
            if len(data) > MAX_DOCUMENT or fingerprint(after) != identity or after.st_ctime_ns != opened.st_ctime_ns:
                raise DeskError('The file changed while it was being read. Reload the source.', 409)
        directory_chain(path.parent)
        if fingerprint(plain_file(path)) != identity:
            raise DeskError('The file changed while it was being read. Reload the source.', 409)
        return data, (*identity, after.st_ctime_ns) if os.name == 'nt' else identity
    except OSError:
        raise DeskError('The configuration file is unavailable or is not a plain, local file.', 403) from None


def yaml_codec(raw):
    from ruamel.yaml import YAML

    # Avoid alias expansion, shared nodes, non-JSON tags and implicit native objects.
    for event in YAML(typ='base', pure=True).parse(raw):
        if getattr(event, 'anchor', None) or getattr(event, 'tag', None):
            raise ValueError('YAML aliases, anchors and explicit tags are unsupported')
    codec = YAML(typ='rt', pure=True)
    codec.preserve_quotes = True
    codec.max_depth = 48
    return codec


def document(path, data, *, jsonc=False, overlay=False):
    from ruamel.yaml.error import YAMLError

    if data is None:
        return {}, False
    try:
        raw = data.decode('utf-8-sig')
        if overlay or path.suffix in ('.yaml', '.yml'):
            value = yaml_codec(raw).load(raw)
            value = {} if value is None else value
            jsonc = False
        else:
            try:
                value = read_json(raw)
            except ValueError:
                if not jsonc:
                    raise
                value = read_json(JSONC_COMMA.sub(lambda m: m[0] if m[0].startswith('"') else '',
                    JSONC.sub(lambda m: m[0] if m[0].startswith('"') else ' ', raw)))
                jsonc = True
            else:
                jsonc = False
        if not isinstance(value, dict):
            raise ValueError('Mapping required')
        check_tree(value)
        return value, jsonc
    except (ValueError, TypeError, RecursionError, YAMLError):
        raise DeskError('This file is invalid or uses an unsupported syntax. It was not changed.', 422) from None


def encode_document(path, payload, prior):
    from ruamel.yaml.error import YAMLError

    try:
        check_tree(payload, writing=True)
        raw = (prior or b'').decode('utf-8-sig')
        if path.suffix in ('.yaml', '.yml'):
            out = StringIO()
            yaml_codec(raw).dump(payload, out)
            new = out.getvalue()
        else:
            new = json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + '\n'
        if '\r\n' in raw and '\n' not in raw.replace('\r\n', ''):
            new = new.replace('\n', '\r\n')
        data = new.encode('utf-8-sig' if (prior or b'').startswith(b'\xef\xbb\xbf') else 'utf-8')
    except (ValueError, TypeError, RecursionError, YAMLError):
        raise DeskError('The proposed change cannot be written in this native format.', 422) from None
    if len(data) > MAX_DOCUMENT:
        raise DeskError('The updated file would exceed the editor limit.', 413)
    return data


def copy_windows_permissions(source, destination):
    from ctypes import POINTER, WinDLL, WinError, byref, c_void_p, get_last_error
    from ctypes import wintypes as win

    api = WinDLL('advapi32', use_last_error=True)
    get = api.GetNamedSecurityInfoW
    set_info = api.SetNamedSecurityInfoW
    free = WinDLL('kernel32').LocalFree
    get.argtypes = (win.LPCWSTR, win.DWORD, win.DWORD) + (POINTER(c_void_p),) * 5
    set_info.argtypes = (win.LPCWSTR, win.DWORD, win.DWORD) + (c_void_p,) * 4
    get.restype = set_info.restype = win.DWORD
    free.argtypes = (c_void_p,)
    free.restype = c_void_p
    control = api.GetSecurityDescriptorControl
    control.argtypes = (c_void_p, POINTER(win.WORD), POINTER(win.DWORD))
    control.restype = win.BOOL
    owner, group, dacl, descriptor = (c_void_p() for _ in range(4))
    file_object = 1
    owner_group_dacl = 0x7
    if code := get(str(source), file_object, owner_group_dacl, byref(owner), byref(group),
                   byref(dacl), None, byref(descriptor)):
        raise WinError(code)
    try:
        bits, revision = win.WORD(), win.DWORD()
        if not control(descriptor, byref(bits), byref(revision)):
            raise WinError(get_last_error())
        if not dacl.value:
            raise OSError('Cannot copy an unrestricted DACL')
        protect = 0x80000000 if bits.value & 0x1000 else 0x20000000
        if code := set_info(str(destination), file_object, owner_group_dacl | protect, owner, group, dacl, None):
            raise WinError(code)
    finally:
        free(descriptor)


def replace_file(path, expected, data, *, keep_mode=False):
    temporary = None
    try:
        directory_chain(path.parent, create=data is not None and expected[1] is None)
        parent = path.parent.stat()
        if data is not None:
            if os.name != 'nt' and expected[1] is not None and expected[1][6] != os.geteuid():
                raise DeskError('Only settings files owned by the current user are editable.', 403)
            with NamedTemporaryFile(dir=path.parent, prefix='.skellywix-', delete=False) as stream:
                temporary = Path(stream.name)
            if os.name == 'nt' and expected[1] is not None:
                copy_windows_permissions(path, temporary)
            with temporary.open('wb') as stream:
                stream.write(data)
                if keep_mode and os.name != 'nt' and expected[1] is not None:
                    os.fchmod(stream.fileno(), expected[1][2] & 0o777)
                stream.flush()
                os.fsync(stream.fileno())
        if read_file(path) != expected or not os.path.samestat(parent, path.parent.stat()):
            raise DeskError('The file changed since you opened it. Reload the source before editing.', 409)
        if temporary is not None:
            if expected[1] is not None:
                os.replace(temporary, path)
                temporary = None
            elif os.name == 'nt':
                os.rename(temporary, path)  # Unlike os.replace, this fails on a concurrent creation.
                temporary = None
            else:
                os.link(temporary, path)  # No clobber when a native client creates the file first.
        else:
            path.unlink()
    except OSError:
        raise DeskError('Could not save the configuration file. Check file access and reload the source.', 409) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise DeskError('Could not remove a private temporary file in the selected configuration directory.', 500) from None


def lookup(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return MISSING
        data = data[part]
    return data


def edit_field(data, path, value):
    for part in path[:-1]:
        if part not in data:
            data[part] = {}
        data = data[part]
        if not isinstance(data, dict):
            raise DeskError('The parent field is not an object. Repair it with the native editor.', 422)
    if value is MISSING:
        data.pop(path[-1], None)
    else:
        data[path[-1]] = value


def value_type(value, field):
    kind = field['type']
    if not (kind == 'json' or
            kind == 'enum' and (value in field['options'] if 'options' in field else isinstance(value, str)) or
            kind == 'boolean' and type(value) is bool or
            kind == 'string' and isinstance(value, str) or
            kind == 'integer' and isinstance(value, int) and not isinstance(value, bool) or
            kind == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool) or
            kind == 'array' and isinstance(value, list) or
            kind in ('object', 'record') and isinstance(value, dict)):
        raise DeskError(f'This field requires a {kind} value.', 422)


def binding_value(value):
    return isinstance(value, str) or isinstance(value, list) and all(isinstance(v, str) for v in value)


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    path: Path
    document: str
    restricted: str = ''

    @property
    def pi(self):
        return self.id.startswith('pi:')

    @property
    def project(self):
        return self.id.startswith(('omp:project:', 'pi:project:'))

    @property
    def overlay(self):
        return self.id.startswith('omp:overlay:')

    def metadata(self):
        return {'id': self.id, 'label': self.label, 'path': str(self.path), 'harness': 'Pi' if self.pi else 'OMP',
                'document': self.document, 'exists': present(self.path), 'readOnly': self.restricted}


def present(path):
    # lstat detects a broken symlink too; don't fall back to a nominally absent file.
    try:
        directory_chain(path.parent)
        path.lstat()
        return True
    except (FileNotFoundError, DeskError, OSError):
        return False


def first_yml(root, name):
    main = root / f'{name}.yml'
    fallback = root / f'{name}.yaml'
    return fallback if not present(main) and present(fallback) else main


class SettingsDesk:
    def __init__(self, omp_agent, project, home, env, *, registry=None, native=True, pi_agent=None):
        self.omp = omp_agent
        self.project = project
        self.home = home
        self.env = env
        self.omp_root = Path(os.path.abspath(Path(env.get('PI_CONFIG_DIR') or home / '.omp').expanduser()))
        alternate = env.get('PI_CODING_AGENT_DIR')
        alternate = Path(os.path.abspath(Path(alternate).expanduser())) if alternate else None
        # Pi and OMP share this variable; don't treat OMP's legacy settings.json as Pi settings.
        self.pi = pi_agent or (alternate if alternate and alternate != omp_agent else home / '.pi/agent')
        self.registry = registry if registry is not None else OmpRegistry(native, env)
        self.lock = Lock()
        self.signature = secrets.token_bytes(32)
        self.undo = None

    def sources(self):
        found = {}

        def add(identifier, label, path, kind, restricted=''):
            found[identifier] = Source(identifier, label, path, kind, restricted)

        def omp_user(root, ident, title):
            for kind, stem, legacy in (('settings', 'config', 'settings.json'),
                                       ('models', 'models', 'models.json'),
                                       ('keybindings', 'keybindings', 'keybindings.json')):
                target = first_yml(root, stem)
                old = root / legacy
                unconverted = present(old) or kind == 'settings' and present(root / 'agent.db')
                add(f'{ident}:{kind}', f'{title} · {kind}', target, kind,
                    'Open OMP to migrate the legacy data before creating this file.'
                    if not present(target) and unconverted else '')
                if present(old):
                    add(f'{ident}:{kind}-legacy', f'{title} · legacy {kind}', old, kind,
                        'Legacy source; use the native OMP migration to edit it.')
            shared(root, ident, title)

        def shared(root, ident, title):
            add(f'{ident}:mcp', f'{title} · MCP', root / 'mcp.json', 'mcp')
            legacy = root / '.mcp.json'
            if present(legacy):
                add(f'{ident}:mcp-legacy', f'{title} · compatibility MCP', legacy, 'mcp')
            lsp_sources(root, ident, title, default=True)

        def lsp_sources(root, ident, title, *, default=False):
            for name in LSP_FILES:
                path = root / name
                if present(path) or default and name == 'lsp.json':
                    add(f'{ident}:lsp:{name}', f'{title} · {name}', path, 'lsp')

        for scope, root, kinds in (
            ('user', self.pi, ('settings', 'models', 'keybindings', 'mcp')),
            ('project', self.project / '.pi', ('settings', 'mcp')),
        ):
            for kind in kinds:
                label = 'MCP' if kind == 'mcp' else kind
                add(f'pi:{scope}:{kind}', f'Pi {scope} · {label}', root / f'{kind}.json', kind)
        omp_user(self.omp, 'omp:active', 'OMP active profile')
        default = self.omp_root / 'agent'
        if default != self.omp and present(default):
            omp_user(default, 'omp:default', 'OMP default profile')
        for profile in entries(self.omp_root / 'profiles', limit=128):
            root = profile / 'agent'
            if PROFILE.fullmatch(profile.name) and root != self.omp and present(root):
                omp_user(root, f'omp:profile:{profile.name}', f'OMP {profile.name} profile')
        root = self.project / '.omp'
        add('omp:project:settings', 'OMP project · settings', root / 'config.yml', 'settings')
        if present(root / 'settings.json'):
            add('omp:project:settings-legacy', 'OMP project · legacy settings', root / 'settings.json', 'settings',
                'Use the project config.yml for new settings.')
        shared(root, 'omp:project', 'OMP project')
        lsp_sources(self.project, 'omp:workspace', 'Workspace root (shared)')
        lsp_sources(self.home, 'omp:home', 'Home (shared)')
        for name in ('mcp.json', '.mcp.json'):
            path = self.project / name
            if present(path):
                add(f'omp:workspace:mcp:{name}', f'Workspace root · {name} (shared)', path, 'mcp')
        for index, raw in enumerate(islice(filter(None, self.env.get('PI_CONFIG_FILES', '').split(os.pathsep)), 32), 1):
            path = Path(os.path.abspath(self.project / Path(raw).expanduser()))
            add(f'omp:overlay:{index}', f'OMP launch overlay {index}', path, 'settings',
                'Launch overlays are read-only here.')
        return found

    def hints(self):
        sources = self.sources()
        for sid in ('omp:active:settings', 'omp:project:settings-legacy', 'omp:project:settings',
                    *(key for key in sources if key.startswith('omp:overlay:')),
                    'pi:user:mcp', 'pi:project:mcp', 'omp:active:mcp', 'omp:active:mcp-legacy',
                    'omp:project:mcp', 'omp:project:mcp-legacy', 'omp:workspace:mcp:mcp.json',
                    'omp:workspace:mcp:.mcp.json'):
            source = sources.get(sid)
            if source and present(source.path):
                try:
                    raw = read_file(source.path)[0]
                    yield source, document(source.path, raw, jsonc=not source.pi, overlay=source.overlay)[0]
                except DeskError:
                    continue  # The source is listed in Settings, where its read error is visible.

    def fields(self, source, payload):
        kind = source.document
        if source.pi and kind != 'mcp':
            return pi_fields(kind), ''
        if kind == 'settings':
            try:
                native = self.registry.fields()
                return {key: (tuple(key.split('.')), field) for key, field in native.items()}, ''
            except DeskError as error:
                return {}, str(error) + ' Stored fields are shown read-only.'
        if kind == 'keybindings':
            return {**omp_bindings(), **{key: ((key,), {'type': 'json'}) for key in payload}}, ''
        field_types = {'providers': 'object'} if kind == 'models' else {'mcpServers': 'object', '$schema': 'string'}
        if kind == 'mcp' and source.pi:
            field_types['autoEnableCodemode'] = 'boolean'
        elif kind == 'mcp' and not source.project:
            field_types.update(disabledServers='array', enabledServers='array')
        elif kind == 'lsp':
            field_types = {'idleTimeoutMs': 'number'}
            if 'servers' in payload or not set(payload) - field_types.keys():
                field_types['servers'] = 'object'
            else:
                field_types.update({key: 'object' for key in payload if key not in field_types})
        return {key: ((key,), {'type': kind}) for key, kind in field_types.items()}, ''

    def revision(self, source, state):
        raw, identity = state
        sig = blake2s(key=self.signature)
        sig.update(f'{source.id}\0{source.path}\0{identity}'.encode('utf-8'))
        if raw is not None:
            sig.update(raw)
        return sig.hexdigest()

    def open_source(self, source, sources, *, state=None):
        state = read_file(source.path) if state is None else state
        payload, comments = document(source.path, state[0], jsonc=not source.pi, overlay=source.overlay)
        fields, notice = self.fields(source, payload)
        revision = self.revision(source, state)
        result = []
        for key, (path, field) in fields.items():
            value = lookup(payload, path)
            row = {'key': key, 'type': field['type'], 'description': field.get('description', ''),
                   'stored': value is not MISSING, 'secret': sensitive(path, value),
                   'readOnly': source.pi and source.project and key == 'defaultProjectTrust'}
            if 'options' in field:
                row['options'] = field['options']
            if value is not MISSING:
                row['value'] = masked(path, value)
            if 'value' in field and not row['secret']:
                row['default'] = masked(path, field['value'])
            result.append(row)
        represented = {path[0] for path, _ in fields.values()}
        meta = source.metadata()
        meta['readOnly'] = source.restricted or ('JSON comments are preserved; edit this file with the native editor.'
                                                  if comments else notice)
        return {'source': meta, 'sources': [item.metadata() for item in sources.values()], 'fields': result,
                'unlisted': {k: masked((k,), v) for k, v in payload.items() if k not in represented},
                'revision': revision, 'canUndo': bool(self.undo and self.undo[:2] == (source.id, revision)),
                'canAdd': not meta['readOnly'] and (source.document == 'keybindings' or source.document == 'lsp'
                                                    and 'servers' not in fields)}

    def validate(self, source, payload, key, value):
        kind = source.document
        if source.pi and kind != 'mcp':
            validate_pi(kind, payload)
        elif kind == 'keybindings' and value is not MISSING and not binding_value(value):
            raise DeskError('Use a key chord or a list of key chords.', 422)
        elif kind == 'mcp' and key == 'mcpServers' and value is not MISSING and not all(
                isinstance(entry, dict) for entry in value.values()):
            raise DeskError('Each MCP server must be a JSON object.', 422)
        elif kind == 'mcp' and key in ('enabledServers', 'disabledServers') and value is not MISSING and not all(
                isinstance(entry, str) for entry in value):
            raise DeskError('Use a list of MCP server names.', 422)
        elif kind == 'lsp' and key == 'servers' and value is not MISSING and not all(
                isinstance(entry, dict) for entry in value.values()):
            raise DeskError('Each language server must be a JSON object.', 422)

    def dispatch(self, body):
        with self.lock:
            action = body.get('action')
            sources = self.sources()
            if action == 'sources':
                return {'sources': [source.metadata() for source in sources.values()]}
            sid = body.get('source')
            source = sources.get(sid) if isinstance(sid, str) else None
            if source is None:
                raise DeskError('Select a discovered configuration source.', 404)
            state = read_file(source.path)
            if action == 'open':
                return self.open_source(source, sources, state=state)
            if action not in ('save', 'reset', 'undo') or body.get('confirm') is not True:
                raise DeskError('Confirm the exact configuration edit before saving.')
            revision = self.revision(source, state)
            if body.get('revision') != revision:
                raise DeskError('The file changed since you opened it. Reload the source before editing.', 409)
            if source.restricted:
                raise DeskError(source.restricted, 403)
            if action == 'undo':
                if not self.undo or self.undo[:2] != (sid, revision):
                    raise DeskError('No settings change can be undone at this revision.', 409)
                replace_file(source.path, state, self.undo[2])
                self.undo = None
                return self.open_source(source, sources)
            payload, comments = document(source.path, state[0], jsonc=not source.pi)
            if comments:
                raise DeskError('Edit this comment-bearing JSON file in the native editor.', 403)
            fields, notice = self.fields(source, payload)
            if notice:
                raise DeskError(notice, 503)
            key = body.get('key')
            if not isinstance(key, str) or len(key) > 250:
                raise DeskError('Select an editable setting.')
            item = fields.get(key)
            if not item and SETTING_KEY.fullmatch(key) and (source.document == 'keybindings' or
                    source.document == 'lsp' and 'servers' not in fields):
                item = ((key,), {'type': 'json' if source.document == 'keybindings' else 'object'})
            if not item or source.pi and source.project and key == 'defaultProjectTrust':
                raise DeskError('This setting is not editable in the selected source.', 403)
            path, field = item
            if any(part in UNSAFE_KEYS for part in path):
                raise DeskError('This setting key cannot be edited.', 403)
            old = lookup(payload, path)
            new = MISSING
            if action == 'save':
                if 'value' not in body:
                    raise DeskError('Enter a replacement value.')
                try:
                    check_tree(body['value'], writing=True)
                except ValueError:
                    raise DeskError('Value is too deeply nested or has unsupported keys.', 422) from None
                new = restore(path, body['value'], old)
                value_type(new, field)
                if not source.pi and source.document == 'settings' and new != old:
                    # The registry may normalize parsed values (for example, flooring a concurrency limit).
                    checked = self.registry.validate(key, new)
                    if checked is not None:
                        new = checked
            if (new is not MISSING and old is not MISSING and type(new) is type(old) and new == old or
                    new is MISSING and old is MISSING):
                return self.open_source(source, sources, state=state)
            edit_field(payload, path, new)
            self.validate(source, payload, key, new)
            encoded = encode_document(source.path, payload, state[0])
            replace_file(source.path, state, encoded)
            current = read_file(source.path)
            self.undo = (sid, self.revision(source, current), state[0]) if current[0] == encoded else None
            return self.open_source(source, sources, state=current)
