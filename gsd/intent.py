"""gsd's intent/1 document for mech-d Page 2: counts, projects and authored reasons."""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db as store
from .steady import qmd_status
from .items import history, is_open
from .worklist import priorities, past_due
from .runs import outcome

from . import owner


def when(value):
    if not value:
        return None
    return datetime.fromisoformat(store.iso(value))


def clip(value, n=160):
    text = ' '.join(str(value or '').split())
    return text if len(text) <= n else text[:n - 1] + '…'


def chain(now, cut):
    hops = []
    try:
        sources = qmd_status()['sources']
        for name, label in (
            ('whatsapp', 'qmd · WhatsApp'), ('gmail', 'qmd · Gmail'), ('fathom', 'qmd · Fathom')
        ):
            source = sources.get(name, {})
            through = source.get('fresh_through')
            lag = int(now.timestamp() - through) if through is not None else None
            current = source.get('current')
            stale = current is not True or through is None
            # Preserve the producer's nullable verdict, not a second currency rule.
            # Provider diagnostics are private; only gsd-authored notes leave here.
            note = ('unknown: capture currency' if current is None else
                    'no fresh_through' if through is None else 'capture behind') if stale else (
                'complete to ' + store.owner_time(datetime.fromtimestamp(through, UTC), day=False)
            )
            hops.append({'name': label, 'lag_s': lag, 'note': note, 'warn': stale})
    except (ValueError, OSError, OverflowError):
        hops = [{'name': 'qmd', 'lag_s': None, 'note': 'unknown: status unreadable or invalid', 'warn': True}]
    hops.append({
        'name': 'gsd cut',
        'lag_s': int((now - when(cut)).total_seconds()) if cut else None,
        'note': 'next cut at the next steady run',
        'warn': not cut,
    })
    return hops


def last_gsd_run(directory):
    paths = sorted(directory.glob('*.json'), reverse=True)
    if not paths:
        return None, False
    def unknown(reason):
        return {'kind': 'funnel', 'layer': 'process', 'title': 'Last gsd run',
                'sub': '', 'steps': [], 'note': 'unknown: newest steady record ' + reason}, True

    try:
        record = json.loads(paths[0].read_text())
    except OSError:
        return unknown('unreadable')
    except ValueError:
        return unknown('invalid JSON')
    if not isinstance(record, dict) or record.get('kind') != 'gsd-steady':
        return unknown('wrong kind')
    counts = record.get('counts', {})
    funnel_keys = ('proposed', 'accepted', 'amended', 'rejected', 'held', 'records_landed')
    if (not isinstance(counts, dict)
            or any(type(counts[k]) not in (int, float) or not math.isfinite(counts[k])
                   for k in funnel_keys if k in counts)
            or any(type(record.get(k, 0)) not in (int, float) or not math.isfinite(record.get(k, 0))
                   for k in ('minutes', 'weighted_tokens'))):
        return unknown('invalid measurements')
    try:
        window = ' → '.join(store.owner_time(record[k]) if record.get(k) else '' for k in ('since', 'until'))
    except (ValueError, TypeError, OverflowError):
        return unknown('invalid window timestamps')
    steps = [
        [label, int(counts[key]), '']
        for key, label in (
            ('proposed', 'proposed'), ('accepted', 'accepted'), ('amended', 'amended'),
            ('rejected', 'rejected'), ('held', 'held'), ('records_landed', 'landed'),
        ) if key in counts
    ]
    note = record.get('empty_reason') or (
        f"{record.get('minutes', 0):.0f} min · {record.get('weighted_tokens', 0) / 1e6:.2f}M weighted tokens"
    )
    verdict = outcome(record)
    failed = verdict['status'] == 'failed'
    if failed:
        note = verdict['reason'] + ' · ' + note
    return {
        'kind': 'funnel', 'layer': 'process', 'title': 'Last gsd run',
        'sub': window,
        'steps': steps, 'note': clip(note),
    }, failed


def document(path, now):
    path = Path(path).expanduser().resolve()
    with store.connect(path) as db:
        db.execute('PRAGMA busy_timeout=200')
        db.execute('BEGIN')
        all_projects = store.rows(db, 'projects')
        projects = [p for p in all_projects if p['status'] == 'active']
        pages = dict(db.execute('SELECT record_id, page_id FROM notion_map'))
        items = store.rows(db, 'items')
        transitions = [(when(row['at']), row) for row in history(db) if when(row['at']) <= now]
        cut = store.pipeline_cut(db)['until']
    week = now + timedelta(days=7)
    open_items = [item for item in items if is_open(item['state'])]

    mine_ids = {i['id'] for i in priorities(items, all_projects, now.isoformat())['mine']}

    def count(rows, actor):
        if actor == 'you':
            return sum(i['id'] in mine_ids for i in rows)
        if actor == 'agents':
            return sum(i['next_actor'] == 'agent' for i in rows)
        # intent/1 'others' is total external stock, not D29's due-soon worklist.
        if actor == 'external_total':
            return sum(i['next_actor'] == 'external' for i in rows)
        raise ValueError('unknown actor count')

    def late(item):
        return past_due(item, now.isoformat())

    def soon(item):
        return bool(item['due_at']) and now <= when(store.notion_date_utc(item['due_at'])) < week

    rows = []
    for project in projects:
        mine = [i for i in open_items if i['project_id'] == project['id']]
        if not mine:
            continue
        page = pages.get(project['id'])
        rows.append({
            'name': clip(project['name'], 80),
            'you': count(mine, 'you'), 'agents': count(mine, 'agents'), 'others': count(mine, 'external_total'),
            'past_due': sum(map(late, mine)), 'due_7d': sum(map(soon, mine)),
            'life': 'Owner commitments' in (project['metadata_json'] or ''),
            'link': f"https://www.notion.so/{page.replace('-', '')}" if page else None,
        })
    rows.sort(key=lambda r: (-r['you'], -r['past_due'], -(r['you'] + r['agents'] + r['others'])))

    since = owner.get('flow_since')
    days, day = [], datetime.fromisoformat(since).replace(tzinfo=owner.zone())
    while day.date() <= now.astimezone(owner.zone()).date() and len(days) < 31:
        end = day + timedelta(days=1)
        events = [row for at, row in transitions if day <= at < end]
        made = sum(row['is_open'] and not row['was_open'] for row in events)
        shut = sum(row['completion'] or (row['op'] == 'delete' and row['was_open']) for row in events)
        versions = {row['id']: row for at, row in transitions if at < end}
        stock = sum(row['is_open'] for row in versions.values())
        days.append({'day': day.strftime('%Y-%m-%d'), 'open': stock, 'in': made, 'out': shut})
        day = end

    hops = chain(now, cut)
    blocks = [
        {'kind': 'stock', 'layer': 'intent', 'open': len(open_items),
         'you': count(open_items, 'you'), 'agents': count(open_items, 'agents'),
         'others': count(open_items, 'external_total'), 'past_due': sum(map(late, open_items)),
         'due_7d': sum(map(soon, open_items)), 'fresh_through': cut},
        {'kind': 'flow', 'layer': 'process', 'since': since, 'days': days},
        {'kind': 'rows', 'layer': 'intent', 'items': rows[:40]},
        {'kind': 'chain', 'layer': 'process', 'hops': hops},
    ]
    last, failed = last_gsd_run(path.parent / 'steady-runs')
    if last:
        blocks.append(last)
    return {
        'schema': 'intent/1', 'system': 'gsd', 'at': int(now.timestamp()),
        # A failed last run stays red until a run succeeds.
        'state': 'red' if failed or any(h['warn'] for h in hops) else 'green',
        'headline': clip(f"{count(open_items, 'you')} wait on you; {sum(map(late, open_items))} past due"),
        'blocks': blocks,
    }


def command(a):
    return document(a.db, datetime.now(UTC))
