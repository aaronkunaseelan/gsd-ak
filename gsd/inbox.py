"""Notion inbox: one pass every 15 minutes refreshes the root status page and reads owner comments.

It never runs inside steady or publish. A pass skips while publish holds the Notion lock,
so the two never share the connection's rate window, and a Notion outage only delays
comments. Comment text lives only in inbox.sqlite next to the store (gsd D22, D23).
"""

import fcntl
import json
import os
import sqlite3
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db as store
from . import notion

# Webhook events (via mech-d) are the primary path; the page-level sweep is the fallback.
SWEEP = timedelta(hours=24)
MECH_D_ENV = Path.home() / '.config/mech-d/collector.env'
BACKOFF = (1800, 21600)
COPYABLE = ('paragraph', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'quote', 'heading_3', 'callout')
TELEMETRY = Path.home() / '.local/state/telemetry/gsd-inbox.json'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS comments (
 id TEXT PRIMARY KEY, discussion_id TEXT, page_id TEXT NOT NULL, block_id TEXT,
 author_id TEXT, created_time TEXT, edited_time TEXT, text TEXT NOT NULL,
 seen_at TEXT NOT NULL, cleared TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) STRICT;
'''


def inbox_path(path):
    return Path(path).expanduser().resolve().parent / 'inbox.sqlite'


def connect(path):
    target = inbox_path(path)
    fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    db = sqlite3.connect(target)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def get(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def put(db, key, value):
    with db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))


def when(value):
    return datetime.fromisoformat(store.iso(value))


def root_page(client, gdb, db):
    root = get(db, 'root_page')
    if not root:
        source = gdb.execute('SELECT data_source_id FROM notion_sources ORDER BY kind LIMIT 1').fetchone()[0]
        database = client.request('GET', '/data_sources/' + source)['parent']['database_id']
        root = client.request('GET', '/databases/' + database)['parent']['page_id']
        put(db, 'root_page', root)
    return root


def store_comment(db, comment, page, now):
    block = comment['parent'].get('block_id')
    text = ''.join(part.get('plain_text', '') for part in comment.get('rich_text', []))
    edited = comment.get('last_edited_time')
    old = db.execute('SELECT edited_time FROM comments WHERE id=?', (comment['id'],)).fetchone()
    with db:
        if old is None:
            db.execute(
                'INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                (comment['id'], comment.get('discussion_id'), page, block,
                 (comment.get('created_by') or {}).get('id'), comment.get('created_time'), edited, text, now),
            )
            return 1
        if old[0] != edited:
            # An edit is new owner text: it reaches the next run again.
            db.execute('UPDATE comments SET text=?, edited_time=?, seen_at=?, cleared=NULL WHERE id=?',
                       (text, edited, now, comment['id']))
            return 1
    return 0


def read_comments(client, gdb, db, started, sweep=False):
    """Page-level comments on the root and every mapped page, at most every SWEEP.

    Measured 18 Oct: a page id lists only page-level comments, and no comment moves the
    page's last_edited_time, so there is nothing cheaper to poll between sweeps.
    """
    last = get(db, 'last_sweep')
    if not sweep and last and started - when(last) < SWEEP:
        return {'pages': 0, 'new': 0, 'swept': False}
    root = root_page(client, gdb, db)
    pages = [r[0] for r in gdb.execute('SELECT page_id FROM notion_map')]
    now, found = store.now(), 0
    for page in dict.fromkeys([root, *pages]):
        for comment in client.paginate(f'/comments?block_id={page}&page_size=100'):
            found += store_comment(db, comment, page, now)
    put(db, 'last_sweep', store.iso(started.isoformat()))
    put(db, 'comments_through', store.iso(started.isoformat()))
    return {'pages': len(pages) + 1, 'new': found, 'swept': True}


def mech_d():
    env = dict(line.split('=', 1) for line in MECH_D_ENV.read_text().splitlines() if '=' in line and not line.startswith('#'))
    return env['MECH_D_URL'].strip().strip('"').rstrip('/'), env['MECH_D_TOKEN'].strip().strip('"')


def read_events(client, db, fetch=None):
    """comment.* events Notion sent to the mech-d Worker: ids only; each comment is fetched here.

    Covers line comments, which no page listing returns. A comment deleted before the fetch is skipped.
    """
    if fetch is None:
        url, token = mech_d()

        def fetch(after):
            request = urllib.request.Request(f'{url}/api/notion/events?after={after}', headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)['events']
    after, found = int(get(db, 'event_seq') or 0), 0
    while True:
        events = fetch(after)
        for event in events:
            if event['type'] in ('comment.created', 'comment.updated') and event['entity_id']:
                try:
                    comment = client.request('GET', '/comments/' + event['entity_id'])
                except ValueError as exc:
                    if 'HTTP 404' not in str(exc):
                        raise
                else:
                    found += store_comment(db, comment, event['page_id'] or comment['parent'].get('page_id'), store.now())
            after = event['seq']
            put(db, 'event_seq', str(after))
        if len(events) < 500:
            return {'events': found}


def mark(db, block, value, cut):
    with db:
        db.execute('UPDATE comments SET cleared=? WHERE block_id=? AND cleared IS NULL AND seen_at <= ?',
                   (value, block, cut))


def unread(db, block, cut):
    return db.execute('SELECT 1 FROM comments WHERE block_id=? AND cleared IS NULL AND seen_at > ? LIMIT 1',
                      (block, cut)).fetchone() is not None


def clear(client, gdb, db, cut):
    """D22: recreate each commented block a committed run has read, then delete the old one.

    The discussion goes to Notion's trash with the old block; the text stays on the page.
    Only leaf blocks on gsd's own pages; `## ` section markers and anything else keep their comments.
    """
    pending = get(db, 'clearing')
    if pending:
        recovery = json.loads(pending)
        old = recovery['old']
        # Recovery can proceed once a later committed run has consumed replies too.
        consumed = cut
        page = db.execute('SELECT page_id FROM comments WHERE block_id=? LIMIT 1', (old,)).fetchone()
        if not page:
            return 0
        changed = sum(store_comment(db, c, page[0], store.now()) for c in client.paginate(f'/comments?block_id={old}&page_size=100'))
        if changed or unread(db, old, consumed):
            return 0
        if not client.request('GET', f'/blocks/{old}').get('in_trash'):
            client.request('DELETE', f'/blocks/{old}')
        mark(db, old, 'cleared ' + store.now(), consumed)
        put(db, 'clearing', None)
    owned = {r[0] for r in gdb.execute('SELECT page_id FROM notion_map')}
    blocks = db.execute(
        'SELECT DISTINCT block_id, page_id FROM comments WHERE block_id IS NOT NULL AND cleared IS NULL AND seen_at <= ?',
        (cut,),
    ).fetchall()
    done = 0
    for block, page in blocks:
        if page not in owned:
            mark(db, block, 'kept: not a gsd page', cut)
            continue
        # A reply since the run read this block waits for the next run.
        if sum(store_comment(db, c, page, store.now()) for c in client.paginate(f'/comments?block_id={block}&page_size=100')):
            continue
        if unread(db, block, cut):
            continue
        current = client.request('GET', f'/blocks/{block}')
        kind = current['type']
        if current.get('in_trash'):
            mark(db, block, 'kept: block already gone', cut)
            continue
        if current.get('has_children') or kind not in COPYABLE:
            mark(db, block, 'kept: ' + kind, cut)
            continue
        copy = {'type': kind, kind: {k: v for k, v in current[kind].items() if k in ('rich_text', 'color', 'checked', 'icon') and v is not None}}
        parent = current['parent'].get('page_id') or current['parent'].get('block_id')
        try:
            client.request('PATCH', f'/blocks/{parent}/children', {
                'children': [copy], 'position': {'type': 'after_block', 'after_block': {'id': block}}})
        except notion.UncertainWrite:
            # The copy may exist: never delete the original on an unconfirmed copy.
            mark(db, block, 'kept: uncertain copy', cut)
            continue
        put(db, 'clearing', json.dumps({'old': block, 'cut': cut}))
        client.request('DELETE', f'/blocks/{block}')
        mark(db, block, 'cleared ' + store.now(), cut)
        put(db, 'clearing', None)
        done += 1
    return done


def window(selected, path, since, until):
    """The comments lane of a step: comments first seen inside its window, tied to their record."""
    target = inbox_path(path)
    if not target.exists():
        return
    db = sqlite3.connect(f'{target.as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        'SELECT id, page_id, block_id, text, created_time, author_id FROM comments WHERE seen_at > ? AND seen_at <= ? ORDER BY seen_at, id',
        (since, until),
    ).fetchall()
    root = db.execute("SELECT value FROM meta WHERE key='root_page'").fetchone()
    db.close()
    if not rows:
        return
    with store.connect(path) as gdb:
        records = {r['page_id']: r['record_id'] for r in gdb.execute('SELECT record_id, page_id FROM notion_map')}
        titles = {}
        for kind, table, column in (('item', 'cur_items', 'title'), ('project', 'cur_projects', 'name'),
                                    ('person', 'cur_people', 'display_name'), ('meeting', 'cur_meetings', 'title')):
            titles.update({r[0]: (kind, r[1]) for r in gdb.execute(f'SELECT id, {column} FROM {table}')})
    lines = [
        '# Comments on Notion pages',
        'Each comment names its recorded Notion author id; page ownership does not establish who wrote it.',
        '',
    ]
    for n, row in enumerate(rows, 1):
        short = f'nc{n}'
        record = records.get(row['page_id'])
        kind, title = titles.get(record, ('page', ''))
        where = (f'{kind} {record} · {title}' if record else
                 'gsd root page' if root and row['page_id'] == root[0] else f"Notion page {row['page_id']} (not a gsd record)")
        lines.append(f"[{short}] {row['created_time'][:16]} · author {row['author_id'] or 'unknown'} · {where}{' · on a line' if row['block_id'] else ''}")
        lines.append('  ' + ' '.join(row['text'].split()))
        selected['refs'][short] = 'notion-comment:' + row['id']
    selected['views']['comments.md'] = '\n'.join(lines)
    selected['manifest']['comments'] = {'in_window': len(rows), 'via': 'inbox.sqlite seen_at'}
    selected['comments'] = len(rows)


def telemetry(result, now):
    checks = [
        {'name': 'pass', 'ok': result['status'] in ('ok', 'busy'),
         'reason': result.get('reason') or result['status']}, 
        {'name': 'status_page', 'ok': result.get('status_page') in ('published', 'unchanged'),
         'reason': result.get('status_page') or 'not run'},
        {'name': 'comments', 'ok': result.get('comments') not in (None, 'no_capability', 'error'),
         'reason': result.get('comments') if isinstance(result.get('comments'), str) else 'ok'},
        {'name': 'webhook_events', 'ok': isinstance(result.get('events'), int),
         'reason': result['events'] if isinstance(result.get('events'), str) else 'ok'},
    ]
    if result['status'] in ('busy', 'backoff'):
        # No operation was attempted; the pass verdict explains the deferred work.
        checks = checks[:1]
    metrics = [{'name': 'requests', 'labels': {}, 'value': result.get('requests', 0), 'unit': 'count'}]
    payload = {'schema': 'telemetry/1', 'system': 'gsd-inbox', 'at': now,
               'state': 'green' if all(c['ok'] for c in checks) else 'red', 'checks': checks, 'metrics': metrics}
    TELEMETRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.gsd-inbox-', dir=TELEMETRY.parent)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream)
    os.replace(temp, TELEMETRY)


def pull(path, *, sweep=False, do_clear=False, client=None, now=None):
    from . import status_page

    path = Path(path).expanduser().resolve()
    started = now or datetime.now(UTC)
    result = {'status': 'ok'}
    lock_path = Path(str(path) + '.notion.lock')
    with lock_path.open('a') as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Publish is running at the connection's limit: touch nothing this pass.
            result.update(status='busy', reason='busy: Notion publish lock held')
            telemetry(result, started.timestamp())
            return result
        db = connect(path)
        backoff = float(get(db, 'backoff_until') or 0)
        if backoff > time.time():
            until = datetime.fromtimestamp(backoff, UTC).isoformat()
            result.update(status='backoff', reason='Notion rate-limit backoff until ' + until)
            put(db, 'last_pass', json.dumps(result))
            telemetry(result, started.timestamp())
            db.close()
            return result
        notion.gate()
        client = client or notion.Client(notion.token())
        try:
            with store.connect(path) as gdb:
                try:
                    result['comments'] = read_comments(client, gdb, db, started, sweep)
                except ValueError as exc:
                    result['comments'] = 'no_capability' if 'HTTP 403' in str(exc) else 'error'
                try:
                    result['events'] = read_events(client, db)['events']
                except (OSError, ValueError, KeyError) as exc:
                    result['events'] = 'error: ' + type(exc).__name__
                if do_clear and isinstance(result['comments'], dict):
                    result['cleared'] = clear(client, gdb, db, store.pipeline_cut(gdb)['until'] or '')
                result['status_page'] = status_page.publish(path, client, root_page(client, gdb, db), db, started, result['comments'])
            put(db, 'backoff_until', None)
            put(db, 'backoff_s', None)
        except ValueError:
            result['status'] = 'error'
            raise
        finally:
            if client.http_429:
                step = min(BACKOFF[1], 2 * float(get(db, 'backoff_s') or BACKOFF[0] / 2))
                put(db, 'backoff_s', str(step))
                put(db, 'backoff_until', str(time.time() + step))
            result['requests'] = client.requests()['total']
            put(db, 'last_pass', json.dumps(result))
            telemetry(result, time.time())
    return result


def status(path):
    target = inbox_path(path)
    if not target.exists():
        return {'comments': 0, 'last_pass': None}
    db = sqlite3.connect(f'{target.as_uri()}?mode=ro', uri=True)
    counts = dict(db.execute(
        "SELECT CASE WHEN cleared IS NULL THEN 'open' WHEN cleared LIKE 'cleared%' THEN 'cleared' ELSE 'kept' END, count(*) FROM comments GROUP BY 1"
    ).fetchall())
    last = db.execute("SELECT value FROM meta WHERE key='last_pass'").fetchone()
    db.close()
    return {'comments': counts, 'last_pass': json.loads(last[0]) if last else None}


def command(a):
    if a.action == 'status':
        return status(a.db)
    if not a.explicit_db:
        raise ValueError('inbox pull writes Notion and inbox.sqlite: pass --db FILE')
    return pull(a.db, sweep=a.sweep, do_clear=a.clear)
