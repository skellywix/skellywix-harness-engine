"""One private SQLite workspace. A revision is visible only after its gate commits."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
import stat

from . import routing

APP_ID = 0x534B5831
DB_NAME = 'engine.sqlite3'
SCHEMA = (
    'CREATE TABLE guides (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
    'CREATE TABLE cases (words TEXT PRIMARY KEY, cohort TEXT NOT NULL, expected TEXT NOT NULL, source TEXT NOT NULL)',
    'CREATE TABLE control (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, seen INTEGER NOT NULL, revision INTEGER NOT NULL, live TEXT NOT NULL, previous TEXT, checked TEXT)',
    'CREATE TABLE receipts (seq INTEGER PRIMARY KEY, body TEXT NOT NULL)',
)
DEMO = (
    ('train', 'test-change', ['flaky', 'suite']),
    ('check', 'test-change', ['flaky', 'intermittent']),
    ('check', 'review-release', ['publish', 'bundle']),
    ('check', 'none', ['unclassified']),
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _file(home):
    root = Path(home).expanduser().absolute()
    path = root / DB_NAME
    if root.is_symlink() or path.is_symlink():
        raise ValueError('Use a dedicated workspace, not a linked directory or database.')
    if path.exists():
        s = path.stat()
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            raise ValueError('The database must be a regular file owned by this workspace.')
    return path


class WorkspaceMissing(ValueError):
    """The selected state directory does not have an engine database."""


@contextmanager
def _transaction(home, *, writer=False, creating=False):
    path = _file(home)
    if not creating and not path.is_file():
        raise WorkspaceMissing('No workspace here. Run init first.')
    mode = 'rwc' if creating else 'rw' if writer else 'ro'
    with closing(sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, isolation_level=None, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN IMMEDIATE' if writer or creating else 'BEGIN')
        schema_id = db.execute('PRAGMA application_id').fetchone()[0]
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if not (schema_id == APP_ID and version == 1) and not (creating and schema_id == 0 and version == 0):
            raise ValueError('Unrecognized database or schema version; nothing was changed.')
        # Closing an uncommitted connection on any exception rolls back the transaction.
        yield db
        db.commit()


def _event(db, decision, revision, **fields):
    item = dict(at=_now(), decision=decision, revision=revision, **fields)
    seq = db.execute('INSERT INTO receipts(body) VALUES (?)', (_encoded(item),)).lastrowid
    db.execute('DELETE FROM receipts WHERE seq <= ?', (seq - 128,))
    return item


def initialize(home, *, demo=False):
    path = _file(home)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    existed = path.exists()
    if not existed and any(path.parent.iterdir()) and not path.exists():
        raise ValueError('Initialize an empty dedicated directory, not an existing project.')
    with _transaction(home, writer=True, creating=not existed) as db:
        if db.execute('PRAGMA application_id').fetchone()[0] == APP_ID:
            if demo:
                raise ValueError('Demo data requires a new workspace. Existing state was preserved.')
            return {'decision': 'existing'}
        if existed or db.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone():
            raise ValueError('Refusing to initialize an unrelated database.')
        for command in SCHEMA:
            db.execute(command)
        db.execute(f'PRAGMA application_id={APP_ID}')
        db.execute('PRAGMA user_version=1')
        guides = json.loads(files(__package__).joinpath('starter.json').read_text(encoding='utf-8'))
        for entry in guides:
            item = routing.guide(entry)
            db.execute('INSERT INTO guides VALUES (?,?)', (item['id'], _encoded(item)))
        db.execute("INSERT INTO control VALUES (1,0,-1,1,'{}',NULL,NULL)")
        if demo:
            for cohort, target, labels in DEMO:
                _record(db, cohort, target, labels, 'demo')
        return _event(db, 'initialized', 1, demo=demo)


def _state(db):
    return db.execute('SELECT * FROM control WHERE id=1').fetchone()


def _guides(db):
    return {r['id']: json.loads(r['body']) for r in db.execute('SELECT * FROM guides ORDER BY id')}


def _cases(db, cohort):
    return [(r['words'].split(), None if r['expected'] == 'none' else r['expected'])
            for r in db.execute('SELECT words,expected FROM cases WHERE cohort=? ORDER BY words', (cohort,))]


def _record(db, cohort, target, values, source):
    words = ' '.join(routing.tags(values))
    if cohort not in ('train', 'check') or (target == 'none' and cohort != 'check'):
        raise ValueError('Use train or check; none is valid only for a checked abstention.')
    routing.token(target)
    if target != 'none' and not db.execute('SELECT 1 FROM guides WHERE id=?', (target,)).fetchone():
        raise ValueError('Unknown guide ID. Register the approved guide first.')
    row = db.execute('SELECT cohort,expected FROM cases WHERE words=?', (words,)).fetchone()
    if row:
        if tuple(row) == (cohort, target):
            return {'decision': 'duplicate'}
        raise ValueError('Those tags already have a label or belong to the other split.')
    if db.execute('SELECT count(*) FROM cases').fetchone()[0] >= routing.MAX_CASES:
        raise ValueError('Workspace is full. Preserve it and initialize a new evaluation window.')
    db.execute('INSERT INTO cases VALUES (?,?,?,?)', (words, cohort, target, source))
    db.execute('UPDATE control SET generation=generation+1 WHERE id=1')
    return {'decision': 'recorded', 'cohort': cohort}


def record(home, cohort, target, values):
    with _transaction(home, writer=True) as db:
        return _record(db, cohort, target, values, 'local')


def add_guide(home, value):
    item = routing.guide(value)
    with _transaction(home, writer=True) as db:
        guides = _guides(db)
        if item['id'] in guides or len(guides) >= routing.MAX_GUIDES:
            raise ValueError('Guide ID already exists or the guide limit has been reached.')
        state = _state(db)
        used = routing.index(guides, json.loads(state['live']))
        if any(word in used for word in item['tags']):
            raise ValueError('Choose new native tags; an existing guide already owns one.')
        db.execute('INSERT INTO guides VALUES (?,?)', (item['id'], _encoded(item)))
        db.execute('UPDATE control SET generation=generation+1,revision=revision+1,previous=NULL WHERE id=1')
        return _event(db, 'registered', state['revision'] + 1, guide=item['id'])


def resolve(home, values):
    words = routing.tags(values)
    with _transaction(home) as db:
        state = _state(db)
        guides = _guides(db)
        target = routing.match(routing.index(guides, json.loads(state['live'])), words)
        return {'revision': state['revision'], 'guide': guides.get(target)}


def run(home):
    with _transaction(home, writer=True) as db:
        state = _state(db)
        db.execute('UPDATE control SET checked=? WHERE id=1', (_now(),))
        rev = state['revision']
        if state['seen'] == state['generation']:
            return {'decision': 'unchanged', 'revision': rev}
        guides = _guides(db)
        candidate = routing.propose(guides, _cases(db, 'train'))
        live = json.loads(state['live'])
        checks = _cases(db, 'check')
        before = routing.grade(routing.index(guides, live), checks)
        after = routing.grade(routing.index(guides, candidate), checks)
        regressions = sum(a and not b for a, b in zip(before, after))
        decision = 'held' if candidate != live else 'evaluated'
        if sum(after) > sum(before) and not regressions:
            decision = 'promoted'
            rev += 1
            db.execute('UPDATE control SET previous=live,live=?,revision=? WHERE id=1', (_encoded(candidate), rev))
        db.execute('UPDATE control SET seen=generation WHERE id=1')
        return _event(db, decision, rev, before=sum(before), after=sum(after),
                      checks=len(checks), regressions=regressions)


def rollback(home):
    with _transaction(home, writer=True) as db:
        state = _state(db)
        if state['previous'] is None:
            raise ValueError('No promoted policy is available to roll back.')
        db.execute('UPDATE control SET live=previous,previous=NULL,revision=revision+1,seen=generation WHERE id=1')
        return _event(db, 'rolled-back', state['revision'] + 1)


def status(home):
    with _transaction(home) as db:
        state = _state(db)
        guides = _guides(db)
        aliases = json.loads(state['live'])
        routes = routing.index(guides, aliases)
        checks = _cases(db, 'check')
        queue = []
        for words, target in checks:
            found = routing.match(routes, words)
            if found != target:
                queue.append({'tags': words, 'expected': target, 'actual': found})
        history = [json.loads(r[0]) for r in db.execute('SELECT body FROM receipts ORDER BY seq DESC')]
        train = db.execute("SELECT count(*) FROM cases WHERE cohort='train'").fetchone()[0]
        demo = db.execute("SELECT count(*) FROM cases WHERE source='demo'").fetchone()[0]
        return {'revision': state['revision'], 'checked': state['checked'], 'pending': state['seen'] != state['generation'],
                'can_rollback': state['previous'] is not None, 'guides': list(guides.values()), 'aliases': aliases,
                'counts': {'train': train, 'check': len(checks)}, 'demo_cases': demo, 'correct': len(checks)-len(queue),
                'queue': queue, 'history': history}
