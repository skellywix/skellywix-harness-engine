"""Apply owner-authored repairs to recognized, existing harness artifacts."""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re

from .neural_inventory import RULE
from .settings_catalog import MISSING, DeskError, sensitive, validate_pi
from .settings_desk import (MAX_DOCUMENT, Source, binding_value, document, edit_field, encode_document, lookup,
                            replace_file, read_file)

MAX_REPAIRS = 16
ID = re.compile(r'[a-z][a-z0-9-]{0,47}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
FIELD = re.compile(r'[A-Za-z0-9_@][A-Za-z0-9_.@+:/ -]{0,143}\Z')
PART = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}\Z')
SCRIPTS = frozenset(('.ts', '.js', '.mts', '.mjs', '.cjs'))
PREFERENCES = {
    'omp': frozenset((('startup', 'showSplash'), ('startup', 'quiet'))),
    'pi': frozenset((('quietStartup',), ('hideThinkingBlock',), ('collapseChangelog',), ('showHardwareCursor',))),
}
REVISION_INPUT = {'type': 'object', 'additionalProperties': False, 'required': ['id', 'revision'], 'properties': {
    'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{0,47}$'},
    'revision': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
}}
TOOLS = (
    {'name': 'harness_repairs', 'description': 'List exact local repairs approved at launch, decisions and current revisions.',
     'inputSchema': {'type': 'object', 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'harness_apply_repair', 'description': 'Apply an existing owner-approved repair at its current revision.',
     'inputSchema': REVISION_INPUT, 'annotations': {'destructiveHint': True, 'openWorldHint': False}},
    {'name': 'harness_undo_repair', 'description': 'Undo a repair written by this process if its revision has not changed.',
     'inputSchema': REVISION_INPUT, 'annotations': {'destructiveHint': True, 'openWorldHint': False}},
)


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def model_value(key, value):
    if key in ('contextWindow', 'maxContextWindow', 'maxTokens'):
        return positive(value)
    if key == 'reasoning':
        return type(value) is bool
    if key in ('id', 'name'):
        return matches(FIELD, value) and value == value.strip()
    if key == 'input':
        return (isinstance(value, list) and 0 < len(value) <= 2
                and all(item in ('text', 'image') for item in value)
                and len(set(value)) == len(value))
    return False


def model_rows(value):
    return (isinstance(value, list) and len(value) <= 256
            and all(isinstance(row, dict) and 'id' in row and all(model_value(k, v) for k, v in row.items()) for row in value)
            and len({row['id'] for row in value}) == len(value))


def field_allowed(source, field, value):
    kind = source.document
    if any(key in ('__proto__', 'prototype', 'constructor') for key in field) or sensitive(field, value):
        return False
    if kind == 'settings':
        harness = 'pi' if source.pi else 'omp'
        if field in PREFERENCES[harness]:
            return type(value) is bool
        return field in ((('theme',),) if source.pi else (('theme', 'dark'), ('theme', 'light'))) and matches(PART, value)
    if kind == 'keybindings':
        return len(field) == 1 and binding_value(value) and all(
            0 < len(chord) <= 64 and chord.isprintable() for chord in ([value] if isinstance(value, str) else value))
    if kind == 'models' and field[0] == 'providers':
        if len(field) == 3 and field[2] == 'models':
            return model_rows(value)
        return (not source.pi and len(field) == 5 and field[2] == 'modelOverrides'
                and field[-1] != 'id' and model_value(field[-1], value))
    if kind == 'mcp' and len(field) == 3 and field[0] == 'mcpServers':
        if field[-1] == 'enabled':
            return value is False
        if not source.pi:
            if field[-1] == 'timeout':
                return positive(value)
            if field[-1] == 'instructions':
                return value is False
            if field[-1] == 'requestIdFormat':
                return value in ('number', 'string')
    if kind == 'lsp':
        if field == ('idleTimeoutMs',):
            return positive(value)
        server = len(field) == 2 or len(field) == 3 and field[0] == 'servers'
        return server and (field[-1] == 'disabled' and value is True
                           or field[-1] == 'warmupTimeoutMs' and positive(value))
    return False


def artifact(root, path, *, pi):
    if not path.is_relative_to(root):
        return None
    parts = path.relative_to(root).parts
    if not parts or any(not PART.fullmatch(part) or part.endswith('.') for part in parts):
        return None
    if len(parts) == 3 and parts[-1] == 'SKILL.md' and (
            parts[0] == 'skills' or not pi and parts[0] == 'managed-skills'):
        return 'skill'
    if not pi and len(parts) == 2 and parts[0] == 'rules' and path.suffix == '.md':
        return 'ttsr'
    if path.suffix not in SCRIPTS or path.name.endswith('.d.ts'):
        return None
    prefix = 2 if parts[:2] in (('hooks', 'pre'), ('hooks', 'post')) else 1
    if len(parts) != prefix + 1 and not (len(parts) == prefix + 2 and path.name == 'index.ts'):
        return None
    if parts[0] == 'extensions' or not pi and parts[0] == 'tools':
        return parts[0][:-1]
    if not pi and prefix == 2:
        return 'hook'
    return None


def approved_text(source, value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ValueError('Expected nonempty UTF-8 replacement text')
    data = value.encode('utf-8')
    if len(data) > MAX_DOCUMENT:
        raise ValueError('Oversized replacement')
    if source.document in ('skill', 'ttsr'):
        lines = value.removeprefix('\ufeff').splitlines()
        if len(lines) < 4 or lines[0] != '---' or '---' not in lines[1:]:
            raise ValueError('Missing front matter')
        header = '\n'.join(lines[1:lines.index('---', 1)])
        payload, _ = document(source.path.with_suffix('.yml'), header.encode('utf-8'))
        if source.document == 'skill':
            if payload.get('name') != source.path.parent.name or not str_value(payload.get('description')):
                raise ValueError('Invalid skill metadata')
        elif not RULE.search(header):
            raise ValueError('Missing TTSR selector')
    return data


def str_value(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class Repair:
    id: str
    source: Source
    preimage: str
    automatic: bool
    field: tuple[str, ...] = ()
    value: object = None
    replacement: bytes | None = None

    def encode(self, raw):
        if self.replacement is not None:
            return self.replacement
        source, field, value = self.source, self.field, self.value
        data, comments = document(source.path, raw, jsonc=not source.pi)
        if comments:
            raise ValueError('A native edit is required for JSON with comments')
        old = lookup(data, field)
        if sensitive(field, old) or source.document == 'models' and field[-1] == 'models' and old is not MISSING and not model_rows(old):
            raise ValueError('The existing field is outside supervisor repair scope')
        # Do not synthesize servers, providers, or overrides that were never declared.
        if source.document in ('mcp', 'lsp', 'models') and len(field) > 1:
            if not isinstance(lookup(data, field[:-1]), dict):
                raise ValueError('The approved parent is not in this configuration')
            if source.document == 'lsp' and len(field) == 2 and 'servers' in data:
                raise ValueError('Use the wrapped language-server field')
        if type(old) is type(value) and old == value:
            return raw
        edit_field(data, field, value)
        if source.pi and source.document in ('settings', 'models', 'keybindings'):
            validate_pi(source.document, data)
        return encode_document(source.path, data, raw)


class Repairs:
    def __init__(self, entries, desk, policy_current):
        if not isinstance(entries, list) or len(entries) > MAX_REPAIRS:
            raise ValueError('Invalid repair approvals')
        self.desk = desk
        self.policy_current = policy_current
        self.entries = {}
        self.receipts = {}
        self.finished = {}
        self.undo = {}
        if not entries:
            return
        sources = desk.sources()
        overlays = {source.path for source in sources.values() if source.overlay}
        known = {source.path: source for source in sources.values() if not source.restricted and not source.overlay
                 and not source.id.startswith('omp:home:') and source.path not in overlays}
        roots = {(source.path.parent, source.pi) for source in known.values()
                 if source.document == 'mcp' and not source.id.startswith('omp:workspace:')}
        seen = set()
        for entry in entries:
            repair = self.parse(entry, known, roots)
            if repair.source.path in seen or repair.id in self.entries:
                raise ValueError('Duplicate target or repair ID')
            self.entries[repair.id] = repair
            seen.add(repair.source.path)

    @staticmethod
    def parse(entry, known, roots):
        required = {'id', 'path', 'sha256'}
        if not isinstance(entry, dict) or not required <= entry.keys() <= required | {'automatic', 'set', 'replacement'}:
            raise ValueError('Invalid repair approval')
        name, target, digest = (entry[key] for key in ('id', 'path', 'sha256'))
        automatic = entry.get('automatic', False)
        if (not matches(ID, name) or not matches(DIGEST, digest) or type(automatic) is not bool
                or not isinstance(target, str) or not target or '\0' in target):
            raise ValueError('Invalid repair approval')
        path = Path(target)
        if not path.is_absolute() or '..' in path.parts or (os.name == 'nt' and path.drive.startswith('\\\\')):
            raise ValueError('Use a plain absolute local path')
        source = known.get(path)
        if source is not None:
            change = entry.get('set')
            if 'replacement' in entry or not isinstance(change, dict) or change.keys() != {'field', 'value'}:
                raise ValueError('Approve one supported field change')
            field, value = change['field'], change['value']
            if not isinstance(field, list) or not 1 <= len(field) <= 5 or not all(matches(FIELD, key) for key in field):
                raise ValueError('Invalid field path')
            field = tuple(field)
            if not field_allowed(source, field, value):
                raise ValueError('Field or value is outside the repair scope')
            if automatic and (source.document != 'settings' or field not in PREFERENCES['pi' if source.pi else 'omp']):
                raise ValueError('Only display preferences can be repaired automatically')
            return Repair(name, source, digest, automatic, field=field, value=value)
        kinds = {kind for root, pi in roots if (kind := artifact(root, path, pi=pi))}
        if len(kinds) != 1 or automatic or 'set' in entry:
            raise ValueError('Target is not an approved native artifact')
        source = Source(f'repair:{name}', '', path, kinds.pop())
        return Repair(name, source, digest, False, replacement=approved_text(source, entry.get('replacement')))

    def current(self, repair):
        state = read_file(repair.source.path)
        if state[0] is None or os.name != 'nt' and state[1][6] != os.geteuid():
            raise ValueError('Target unavailable')
        return state, self.desk.revision(repair.source, state)

    def status(self):
        rows = []
        authorized = not self.entries or self.policy_current()
        for name, repair in self.entries.items():
            row = {'id': name, 'kind': repair.source.document, 'automatic': repair.automatic, 'state': 'blocked'}
            if not authorized:
                row['reason'] = 'policy_changed'
            else:
                try:
                    state, revision = self.current(repair)
                except ValueError:
                    row['reason'] = 'target_unavailable'
                else:
                    row['revision'] = revision
                    undo = self.undo.get(name)
                    if undo and undo[0] == revision:
                        row['state'] = 'applied'
                        row['canUndo'] = True
                    elif name in self.finished:
                        row['state'] = self.finished[name]
                    elif sha256(state[0]).hexdigest() == repair.preimage and not undo:
                        row['state'] = 'ready'
                    else:
                        row['reason'] = 'preimage_mismatch'
            if name in self.receipts:
                row['lastDecision'] = self.receipts[name]
            rows.append(row)
        return {'schema': 'harness-repairs/1', 'repairs': rows}

    def receipt(self, name, decision, *, revision=None, reason=None):
        result = {'schema': 'harness-repair-receipt/1', 'id': name, 'decision': decision}
        if revision is not None:
            result['revision'] = revision
        if reason is not None:
            result['reason'] = reason
        if name in self.entries:
            self.receipts[name] = result
            if decision in ('undone', 'unchanged', 'indeterminate'):
                self.finished[name] = decision
        return result

    def auto_apply(self):
        for name, repair in self.entries.items():
            if repair.automatic:
                self.change(name, None, undo=False)

    def act(self, args, *, undo):
        if (not isinstance(args, dict) or args.keys() != {'id', 'revision'}
                or not matches(ID, args['id']) or not matches(DIGEST, args['revision'])):
            raise ValueError('Supply only an approved repair ID and its current revision.')
        return self.change(args['id'], args['revision'], undo=undo)

    def change(self, name, expected, *, undo):
        def blocked(reason):
            return self.receipt(name, 'blocked', reason=reason)

        repair = self.entries.get(name)
        if repair is None:
            return blocked('unknown_repair')
        if not self.policy_current():
            return blocked('policy_changed')
        if repair.field:
            source = self.desk.sources().get(repair.source.id)
            if source != repair.source:
                return blocked('source_changed')
        try:
            state, revision = self.current(repair)
        except ValueError:
            return blocked('target_unavailable')
        if expected is not None and expected != revision:
            return blocked('revision_changed')
        saved = self.undo.get(name)
        if undo:
            if saved is None or saved[0] != revision:
                return blocked('undo_unavailable')
            replacement = saved[1]
        else:
            if saved or name in self.finished:
                return blocked('repair_consumed')
            if sha256(state[0]).hexdigest() != repair.preimage:
                return blocked('preimage_mismatch')
            try:
                replacement = repair.encode(state[0])
            except (ValueError, RecursionError):
                return blocked('replacement_invalid')
            if state[0] == replacement:
                return self.receipt(name, 'unchanged', revision=revision)
        if not self.policy_current():
            return blocked('policy_changed')
        try:
            replace_file(repair.source.path, state, replacement, keep_mode=True)
        except DeskError:
            return blocked('write_conflict')
        if undo:
            del self.undo[name]
        try:
            current, revision = self.current(repair)
        except ValueError:
            return self.receipt(name, 'indeterminate', reason='readback_unavailable')
        if current[0] != replacement:
            return self.receipt(name, 'indeterminate', reason='readback_changed')
        if not undo:
            self.undo[name] = revision, state[0]
        return self.receipt(name, 'undone' if undo else 'applied', revision=revision)
