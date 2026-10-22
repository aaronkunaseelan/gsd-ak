"""One readable spool payload per landed change set; durable spool precedes position.
Replays may append the same native id, which QMD deduplicates under K2.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
from . import db as store


def spool_entry(root, index, db, change):
    payload = json.dumps(event(db, change), ensure_ascii=False, sort_keys=True).encode()
    sha = hashlib.sha256(payload).hexdigest()
    name = sha + '.json'
    with (root / name).open('wb') as dest:
        os.chmod(root / name, 0o600)
        dest.write(payload)
        dest.flush()
        os.fsync(dest.fileno())
    entry = dict(
        schema='qmd.spool-entry.v1',
        source='gsd-events',
        account='',
        key=change['change_set_id'],
        path=name,
        bytes=len(payload),
        sha256=sha,
        captured_at=change['at'],
        state={'labels': {}},
    )
    index.write(json.dumps(entry) + '\n')


def event(db, change):
    cid = change['change_set_id']
    lines = [f'Change set: {cid}', f'Actor: {change["actor"]}', f'Note: {change["note"] or ""}']
    for table in store.TABLES:
        for row_ in db.execute(f'SELECT * FROM {table} WHERE change_set_id=? ORDER BY seq', (cid,)):
            row = dict(row_)
            identity = store.key(table, row)
            prev = store.previous(db, table, row)
            previous = dict(prev) if prev else {}
            rid = row.get('id') or row.get('record_id') or row.get('from_record_id')
            record = store.current(db, 'records', (rid,), True) if rid else None
            lines.append(
                f'\n{row["verb"]} {table}: {store.name(record or row)} ({rid or identity}) [{row["op"]}]'
            )
            for field, value in row.items():
                if field not in store.META and (not prev or previous.get(field) != value):
                    lines.append(f'- {field}: {json.dumps(value, ensure_ascii=False)}')
    return {
        'change_set_id': cid,
        'time': change['at'],
        'actor': change['actor'],
        'title': f'gsd change {cid}',
        'text': '\n'.join(lines),
    }


def publish(path, spool=None):
    root = (
        Path(spool).expanduser()
        if spool
        else Path(os.environ.get('QMD2_HOME', str(Path.home() / '.local/share/qmd2')))
        / 'spool/gsd-events'
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with store.connect(path, True) as db:
            if not store.has_table(db, 'publish_positions'):
                raise ValueError('run gsd migrate first')
            db.execute('BEGIN IMMEDIATE')
            pos = db.execute(
                "SELECT position FROM publish_positions WHERE lane='gsd-events'"
            ).fetchone()
            pos = pos[0] if pos else 0
            changes = list(
                db.execute(
                    'SELECT rowid AS position,* FROM change_sets WHERE rowid>? ORDER BY rowid',
                    (pos,),
                )
            )
            with (root / 'index.jsonl').open('a') as index:
                os.chmod(root / 'index.jsonl', 0o600)
                for change in changes:
                    spool_entry(root, index, db, change)
                index.flush()
                os.fsync(index.fileno())
            fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if changes:
                pos = changes[-1]['position']
                db.execute(
                    "INSERT OR REPLACE INTO publish_positions VALUES ('gsd-events',?)", (pos,)
                )
    return {'published': len(changes), 'position': pos, 'spool': str(root)}
