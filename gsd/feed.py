"""Arrival enumeration and draft-owned transactional checkpoints (D27)."""

import json
from datetime import datetime, timezone

from . import db as store
from .contract_validate import problems

CONTRACT = json.loads((store.ROOT / 'contracts/vendor/qmd.feed.v1.json').read_text())
SQL = """
CREATE TABLE IF NOT EXISTS feed_positions (
 source TEXT PRIMARY KEY, token TEXT, since_arrival TEXT NOT NULL
) STRICT;
"""


def positions(db):
    return dict(db.execute('SELECT source,token FROM feed_positions'))


def bootstraps(db):
    return dict(db.execute('SELECT source,since_arrival FROM feed_positions'))


class Feed:
    def __init__(self, qmd, since, until):
        self.qmd, self.since = qmd, getattr(qmd, 'feed_since', since)
        self.ceiling = datetime.fromisoformat(store.iso(until)).timestamp()
        self.starts = getattr(qmd, 'feed_positions', {})
        self.bootstraps = getattr(qmd, 'feed_bootstraps', {})
        # qmd.feed.v1: (id, version) identifies one row; its token is its
        # position, and --after resumes strictly past it (tokens never reused,
        # no new row at/before a returned token). Atomic checkpoints therefore
        # exclude committed rows on restart. Failed/dry runs replay intentionally.
        # Only duplicates within this uncommitted enumeration need a seen set.
        self.seen = set()
        self.arrivals = {}
        self.counts = {}
        self.state = {'starts': {}, 'positions': {}, 'bootstraps': {}}

    def rows(self, source):
        token = self.starts.get(source)
        self.state['starts'][source] = token
        since = self.bootstraps.get(source, self.since)
        # An invisible first entry has no predecessor token. Keep the original
        # bootstrap bound even if this empty commit advances the currency cut.
        self.state['bootstraps'][source] = since
        out = self.arrivals[source] = []
        counts = self.counts[source] = {
            'feed_rows': 0, 'skipped_reparse': 0,
            'skipped_tombstone': 0, 'skipped_duplicate_versions': 0,
            'withdrawn_by_tombstone': 0,
        }
        while True:
            args = ['feed', '--source', source, '--limit', '100', '--json']
            args += ['--after', token] if token is not None else ['--since-arrival', since]
            page = self.qmd(*args)
            errors = problems(CONTRACT, page)
            if errors:
                raise ValueError('qmd.feed.v1 mismatch: ' + '; '.join(errors[:5]))
            if page['source'] != source or any(r['source'] != source for r in page['items']):
                raise ValueError('qmd.feed.v1 source mismatch')
            if page['complete'] != (page['next'] == page['head']):
                raise ValueError('qmd.feed.v1 complete disagrees with next/head')
            before = token
            for row in page['items']:
                # Enumeration has no QMD_UNTIL: never consume past an invisible
                # entry, including a newer version of an already-seen id.
                if any(row[k] is not None and row[k] > self.ceiling for k in ('time', 'arrived')):
                    self.state['positions'][source] = token
                    return out
                token = row['token']
                counts['feed_rows'] += 1
                if row['reparse']:
                    counts['skipped_reparse'] += 1
                    continue
                if row['tombstone']:
                    counts['skipped_tombstone'] += 1
                    # A document deleted after it arrived in this window is no longer an input.
                    kept = [r for r in out if r['id'] != row['id']]
                    counts['withdrawn_by_tombstone'] += len(out) - len(kept)
                    out[:] = kept
                    continue
                key = (source, row['id'], row['version'])
                if key in self.seen:
                    counts['skipped_duplicate_versions'] += 1
                    continue
                self.seen.add(key)
                if row['time'] is None:
                    raise ValueError('qmd.feed.v1 item time unknown')
                stamp = datetime.fromtimestamp(row['time'], timezone.utc)
                out.append({**row, 'time': stamp.isoformat().replace('+00:00', 'Z')})
            if page['items'] and page['next'] != token:
                raise ValueError('qmd.feed.v1 next does not match consumed prefix')
            if not page['items']:
                token = page['next']
            self.state['positions'][source] = token
            if page['complete']:
                return out
            if token == before or not page['items']:
                raise ValueError('qmd.feed.v1 incomplete page made no progress')


def committed_inputs(path, children):
    """Selection/inclusion evidence from committed runs, never abandoned drafts.

    The existing retained selected.json is the content actually put in the packet;
    a done acquisition alone is not proof that a particular child was included.
    """
    from pathlib import Path
    from .draft import run_directory

    result = {row['id']: {} for row in children}
    with store.connect(path) as db:
        runs = db.execute("SELECT change_set_id,note FROM change_sets WHERE origin='pipeline' ORDER BY rowid DESC").fetchall()
    for committed in runs:
        directory = run_directory(committed['note'])
        if not directory or not (Path(directory) / 'selected.json').is_file():
            continue  # No proof of inclusion; the child must not be excluded.
        docs = json.loads((Path(directory) / 'selected.json').read_text())
        if isinstance(docs, list):
            docs = {d['id']: d for d in docs}
        relevant = [row for row in children if row['parent'] in docs]
        if not relevant:
            continue
        manifest = json.loads((Path(directory) / 'manifest.json').read_text())
        entries = {e['ref']: e for e in manifest.get('mail', {}).get('kept_list', [])}
        for row in relevant:
            proof = result[row['id']]
            parent = row['parent']
            if 'doc' not in proof:
                entry = entries.get(parent, {})
                proof.update(doc=docs[parent], why=entry.get('why', 'keep:committed-parent'),
                             parent_commit=entry.get('selected_in') or committed['change_set_id'])
            child = docs.get(row['id'])
            if child:
                proof.setdefault('included', []).append((child, committed['change_set_id']))
    return result


def attach(db, state):
    if state is not None:
        store.put_meta(db, 'feed', json.dumps(state))


def commit(db, meta):
    if 'feed' not in meta:
        return
    state = json.loads(meta['feed'])
    current = positions(db)
    if any(current.get(src) != start for src, start in state['starts'].items()):
        raise ValueError('feed position moved after selection; retry')
    db.executemany(
        'INSERT OR REPLACE INTO feed_positions VALUES (?,?,?)',
        [(src, token, state['bootstraps'][src]) for src, token in state['positions'].items()],
    )
