"""What an item needs next, as gsd holds it (gsd.brief.v1): the view Steward's prepare job reads."""

from . import db as store


def command(a):
    with store.connect(a.db) as db:
        db.execute('BEGIN')
        at = store.now()
        cut = store.pipeline_cut(db)
        graph = {t: store.rows(db, t) for t in ('items', 'projects', 'people', 'record_links', 'source_refs')}
        pages = (
            dict(db.execute('SELECT record_id, page_id FROM notion_map'))
            if store.has_table(db, 'notion_map')
            else {}
        )
        return {
            'schema': 'gsd.brief.v1',
            'at': at,
            'cut': cut,
            'items': [entry(q, graph, pages) for q in a.ids],
        }


def entry(query, graph, pages):
    items = graph['items']
    matches = [i for i in items if i['id'] == query] or [i for i in items if i['id'].startswith(query)]
    if len(matches) != 1:
        return {'id': query, 'error': ('ambiguous' if matches else 'unknown') + ' item'}
    item = matches[0]
    rid = item['id']
    projects = {p['id']: p['name'] for p in graph['projects']}
    people = {p['id']: p['display_name'] for p in graph['people']}
    waiting = item['waiting_on'] or None
    return {
        'id': rid,
        'title': item['title'],
        'kind': item['kind'],
        'project': projects.get(item['project_id']),
        'state': item['state'],
        'next_actor': item['next_actor'],
        'due_at': store.notion_date_utc(item['due_at']) if item['due_at'] else None,
        'priority': str(item['priority']) if item['priority'] is not None else None,
        'created_at': store.iso(item['created_at']) if item['created_at'] else None,
        'updated_at': store.iso(item['updated_at']) if item['updated_at'] else None,
        'summary': item['summary'] or None,
        'next_actions': item['next_actions'] or None,
        'waiting_on': people.get(waiting, waiting),
        'notion_url': store.notion_url(pages.get(rid)),
        'people': [
            {'id': l['to_record_id'], 'name': people[l['to_record_id']], 'relation': l['relation_kind']}
            for l in graph['record_links']
            if l['from_record_id'] == rid and l['to_record_id'] in people
        ],
        'citations': [
            {'ref': r['evidence_qmd_event_id'], 'role': r['role'], 'excerpt': r['excerpt'], 'at': store.iso(r['at'])}
            for r in graph['source_refs']
            if r['record_id'] == rid
        ],
    }
