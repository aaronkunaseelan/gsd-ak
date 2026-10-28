"""Store access and the sole DDL entry point. Reads always open mode=ro."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from . import owner


def owner_time(value, day=True):
    """Owner prose only; machine fields remain ISO UTC."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(iso(value))
    if dt.utcoffset() is None:
        raise ValueError('owner time requires a timezone')
    return dt.astimezone(owner.zone()).strftime('%a %H:%M %Z' if day else '%H:%M %Z')


ROOT = Path(__file__).resolve().parent.parent
LIVE = Path.home() / '.local/share/gsd/gsd.sqlite'
TABLES = ('records', 'people', 'projects', 'items', 'meetings', 'record_links', 'source_refs')
KINDS = {'person': 'people', 'project': 'projects', 'item': 'items', 'meeting': 'meetings'}
IDENTITY = {t: ('id',) for t in TABLES} | {
    'record_links': ('from_record_id', 'to_record_id', 'relation_kind'),
    'source_refs': ('source_ref_id',),
}
META = {'seq', 'op', 'change_set_id', 'verb', 'actor', 'at'}
CHANGE_SQL = """CREATE TABLE change_sets (
  change_set_id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
  origin TEXT NOT NULL CHECK (origin IN ('pipeline','agent','owner','port')),
  step_id TEXT, window_until TEXT, note TEXT
) STRICT;"""
NOTION_MAP_SQL = """CREATE TABLE IF NOT EXISTS notion_map (
 record_id TEXT PRIMARY KEY, page_id TEXT, content_hash TEXT,
 body_hash TEXT, published_at TEXT
) STRICT;"""
DRAFT_SQL = """
CREATE TABLE draft_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE draft_change (
  change_id TEXT PRIMARY KEY, why TEXT, confidence REAL, importance REAL,
  grp TEXT,
  decision TEXT CHECK (decision IN ('accept','amend','hold','reject')),
  quote TEXT, decided_by TEXT, decided_at TEXT
) STRICT;
CREATE TABLE draft_ref (short TEXT PRIMARY KEY, qmd_id TEXT NOT NULL) STRICT;
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def iso(value):
    """Canonical UTC windows make SQLite's lexicographic cursor chronological."""
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError, TypeError):
        raise ValueError('window bounds must be zoned ISO timestamps') from None
    if dt.utcoffset() is None:
        raise ValueError('timestamp requires an explicit timezone')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def notion_date_utc(value):
    """Date-only graph/Notion properties retain their existing UTC-midnight boundary.

    Calendar dates are not naive instants. Only this named boundary converts them.
    """
    if len(value) == 10:
        value = datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%dT00:00:00Z')
    return iso(value)


def connect(path, writable=False):
    path = Path(path).expanduser().resolve()
    db = sqlite3.connect(f'file:{quote(str(path))}?mode={"rw" if writable else "ro"}', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    return db


RETIRED_ITEMS = (
    'parent_item_id',
    'parent_project_item_id',
    'thread_url',
    'latest_deliverable_url',
    'calendar_event_id',
    'schedule_status',
    'planned_start',
    'planned_end',
    'estimated_minutes',
    'effort_band',
    'notes',
    'record_status',
)


def migrate_d14(db):
    """Fold current values before dropping obsolete columns; retain all other history."""
    folded = {f: 0 for f in RETIRED_ITEMS}
    db.execute('BEGIN IMMEDIATE')
    try:
        retired = [f for f in RETIRED_ITEMS if f in columns(db, 'items')]
        if retired:
            cid = None
            for row in rows(db, 'items'):
                values = {f: row[f] for f in retired if row[f] not in (None, '')}
                if not values:
                    continue
                if cid is None:
                    cid = 'migration:d14'
                    change_set(db, cid, 'migration', 'port', note='D14 retired item fields')
                for f in values:
                    folded[f] += 1
                row['body_markdown'] = (
                    (row.get('body_markdown') or '')
                    + '\nRetired fields: '
                    + '; '.join(
                        f'{f}={json.dumps(v, ensure_ascii=False)}' for f, v in values.items()
                    )
                )
                row['updated_at'] = now()
                append(db, 'items', row, cid, 'migrate', 'migration')
            # SQLite rejects DROP COLUMN while a trigger mentions it. Reuse the
            # existing required trigger, removing only retired-field predicates.
            triggers = list(
                db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='items'"
                )
            )
            for name_, sql in triggers:
                if any('NEW.' + f in sql for f in retired):
                    db.execute(f'DROP TRIGGER {name_}')
                    if name_ == 'items_upsert_required':
                        for f in retired:
                            sql = sql.replace(' OR NEW.' + f + ' IS NULL', '')
                        db.execute(sql)
            for f in retired:
                db.execute(f'ALTER TABLE items DROP COLUMN {f}')
        if 'other_emails' in columns(db, 'people'):
            db.execute('ALTER TABLE people RENAME COLUMN other_emails TO aliases')
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='source_refs_role_enum'"
        ).fetchone()[0]
        if "'decides'" not in sql:
            db.execute('DROP TRIGGER source_refs_role_enum')
            db.execute(sql.replace("'context')", "'context', 'decides', 'commits')"))
        if has_table(db, 'draft_change'):
            for f in ('needs_owner', 'question'):
                if f in columns(db, 'draft_change'):
                    db.execute(f'ALTER TABLE draft_change DROP COLUMN {f}')
        db.execute('PRAGMA user_version=3')
        db.commit()
    except Exception:
        db.rollback()
        raise
    return folded


def migrate_d29(db):
    """Append ready versions; old awaiting states survive only as history."""
    counts = {'awaiting_user': 0, 'awaiting_external': 0}
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('DROP TRIGGER IF EXISTS items_state_enum')
        db.execute(next(line for line in (ROOT / 'schema.sql').read_text().splitlines()
                        if line.startswith('CREATE TRIGGER items_state_enum ')))
        for row in rows(db, 'items'):
            if row['state'] not in counts:
                continue
            if not any(counts.values()):
                change_set(db, 'migration:d29', 'migration', 'port',
                           note='D29: next_actor owns who moves next')
            counts[row['state']] += 1
            row.update(state='ready', updated_at=now())
            append(db, 'items', row, 'migration:d29', 'migrate', 'migration')
        db.commit()
    except Exception:
        db.rollback()
        raise
    return counts


def migrate(db, draft=False):
    """Only DDL path, including fresh stores and optional snapshot-only tables."""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > 5:
        raise ValueError('store schema is newer than this CLI')
    trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='items_state_enum'").fetchone()
    if trigger and 'awaiting_' in trigger[0]:
        path = Path(db.execute('PRAGMA database_list').fetchone()[2])
        if str(path) != '.':
            backup = path.parent / 'backups' / ('pre-d29-' + now().replace(':', '-') + '.sqlite')
            backup.parent.mkdir(mode=0o700, exist_ok=True)
            db.execute('VACUUM INTO ?', (str(backup),))
            backup.chmod(0o600)
    if has_table(db, 'records') and not has_table(db, 'feed_positions') and not draft:
        path = Path(db.execute('PRAGMA database_list').fetchone()[2])
        if str(path) != '.':
            backup = path.parent / 'backups' / ('pre-feed-' + now().replace(':', '-') + '.sqlite')
            backup.parent.mkdir(mode=0o700, exist_ok=True)
            db.execute('VACUUM INTO ?', (str(backup),))
            backup.chmod(0o600)
    if not has_table(db, 'records'):
        db.executescript((ROOT / 'schema.sql').read_text())
    elif not has_table(db, 'change_sets'):
        db.executescript('BEGIN IMMEDIATE;\n' + CHANGE_SQL + '\nPRAGMA user_version=1; COMMIT;')
    else:
        db.execute('PRAGMA user_version=1')
    db.executescript(NOTION_MAP_SQL + '''
CREATE TABLE IF NOT EXISTS notion_sources (kind TEXT PRIMARY KEY, data_source_id TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS publish_positions (lane TEXT PRIMARY KEY, position INTEGER NOT NULL) STRICT;
PRAGMA user_version=2;
''')
    factory = db.row_factory
    try:
        db.row_factory = sqlite3.Row
        folded = migrate_d14(db)
        folded.update(migrate_d29(db))
    finally:
        db.row_factory = factory
    from .feed import SQL

    db.executescript('BEGIN IMMEDIATE;\n' + SQL + '\nPRAGMA user_version=4; COMMIT;')
    # A null page identity retains an attempted create until remote discovery
    # resolves it. Rebuild only the auxiliary mapping, never graph history.
    if any(r[1] == 'page_id' and r[3] for r in db.execute('PRAGMA table_info(notion_map)')):
        db.executescript('''BEGIN IMMEDIATE;
ALTER TABLE notion_map RENAME TO notion_map_old;
''' + NOTION_MAP_SQL + '''
INSERT INTO notion_map SELECT * FROM notion_map_old;
DROP TABLE notion_map_old;
COMMIT;''')
    db.execute('PRAGMA user_version=5')
    if draft and not has_table(db, 'draft_meta'):
        db.executescript('BEGIN IMMEDIATE;\n' + DRAFT_SQL + '\nCOMMIT;')
    return folded


def has_table(db, name):
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


PIPELINE_ACTOR = 'pipeline'  # the step runner; only its commits enumerated a whole window


def pipeline_cut(db):
    # A hand-landed draft (any other actor) changes records, not how far the pipeline has read (F-53).
    row = (
        db.execute(
            "SELECT window_until, change_set_id, at FROM change_sets "
            "WHERE origin='pipeline' AND actor=? AND window_until IS NOT NULL "
            "ORDER BY window_until DESC, at DESC, change_set_id DESC LIMIT 1",
            (PIPELINE_ACTOR,),
        ).fetchone()
        if has_table(db, 'change_sets')
        else None
    )
    return dict(zip(('until', 'change_set_id', 'committed_at'), row or (None, None, None)))


def columns(db, table):
    return {r['name']: r['type'] for r in db.execute(f'PRAGMA table_info({table})')}


def key(table, row):
    return tuple(row[f] for f in IDENTITY[table])


def current(db, table, identity, include_deleted=False):
    where = ' AND '.join(f'{f}=?' for f in IDENTITY[table])
    r = db.execute(
        f'SELECT * FROM {table} WHERE {where} ORDER BY seq DESC LIMIT 1', tuple(identity)
    ).fetchone()
    return dict(r) if r and (include_deleted or r['op'] != 'delete') else None


def previous(db, table, row):
    """Immediately preceding version, including tombstones."""
    where = ' AND '.join(f'{f}=?' for f in IDENTITY[table])
    old = db.execute(
        f'SELECT * FROM {table} WHERE {where} AND seq<? ORDER BY seq DESC LIMIT 1',
        (*key(table, row), row['seq']),
    ).fetchone()
    return dict(old) if old else None


def notion_url(page_id):
    return 'https://www.notion.so/' + page_id.replace('-', '') if page_id else None


def rows(db, table):
    return [dict(r) for r in db.execute(f'SELECT * FROM cur_{table} ORDER BY seq')]


def append(db, table, data, change, verb, actor, op='upsert'):
    data = {k: v for k, v in data.items() if k not in META}
    data.update(op=op, change_set_id=change, verb=verb, actor=actor, at=now())
    fs = list(data)
    db.execute(
        f'INSERT INTO {table} ({",".join(fs)}) VALUES ({",".join("?" for _ in fs)})',
        [data[f] for f in fs],
    )


def name(r):
    return (
        r.get('name')
        or r.get('title')
        or r.get('display_name')
        or r.get('canonical_title')
        or r.get('id', '?')
    )


def resolve(db, text, kind=None):
    rs = rows(db, 'records')
    exact = [r for r in rs if r['id'] == text]
    matches = exact or [
        r
        for r in rs
        if r['id'].startswith(text) or (r['canonical_title'] or '').casefold() == text.casefold()
    ]
    if kind:
        matches = [r for r in matches if r['record_kind'] == kind]
    if len(matches) != 1:
        raise ValueError(f'{text}: {"ambiguous" if matches else "unknown"} record reference')
    return matches[0]['id']


def maxima(db):
    return {t: db.execute(f'SELECT COALESCE(MAX(seq),0) FROM {t}').fetchone()[0] for t in TABLES}


def metadata(db):
    return dict(db.execute('SELECT key,value FROM draft_meta'))


def put_meta(db, key_, value):
    db.execute('INSERT OR REPLACE INTO draft_meta VALUES (?,?)', (key_, value))


def change_set(db, cid, actor, origin=None, **extra):
    db.execute(
        'INSERT INTO change_sets VALUES (?,?,?,?,?,?,?)',
        (
            cid,
            now(),
            actor,
            origin or ('owner' if actor == 'owner' else 'agent'),
            extra.get('step_id'),
            extra.get('window_until'),
            extra.get('note'),
        ),
    )
