"""The consistent work snapshot for Steward and mech-d (gsd.worklist.v1)."""

from datetime import datetime, timedelta

from . import db as store
from .items import history, is_open


def past_due(item, at):
    """Past due means its due boundary has passed, not an age bucket."""
    return bool(item['due_at']) and store.notion_date_utc(item['due_at']) < store.iso(at)


def waiting_on_others(item, at):
    """D29: open work with people, due within two days; excludes overdue work."""
    start = datetime.fromisoformat(store.iso(at))
    return (is_open(item['state']) and item['next_actor'] == 'external'
            and bool(item['due_at'])
            and start <= datetime.fromisoformat(store.notion_date_utc(item['due_at'])) <= start + timedelta(days=2))


def priorities(items, projects, at):
    """D29: one owner list, ordered by priority, due date, then graph sequence.

    External work is waiting only when due within the next two days, inclusive.
    Input rows are in graph sequence order, so stable sorting preserves ties.
    """
    names = {p['id']: p['name'] for p in projects}
    at = store.iso(at)
    ordered = sorted((i for i in items if is_open(i['state'])), key=lambda i: (
        -float(i['priority']) if i['priority'] is not None else float('inf'),
        store.notion_date_utc(i['due_at']) if i['due_at'] else '9999',
    ))

    def entry(item):
        return {
            'id': item['id'], 'title': item['title'], 'project': names.get(item['project_id']),
            'due_at': store.notion_date_utc(item['due_at']) if item['due_at'] else None,
            'priority': str(item['priority']) if item['priority'] is not None else None,
        }

    return {
        'mine': [entry(i) for i in ordered if i['next_actor'] == 'user'],
        'waiting_on_others': [entry(i) for i in ordered if waiting_on_others(i, at)],
    }


def command(a):
    with store.connect(a.db) as db:
        db.execute('BEGIN')
        at = store.now()
        cut = store.pipeline_cut(db)
        pages = (
            dict(db.execute('SELECT record_id, page_id FROM notion_map'))
            if store.has_table(db, 'notion_map')
            else {}
        )
        people = {p['id']: p['display_name'] for p in store.rows(db, 'people')}

        def url(rid):
            return store.notion_url(pages.get(rid))

        all_projects = store.rows(db, 'projects')
        projects = [
            {
                'id': p['id'],
                'name': p['name'],
                'status': p['status'],
                'notion_url': url(p['id']),
            }
            for p in all_projects
            if p['status'] == 'active'
        ]
        items = []
        fields = (
            'id title kind project_id state due_at next_actor waiting_on assigned_to '
            'priority urgency next_review_at updated_at'
        ).split()
        for item in store.rows(db, 'items'):
            if not is_open(item['state']):
                continue
            row = {key: item.get(key) for key in fields}
            for key in ('project_id', 'assigned_to', 'waiting_on', 'urgency', 'next_actor'):
                row[key] = row[key] or None
            if row['assigned_to'] and row['assigned_to'] not in people:
                try:
                    row['assigned_to'] = store.resolve(db, row['assigned_to'], 'person')
                except ValueError:
                    row['assigned_to'] = None
            row['assigned_name'] = people.get(row['assigned_to'])
            row['notion_url'] = url(row['id'])
            if row['priority'] is not None:
                row['priority'] = str(row['priority'])
            for key in ('due_at', 'next_review_at', 'updated_at'):
                row[key] = store.notion_date_utc(row[key]) if row[key] else None
            items.append(row)
        since = store.iso((datetime.fromisoformat(at.replace('Z', '+00:00'))
                           - timedelta(hours=48)).isoformat())
        return {
            'schema': 'gsd.worklist.v1',
            'at': at,
            'cut': cut,
            'projects': projects,
            'items': items,
            'done': closures(db, since, at),
            'priorities': priorities(items, all_projects, at),
        }


def closures(db, since, at):
    """Closure events at the closure's own time, since..at inclusive.

    Compare adjacent full versions before applying the time window: an edit
    to an already closed item is not another completion.
    """
    return [
        {**{k: row[k] for k in ('id', 'title', 'project', 'state')}, 'at': store.iso(row['at'])}
        for row in history(db)
        if row['completion'] and since <= store.iso(row['at']) <= at
    ]


def closures_command(a):
    """gsd.closures.v1: the same rows as done, over the caller's window (Steward's 'used' reads this)."""
    since = store.iso(a.since)
    with store.connect(a.db) as db:
        db.execute('BEGIN')
        at = store.now()
        return {
            'schema': 'gsd.closures.v1',
            'at': at,
            'since': since,
            'cut': store.pipeline_cut(db),
            'closures': closures(db, since, at),
        }
