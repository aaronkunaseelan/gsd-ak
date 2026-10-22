"""Snapshot, review relay and delta-based commit. Draft snapshots are never copied as truth."""

import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import nullcontext, contextmanager
from pathlib import Path
from . import db as store
from . import owner
from .write import Invalid, err, fail, operation, qmd_ref, validate


def snapshot(source, out, step_id, since, until):
    since, until = store.iso(since), store.iso(until)
    if since >= until:
        raise ValueError('snapshot --since must precede --until')
    if not step_id.strip():
        raise ValueError('step id must not be empty')
    out = Path(out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out.exists():
        raise ValueError('snapshot output already exists')
    with store.connect(source) as db:
        db.execute('VACUUM INTO ?', (str(out),))
    os.chmod(out, 0o600)
    with store.connect(out, True) as db:
        maximum = store.maxima(db)
        store.migrate(db, draft=True)
        for k, v in {
            'step_id': step_id,
            'since': since,
            'until': until,
            'snapshot_max': json.dumps(maximum),
        }.items():
            store.put_meta(db, k, v)
    return {'draft': str(out), 'step_id': step_id, 'snapshot_max': maximum}


def require_draft(db):
    if not store.has_table(db, 'draft_change'):
        raise ValueError('command requires a draft store')


def decide(db, decisions, default_by='owner'):
    require_draft(db)
    result = []
    for d in decisions:
        cid = d.get('id', d.get('change')) if isinstance(d, dict) else None
        try:
            db.execute('BEGIN IMMEDIATE')
            if not isinstance(d, dict) or set(d) - {
                'id',
                'change',
                'decision',
                'quote',
                'set',
                'note',
                'by',
            }:
                fail('decision', d, 'unknown decision fields or not an object')
            change = db.execute('SELECT * FROM draft_change WHERE change_id=?', (cid,)).fetchone()
            if not change:
                fail('id', cid, 'unknown change')
            decision = d.get('decision')
            if decision not in ('accept', 'amend', 'hold', 'reject'):
                fail('decision', decision, 'expected accept, amend, hold or reject')
            by = d.get('by') or default_by
            if not isinstance(by, str) or not by.strip():
                fail('by', by, 'expected reviewer name')
            quote = review_quote(db, d, cid, decision)
            if 'set' in d and decision != 'amend':
                fail('set', d['set'], 'only amend accepts corrected fields')
            if decision == 'amend':
                amend_fields(db, d, cid, by, quote)
            note = d.get('note')
            if note is not None:
                if not isinstance(note, str):
                    fail('note', note, 'expected text')
                store.put_meta(db, 'decision_note:' + cid, note)
            db.execute(
                'UPDATE draft_change SET decision=?,quote=?,decided_by=?,decided_at=? WHERE change_id=?',
                (
                    decision,
                    json.dumps(quote, ensure_ascii=False) if quote else None,
                    by,
                    store.now(),
                    cid,
                ),
            )
            db.commit()
            result.append({'id': cid, 'ok': True, 'decision': decision})
        except Invalid as e:
            db.rollback()
            result.append({'id': cid, 'ok': False, 'errors': e.errors})
        except sqlite3.IntegrityError as e:
            db.rollback()
            result.append({'id': cid, 'ok': False, 'errors': [err('decision', None, str(e))]})
    return result


def review_quote(db, d, cid, decision):
    quote = d.get('quote')
    is_a = cid.lower().startswith('a')
    if not is_a:
        # The step pins proposer actor=a. Provenance remains A's even
        # when a model accidentally names its second batch b1, b2, ….
        cutoff = json.loads(store.metadata(db)['snapshot_max'])
        is_a = any(
            db.execute(
                f"SELECT 1 FROM {t} WHERE change_set_id=? AND seq>? AND (lower(actor)='a' OR lower(actor) LIKE 'a-%') LIMIT 1",
                (cid, cutoff[t]),
            ).fetchone()
            for t in store.TABLES
        )
    if decision in ('amend', 'reject') and is_a and not quote:
        fail('quote', quote, 'amend/reject of an A change requires a source quote')
    if quote:
        if isinstance(quote, list) and len(quote) == 2:
            quote = dict(zip(('ref', 'text'), quote))
        if (
            not isinstance(quote, dict)
            or set(quote) != {'ref', 'text'}
            or not isinstance(quote['text'], str)
            or not quote['text'].strip()
        ):
            fail('quote', quote, 'expected {ref, text}, with nonempty text')
        quote = {'ref': qmd_ref(db, quote['ref']), 'text': quote['text']}
    return quote


def amend_fields(db, d, cid, by, quote=None):
    candidates = []
    cutoff = json.loads(store.metadata(db)['snapshot_max'])
    for kind, table in store.KINDS.items():
        candidates.extend(
            (kind, r[0])
            for r in db.execute(
                f'SELECT DISTINCT id FROM {table} WHERE change_set_id=? AND seq>?',
                (cid, cutoff[table]),
            )
        )
    fields = d.get('set')
    if not isinstance(fields, dict) or not fields:
        fail('set', fields, 'amend requires a nonempty field object or {record_id: {field: value}}')
    kinds = {rid: kind for kind, rid in candidates}
    if any(isinstance(v, dict) for v in fields.values()):
        updates = fields
    elif len(candidates) == 1:
        updates = {candidates[0][1]: fields}
    else:
        fail('set', fields, 'multi-entity amend requires {record_id: {field: value}}')
    for rid, values in updates.items():
        if rid not in kinds:
            fail('set', rid, 'record must be an entity written by this change')
        if not isinstance(values, dict) or not values:
            fail('set', values, 'each record needs a nonempty field object')
        op = {'upsert': kinds[rid], 'id': rid, 'set': values}
        if quote and {'state', 'assigned_to'} & set(values):
            # The reviewer's quote is the decides citation that closure and assignment need.
            op['cite'] = [{'ref': quote['ref'], 'role': 'decides', 'excerpt': quote['text']}]
        operation(db, op, cid, by, 'decide-amend')


def touched(table, row):
    if table == 'record_links':
        return [row['from_record_id'], row['to_record_id']]
    if table == 'source_refs':
        return [row['record_id']] if row.get('record_id') else []
    return [row['id']]


def status(db, group=None):
    require_draft(db)
    changes = [
        {k: r[k] for k in r.keys() if k not in ('needs_owner', 'question')}
        for r in db.execute('SELECT * FROM draft_change ORDER BY rowid')
        if group is None or r['grp'] == group
    ]
    cutoff = json.loads(store.metadata(db)['snapshot_max'])
    names = {r['id']: store.name(r) for r in store.rows(db, 'records')}
    for c in changes:
        ids = []
        for t in store.TABLES:
            for r in db.execute(
                f'SELECT * FROM {t} WHERE change_set_id=? AND seq>? ORDER BY seq',
                (c['change_id'], cutoff[t]),
            ):
                ids.extend(touched(t, dict(r)))
        c['records'] = [{'id': rid, 'name': names.get(rid, rid)} for rid in dict.fromkeys(ids)]
    counts = {
        k: sum((c['decision'] or 'undecided') == k for c in changes)
        for k in ('undecided', 'accept', 'amend', 'hold', 'reject')
    }
    return {
        'counts': counts,
        'undecided': [c['change_id'] for c in changes if c['decision'] is None],
        'changes': changes,
    }


def status_text(data, brief=False):
    lines = ['counts: ' + json.dumps(data['counts']), 'undecided: ' + ', '.join(data['undecided'])]
    limit = max(45, 6800 // max(1, len(data['changes']))) if brief else None
    for c in data['changes']:
        records = ', '.join(f'[{r["id"][:8]}] {r["name"]}' for r in c['records'])
        line = f'{c["change_id"]} {c["decision"] or "undecided"} | {records[:36] if brief else records} | {c["why"] or ""}'
        lines.append(line[:limit] if limit else line)
    return '\n'.join(lines)


@contextmanager
def live_lock(target, *, blocking=True):
    lock = Path(target).expanduser().resolve().parent / 'gsd.lock'
    inherited = os.environ.get('GSD_LOCK_FD')
    if inherited is not None:
        try:
            fd = int(inherited)
            held, wanted = os.fstat(fd), lock.stat()
            if (held.st_dev, held.st_ino) != (wanted.st_dev, wanted.st_ino):
                raise ValueError('wrong lock file')
            fd = os.dup(fd)
        except (ValueError, OSError):
            raise ValueError('invalid inherited gsd lock') from None
    else:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError('gsd store lock is held by another run') from None
        yield f.fileno()


def archive_file(target, step_id):
    if not step_id or Path(step_id).name != step_id or step_id in ('.', '..'):
        raise ValueError('archive requires a step id that is a single filename component')
    return Path(target).expanduser().resolve().parent / 'archive' / (step_id + '.sqlite')


def archive(draft, target):
    """Freeze once, locally. Existing bytes are retained for immutable upload retries."""
    with store.connect(draft) as db:
        require_draft(db)
        staged = archive_file(target, store.metadata(db)['step_id'])
        staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(staged.parent, 0o700)
        if not staged.exists():
            with tempfile.TemporaryDirectory(prefix='freeze-', dir=staged.parent) as temporary:
                frozen = Path(temporary) / staged.name
                db.execute('VACUUM INTO ?', (str(frozen),))
                os.chmod(frozen, 0o600)
                os.replace(frozen, staged)
    os.chmod(staged, 0o600)
    return str(staged)


def upload_archives(target):
    root = Path(target).expanduser().resolve().parent / 'archive'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    result = subprocess.run(
        [
            'rclone', 'copy', '--immutable', '--checksum', str(root),
            owner.get('backup_remote').rstrip('/') + '/steps/',
        ],
        capture_output=True,
    )
    if result.returncode:
        raise ValueError(f'rclone archive failed (exit {result.returncode}); local archives retained')


def archive_check(target):
    """Read only: report committed pipeline drafts missing their local archive."""
    with store.connect(target) as db:
        return [
            {'step_id': row['step_id'], 'run': run_directory(row['note'])}
            for row in db.execute(
                "SELECT step_id,note FROM change_sets WHERE origin='pipeline' AND step_id IS NOT NULL ORDER BY at,step_id"
            )
            if not archive_file(target, row['step_id']).is_file()
        ]


def run_directory(note):
    for prefix in ('gsd-step-run:', 'gsd-empty-run:'):
        if note and note.startswith(prefix):
            return note[len(prefix):]
    return None


def recover_archive(target, step_id):
    """Recover evidence only; never reapply a window or upload anything."""
    with live_lock(target):
        with store.connect(target) as db:
            row = db.execute(
                "SELECT note FROM change_sets WHERE origin='pipeline' AND step_id=?", (step_id,)
            ).fetchone()
        run = run_directory(row['note']) if row else None
        if not run:
            raise ValueError('committed pipeline step has no local run directory')
        source = Path(run) / 'step.sqlite'
        with store.connect(source) as db:
            require_draft(db)
            if store.metadata(db)['step_id'] != step_id:
                raise ValueError('run draft step id does not match committed step')
        return {'step_id': step_id, 'archive': archive(source, target)}


def commit(draft_path, target, window_until=None, note=None, actor='owner'):
    if window_until is not None:
        window_until = store.iso(window_until)
    is_live = Path(target).expanduser().resolve() == store.LIVE.resolve()
    with store.connect(draft_path) as draft:
        require_draft(draft)
        meta = store.metadata(draft)
        changes = {
            r['change_id']: dict(r)
            for r in draft.execute('SELECT * FROM draft_change ORDER BY rowid')
        }
        undecided = [cid for cid, c in changes.items() if not c['decision']]
        if undecided:
            fail('decision', undecided, 'all changes must be decided before commit')
        cutoff = json.loads(meta['snapshot_max'])
        accepted = {cid for cid, c in changes.items() if c['decision'] in ('accept', 'amend')}
        plans = commit_plans(draft, cutoff, accepted)
        cid = 'pipeline:' + meta['step_id']
        with live_lock(target) if is_live else nullcontext():
            with store.connect(target, True) as live:
                if not store.has_table(live, 'change_sets'):
                    raise ValueError('target requires gsd migrate')
                live.execute('BEGIN IMMEDIATE')
                if live.execute(
                    'SELECT 1 FROM change_sets WHERE change_set_id=?', (cid,)
                ).fetchone():
                    fail('step_id', meta['step_id'], 'step already committed to this target')
                refused = prepare_plans(draft, live, plans, cutoff)
                preflight(live, plans, refused, cid, actor)
                if refused and 'feed' in meta:
                    raise Invalid([err('commit', rid, 'source window rolled back: ' + why)
                                   for rid, why in sorted(refused.items())])
                landed_records, landed_changes = set(), set()
                store.change_set(
                    live,
                    cid,
                    actor,
                    'pipeline',
                    step_id=meta['step_id'],
                    window_until=window_until or meta['until'],
                    note=note,
                )
                for (table, _), p in plans.items():
                    if p['records'] & refused.keys():
                        continue
                    if p['op'] == 'upsert':
                        validate(live, table, p['data'])
                    store.append(live, table, p['data'], cid, 'commit', actor, op=p['op'])
                    landed_records.update(p['records'])
                    landed_changes.update(p['changes'])
                from . import feed

                feed.commit(live, meta)
                if is_live:
                    archive(draft_path, target)
                live.commit()
            if is_live:
                upload_archives(target)
        return {
            'change_set_id': cid,
            'landed_records': sorted(landed_records),
            'landed_changes': sorted(landed_changes),
            'refused': [{'id': rid, 'why': why} for rid, why in sorted(refused.items())],
            'archive': 'B2' if is_live else 'skipped (target is not the live path)',
        }


def commit_plans(draft, cutoff, accepted):
    plans = {}
    # Delta against the immediately preceding draft version, even when it belongs
    # to a rejected change. This prevents rejected fields leaking via full snapshots.
    for table in store.TABLES:
        for row in draft.execute(
            f'SELECT * FROM {table} WHERE seq>? ORDER BY seq', (cutoff[table],)
        ):
            r = dict(row)
            if r['change_set_id'] not in accepted:
                continue
            identity = store.key(table, r)
            before = store.previous(draft, table, r)
            previous = dict(before) if before and before['op'] != 'delete' else {}
            fields = {
                f: v
                for f, v in r.items()
                if f not in store.META and (not previous or previous.get(f) != v)
            }
            p = plans.setdefault(
                (table, identity), {'events': [], 'records': set(), 'changes': set()}
            )
            p['events'].append((r['op'], fields))
            p['records'].update(touched(table, r))
            p['changes'].add(r['change_set_id'])
    return plans


def prepare_plans(draft, live, plans, cutoff):
    refused = {}
    all_records = set().union(*(p['records'] for p in plans.values())) if plans else set()
    for rid in sorted(all_records):
        for table in ('records', *store.KINDS.values()):
            r = store.current(live, table, (rid,), True)
            if r and r['seq'] > cutoff[table]:
                refused[rid] = f'{table} current seq {r["seq"]} > snapshot max {cutoff[table]}'
    next_ref = live.execute('SELECT COALESCE(MAX(source_ref_id),0) FROM source_refs').fetchone()[0]
    for (table, identity), p in plans.items():
        current = store.current(live, table, identity, True)
        # Draft-added citation integers are local; allocate fresh target ids.
        new_citation = (
            table == 'source_refs'
            and not draft.execute(
                'SELECT 1 FROM source_refs WHERE source_ref_id=? AND seq<=?',
                (identity[0], cutoff[table]),
            ).fetchone()
        )
        if current and current['seq'] > cutoff[table] and not new_citation:
            for rid in p['records']:
                refused[rid] = f'{table} identity moved after snapshot'
        data = (
            {f: v for f, v in (current or {}).items() if f not in store.META}
            if current and current['op'] != 'delete' and not new_citation
            else {}
        )
        op = 'upsert'
        for op, delta in p['events']:
            if op == 'delete':
                data = dict(zip(store.IDENTITY[table], identity))
            else:
                data.update(delta)
        for f, v in zip(store.IDENTITY[table], identity):
            data[f] = v
        if new_citation:
            next_ref += 1
            data['source_ref_id'] = next_ref
        p.update(data=data, op=op)
    return refused


def preflight(live, plans, refused, cid, actor):
    # Validate against the prospective record registry in a rolled-back
    # savepoint. Repeat when a refused creation invalidates a dependency.
    # A skipped link never cascades a refusal to its innocent endpoint.
    changed = True
    while changed:
        changed = False
        live.execute('SAVEPOINT preflight')
        try:
            for (table, _), p in plans.items():
                if table != 'records' or p['records'] & refused.keys():
                    continue
                try:
                    if p['op'] == 'upsert':
                        validate(live, table, p['data'])
                    store.append(live, table, p['data'], cid, 'commit', actor, op=p['op'])
                except (Invalid, sqlite3.IntegrityError) as e:
                    reason = str(e.errors) if isinstance(e, Invalid) else str(e)
                    for rid in p['records']:
                        refused[rid] = reason
                    changed = True
            for (table, _), p in plans.items():
                if table == 'records' or p['op'] != 'upsert' or p['records'] & refused.keys():
                    continue
                try:
                    validate(live, table, p['data'])
                    if table in store.KINDS.values() and not store.current(
                        live, 'records', (p['data']['id'],)
                    ):
                        fail(
                            'id',
                            p['data']['id'],
                            'accepted delta depends on a rejected creation/deleted record',
                        )
                    for f in ('project_id', 'owner_person_id'):
                        if p['data'].get(f) in refused:
                            fail(f, p['data'][f], 'depends on a refused record')
                except Invalid as e:
                    for rid in p['records']:
                        refused[rid] = str(e.errors)
                    changed = True
        finally:
            live.execute('ROLLBACK TO preflight')
            live.execute('RELEASE preflight')
