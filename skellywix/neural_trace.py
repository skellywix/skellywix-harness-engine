"""Project OMP journal evidence by prompt ancestry, not by file position or content."""
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import blake2s
import json
import os
import re
from secrets import token_bytes
from time import monotonic

from .neural_inventory import clean, entries, mapping, plain_file, sequence

MIB = 1024 * 1024
SAMPLE_FILES = 12
MAX_LINE = 2 * MIB
MAX_WORK = 12 * MIB
MAX_LINKS = 12_288
MAX_PROMPTS = 24
MAX_EVENTS = 192
CALL_ID = re.compile(r'[A-Za-z0-9_@.|:/+\-]{1,192}\Z')
MCP_ROUTE = re.compile(r'xd://(mcp__[a-zA-Z0-9_]{1,59})\Z')
OUTCOMES = frozenset(('allowed', 'blocked', 'denied', 'skipped', 'warned', 'pass', 'fail', 'error', 'warn', 'allow', 'block'))


def call_id(value):
    return value if isinstance(value, str) and CALL_ID.fullmatch(value) else None


def stamp(value):
    if not isinstance(value, str) or not 19 <= len(value) <= 35:
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix('Z') + ('+00:00' if value.endswith('Z') else ''))
        return parsed.astimezone(timezone.utc).isoformat(timespec='milliseconds') if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def device(tool, args):
    # read(xd://...) describes a device; only write(...) dispatches it.
    path = mapping(args).get('path')
    match = MCP_ROUTE.fullmatch(path) if tool == 'write' and isinstance(path, str) else None
    return match[1] if match else None


class Journal:
    def __init__(self, path, identity, key, offset):
        self.path = path
        self.identity = identity
        self.key = key
        self.namespace = blake2s(os.fsencode(path), key=key, digest_size=16).digest()
        self.offset = offset
        self.discard = offset != 0
        self.seen_size = None
        self.limited = self.discard
        self.links = OrderedDict()
        self.tool_frames = OrderedDict()
        self.prompts = OrderedDict()
        self.calls = OrderedDict()

    def opaque(self, entry, suffix=''):
        return blake2s(self.namespace + (entry + ':' + suffix).encode('ascii'), key=self.key, digest_size=10).hexdigest()

    def event(self, entry, when, kind, name, state, *, suffix='', via=None):
        event = {'id': self.opaque(entry, suffix), 'kind': kind, 'name': name, 'state': state, 'at': when}
        if via:
            event['via'] = via
        return event

    def push(self, prompt, event):
        trace = self.prompts.get(prompt)
        if trace is None:
            return False
        if len(trace['events']) >= MAX_EVENTS:
            # Keep the prompt and the live tail. The UI explicitly labels omissions.
            del trace['events'][1]
            trace['omitted'] += 1
        trace['events'].append(event)
        return True

    def descends_from(self, parent, ancestor):
        while parent and parent in self.links:
            if parent == ancestor:
                return True
            parent = self.links[parent][0]
        return False

    def record(self, row):
        entry = call_id(row.get('id'))
        if not entry or entry in self.links:
            return
        parent = call_id(row.get('parentId'))
        # Only links already seen can be parents; never guess from adjacent files or branches.
        parent = parent if parent in self.links else None
        _, prompt, frame, prelude = self.links[parent] if parent else (None, None, None, None)
        when = stamp(row.get('timestamp'))
        row_type = row.get('type')
        msg = mapping(row.get('message'))
        data = mapping(row.get('data'))
        role = msg.get('role')
        tag = row.get('customType')
        if row_type == 'message' and role == 'user':
            prompt, frame = entry, None
            if when:
                trace = {'id': self.opaque(entry), 'at': when, 'events': [], 'omitted': 0}
                self.prompts[entry] = trace
                if len(self.prompts) > MAX_PROMPTS:
                    self.prompts.popitem(last=False)
                self.push(prompt, self.event(entry, when, 'prompt', 'Prompt submitted', 'recorded'))
                if prelude:
                    self.push(prompt, self.event(prelude[0], prelude[1], 'memory', 'recall', 'injected',
                                                 suffix=entry))
            prelude = None
        elif row_type == 'message' and role == 'assistant':
            frame = entry
            bridges = {}
            for candidate in sequence(msg.get('content'))[:96]:
                block = mapping(candidate)
                if block.get('type') == 'toolCall' and (ident := call_id(block.get('id'))):
                    if target := device(block.get('name'), block.get('arguments')):
                        bridges[ident] = target
            if bridges:
                self.tool_frames[entry] = bridges
                if len(self.tool_frames) > 256:
                    self.tool_frames.popitem(last=False)
            provider = clean(msg.get('provider'), model=True)
            model = clean(msg.get('model'), model=True)
            if when and provider and model and (name := clean(f'{provider}/{model}', model=True)):
                reason = msg.get('stopReason')
                state = 'error' if reason == 'error' else 'aborted' if reason == 'aborted' else 'recorded'
                self.push(prompt, self.event(entry, when, 'model', name, state))
        elif row_type == 'custom' and tag == 'tool_execution_start' and when:
            tool = clean(data.get('toolName'))
            ident = call_id(data.get('toolCallId'))
            if tool and ident and prompt in self.prompts:
                route = device(tool, data.get('args'))
                if tool == 'write' and not route and 'args' not in data:
                    route = self.tool_frames.get(frame, {}).get(ident)
                event = self.event(entry, stamp(data.get('startedAt')) or when, 'tool', route or tool, 'started',
                                   via=tool if route else None)
                self.push(prompt, event)
                self.calls[(prompt, ident)] = (entry, tool, event)
                self.calls.move_to_end((prompt, ident))
                if len(self.calls) > MAX_PROMPTS * MAX_EVENTS:
                    self.calls.popitem(last=False)
        elif row_type == 'message' and role == 'toolResult' and when:
            ident = call_id(msg.get('toolCallId'))
            tool = clean(msg.get('toolName'))
            if tool and ident and prompt in self.prompts:
                outcome = msg.get('isError')
                state = 'error' if outcome is True else 'ok' if outcome is False else 'recorded'
                match = self.calls.get((prompt, ident))
                if match and match[1] == tool and self.descends_from(parent, match[0]):
                    match[2].update(state=state, completedAt=when)
                    del self.calls[(prompt, ident)]
                else:
                    # A result without its start is evidence of the reported tool, not its proposed route.
                    self.push(prompt, self.event(entry, when, 'tool', tool, state))
        elif row_type == 'custom' and tag == 'memory_recall' and when:
            if prompt is None:
                prelude = (entry, when)
            else:
                self.push(prompt, self.event(entry, when, 'memory', 'recall', 'injected'))
        elif row_type == 'ttsr_injection' and when:
            names = dict.fromkeys(name for item in sequence(row.get('rules'))[:64] if (name := clean(item)))
            for index, rule in enumerate(names):
                self.push(prompt, self.event(entry, when, 'ttsr', rule, 'injected', suffix=str(index)))
        elif row_type == 'custom' and tag == 'harness.guard_decision' and when:
            name = clean(data.get('guard_id'))
            outcome = data.get('outcome')
            if name and isinstance(outcome, str) and outcome in OUTCOMES:
                self.push(prompt, self.event(entry, when, 'hook', name, outcome))
        elif row_type == 'compaction' and when:
            self.push(prompt, self.event(entry, when, 'runtime', 'Context compaction', 'recorded'))
        self.links[entry] = (parent, prompt, frame, prelude)
        if len(self.links) > MAX_LINKS:
            self.links.popitem(last=False)
            self.limited = True

    def read(self, size, budget):
        if self.seen_size == size or budget <= 0:
            return 0
        with self.path.open('rb') as source:
            source.seek(self.offset)
            data = source.read(budget)
        start = 0
        consumed = len(data)
        while start < len(data):
            end = data.find(b'\n', start)
            if end < 0:
                if self.discard or len(data) - start >= MAX_LINE:
                    self.discard = True
                    self.limited = True
                else:
                    consumed = start  # Leave the incomplete row for the next append.
                break
            if self.discard or end - start > MAX_LINE:
                self.discard = False
                self.limited = True
            else:
                try:
                    row = mapping(json.loads(data[start:end]))
                except (ValueError, RecursionError):
                    self.limited = True
                else:
                    self.record(row)
            start = end + 1
        self.offset += consumed
        if self.offset >= size or self.offset - consumed + len(data) >= size:
            self.seen_size = size
        return len(data)


class TraceReader:
    def __init__(self, root, *, preferred=None):
        self.root = root
        self.preferred = preferred
        self.key = token_bytes(16)
        self.journals = {}
        self.catalogue = ()
        self.refresh_at = 0.0
        self.coverage = 'No recent OMP session journal found'

    def candidates(self):
        paths = set()
        buckets = [(self.root, 1024)]
        folders = []
        for path in entries(self.root):
            try:
                if path.is_symlink():
                    continue
                if path.is_dir() and not path.name.startswith('.'):
                    folders.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue  # A directory removed between enumeration and stat is no longer a source.
        buckets.extend((path, 2048) for _, path in sorted(folders, reverse=True)[:24])
        for folder, bound in buckets:
            paths.update(path for path in entries(folder, limit=bound) if path.suffix == '.jsonl')
        if self.preferred:
            preferred = self.preferred.absolute()
            try:
                beneath = preferred.parent.relative_to(self.root.absolute())
            except ValueError:
                beneath = None
            if (beneath is not None and '..' not in beneath.parts and len(beneath.parts) <= 1
                    and preferred.suffix == '.jsonl' and preferred in entries(preferred.parent, limit=4096)):
                paths.add(preferred)
        found = []
        for path in paths:
            try:
                info = plain_file(path)
            except OSError:
                continue  # Disappeared, unreadable and nonregular journals are not inputs.
            found.append((path, info))
        return sorted(found, key=lambda item: (item[0] == self.preferred, item[1].st_mtime_ns, item[0]),
                      reverse=True)[:SAMPLE_FILES]

    def scan(self):
        now = monotonic()
        if now >= self.refresh_at:
            candidates = self.candidates()
            self.catalogue = tuple(path for path, _ in candidates)
            self.refresh_at = now + 3
        else:
            candidates = []
            for path in self.catalogue:
                try:
                    candidates.append((path, plain_file(path)))
                except OSError:
                    self.refresh_at = 0.0
        self.journals = {p: self.journals[p] for p, _ in candidates if p in self.journals}
        used = 0
        unavailable = False
        for index, (path, info) in enumerate(candidates):
            journal = self.journals.get(path)
            identity = (info.st_dev, info.st_ino)
            window = 6 * MIB if index == 0 else 512 * 1024
            if journal is None or journal.identity != identity or info.st_size < journal.offset:
                journal = self.journals[path] = Journal(path, identity, self.key, max(0, info.st_size - window))
            try:
                budget = min(MAX_WORK - used, 6 * MIB if index == 0 else MAX_LINE + 1)
                if budget > MAX_LINE or 0 < info.st_size - journal.offset <= budget:
                    used += journal.read(info.st_size, budget)
            except OSError:
                unavailable = True
        traces = [trace for journal in self.journals.values() for trace in journal.prompts.values()]
        limited = unavailable or any(j.limited for j in self.journals.values()) or used >= MAX_WORK
        if candidates:
            self.coverage = ('Recent journal tails; some records outside the readable window' if limited
                             else 'Recent native OMP journals; per-file prompt ancestry')
        else:
            self.coverage = 'No recent OMP session journal found'
        latest = sorted(reversed(traces), key=lambda trace: trace['at'], reverse=True)[:MAX_PROMPTS]
        # A read keeps immutable snapshots; a subsequent poll can finish a previously returned call.
        return [dict(trace, events=[event.copy() for event in trace['events']]) for trace in latest]
