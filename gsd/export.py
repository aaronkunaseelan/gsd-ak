"""Evaluation exports and prior-step digest. No source reads or store mutations."""

import json
from . import db as store, draft

TABLES = ('projects', 'items', 'people', 'meetings', 'record_links', 'source_refs')


def state(db):
    # Current-state exports omit managed metadata and empty values.
    return {
        t: [
            {k: v for k, v in r.items() if k not in store.META and v not in (None, '', '[]', '{}')}
            for r in store.rows(db, t)
        ]
        for t in TABLES
    }


def command(a):
    with store.connect(a.db) as db:
        return state(db)


def changes(db):
    meta = store.metadata(db)
    cutoff = json.loads(meta['snapshot_max'])
    result = draft.status(db)['changes']
    for c in result:
        c['note'] = meta.get('decision_note:' + c['change_id'])
        c['notes'] = {k: c.get(k) for k in ('why', 'confidence', 'importance')}
        c['notes']['review'] = c['note']
        c['deltas'], c['citations'] = [], []
        for table in store.TABLES:
            for row in db.execute(
                f'SELECT * FROM {table} WHERE change_set_id=? AND seq>? ORDER BY seq',
                (c['change_id'], cutoff[table]),
            ):
                after = dict(row)
                old = store.previous(db, table, after)
                before = dict(old) if old and old['op'] != 'delete' else None
                delta = {
                    k: v
                    for k, v in after.items()
                    if k not in store.META and (before is None or before.get(k) != v)
                }
                c['deltas'].append(
                    {'table': table, 'before': before, 'after': after, 'fields': delta}
                )
                if table == 'source_refs':
                    c['citations'].append({k: v for k, v in after.items() if k not in store.META})
    return result


def carry(previous):
    """Only explicitly linked previous run: never scan archived experiments."""
    rows = [
        json.loads(l) for l in (previous / 'changes.jsonl').read_text().splitlines() if l.strip()
    ]
    log = (previous / 'open.md').read_text()
    out = [
        '# Previous step: belief to re-check, not evidence',
        'Resolve only on a direct statement; otherwise carry the question.',
        '## A open log',
        log,
        '## Held changes',
    ]
    for c in rows:
        if c.get('decision') == 'hold':
            out.append(
                json.dumps(
                    {
                        k: c.get(k)
                        for k in ('change_id', 'why', 'decision', 'records')
                    },
                    ensure_ascii=False,
                )
            )
    out.append('## Records written (all proposal records, including held/rejected; check decision)')
    for c in rows:
        out.append(
            f"{c['change_id']} {c.get('decision')}: "
            + json.dumps(c.get('records', []), ensure_ascii=False)
        )
    for f in sorted(previous.glob('notes-*.md')):
        out.extend(['## ' + f.stem, f.read_text()])
    return '\n\n'.join(out)
