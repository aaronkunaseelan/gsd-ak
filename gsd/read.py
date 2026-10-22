"""Agent-facing graph views: written fields are uncut unless --sparse is requested."""

import json
from . import db as store
from .write import rules, DEFAULTS

# Identity, state and all written narrative fields, not ingest bookkeeping.
VIEW_FIELDS = {
    'projects': 'status priority owner_person_id horizon summary desired_outcome current_state next_action risk_summary body_markdown',
    'items': 'kind state project_id assigned_to next_actor waiting_on due_at next_review_at closed_at urgency priority summary description desired_outcome next_actions deadline_reason body_markdown',
    'people': 'organization role_title relationship_type status primary_email aliases expected_response_window summary body_markdown',
    'meetings': 'status starts_at ends_at start_date end_date is_all_day duration_minutes location attendees_json summary action_points logistics_details previous_action_points body_markdown',
}

# Sparse is an index, not a second whole-record renderer: long-form bodies,
# descriptions and notes belong in show.
SPARSE_FIELDS = {
    'projects': 'status priority owner_person_id current_state next_action risk_summary',
    'items': 'kind state project_id assigned_to next_actor waiting_on due_at next_review_at urgency priority summary next_actions',
    'people': 'organization role_title relationship_type status primary_email aliases summary',
    'meetings': 'status starts_at start_date summary action_points',
}


def item_projects(state):
    # The project_id column is the only home of an item's project.
    return {i['id']: i.get('project_id') for i in state['items']}


def graph(db):
    return {t: store.rows(db, t) for t in store.TABLES}


def short_ids(state):
    ids = [r['id'] for r in state['records']]
    out = {}
    for rid in ids:
        n = 8
        while n < len(rid) and sum(x.startswith(rid[:n]) for x in ids) > 1:
            n += 1
        out[rid] = rid[:n]
    return out


def view(db, sparse=False, ids=None):
    state = graph(db)
    shorts = short_ids(state)
    names = {r['id']: store.name(r) for r in state['records']}

    def label(rid, link=False):
        title = names.get(rid, rid)
        cap = 48 if link else 180 if sparse else None
        if cap and len(title) > cap:
            title = title[:cap] + '…'
        return f'{title} [{shorts.get(rid, rid)}]'

    def text(v):
        v = ' '.join(str(v).split()) if sparse else str(v)
        return v[:180] + '…' if sparse and len(v) > 180 else v

    out = []
    written = set()

    def block(table, r):
        rid = r['id']
        if rid in written or (ids is not None and rid not in ids):
            return
        written.add(rid)
        out.append(f'## {table.rstrip("s")} {label(rid)}')
        for f in (SPARSE_FIELDS if sparse else VIEW_FIELDS)[table].split():
            v = r.get(f)
            if v in (None, '', '[]', '{}'):
                continue
            if f in ('owner_person_id', 'project_id'):
                v = label(v, link=True)
            out.append(f'{f}: {text(v)}')
        links = []
        for l in state['record_links']:
            if l['from_record_id'] == rid:
                links.append(f'{l["relation_kind"]} → {label(l["to_record_id"], link=True)}')
        if links:
            out.append('links: ' + '; '.join(links))
        out.append('')

    project_for = item_projects(state)
    for p in state['projects']:
        if (
            ids is None
            or p['id'] in ids
            or any(i['id'] in ids and project_for[i['id']] == p['id'] for i in state['items'])
        ):
            out.append('# Project ' + label(p['id']))
        block('projects', p)
        for i in state['items']:
            if project_for[i['id']] == p['id']:
                block('items', i)
    for table, title in (
        ('people', 'People'),
        ('meetings', 'Meetings'),
        ('items', 'Unattached items'),
    ):
        out.append('# ' + title)
        for r in state[table]:
            block(table, r)
    return '\n'.join(out).rstrip() + '\n'


def show(db, queries):
    state = graph(db)
    names = {r['id']: store.name(r) for r in state['records']}
    result = []
    for q in queries:
        exact = [r for r in state['records'] if r['id'] == q]
        matches = exact or [
            r
            for r in state['records']
            if r['id'].startswith(q) or q.casefold() in store.name(r).casefold()
        ]
        if len(matches) != 1:
            result.append(
                {
                    'query': q,
                    'matches': [
                        {'id': r['id'], 'name': store.name(r), 'kind': r['record_kind']}
                        for r in matches
                    ],
                }
            )
            continue
        record = matches[0]
        rid = record['id']
        table = store.KINDS[record['record_kind']]
        fields = next((r for r in state[table] if r['id'] == rid), {})
        links = []
        for l in state['record_links']:
            if rid in (l['from_record_id'], l['to_record_id']):
                links.append(
                    {k: v for k, v in l.items() if v is not None}
                    | {
                        'from_name': names.get(l['from_record_id']),
                        'to_name': names.get(l['to_record_id']),
                    }
                )
        refs = [
            {k: v for k, v in r.items() if v is not None}
            for r in state['source_refs']
            if r['record_id'] == rid
        ]
        result.append(
            {
                'record': {k: v for k, v in record.items() if v is not None},
                'fields': {k: v for k, v in fields.items() if v is not None},
                'links': links,
                'citations': refs,
            }
        )
    return result


def fields(db):
    result = {}
    for table in store.TABLES:
        enums, required = rules(db, table)
        result[table] = []
        for f, typ in store.columns(db, table).items():
            example = db.execute(
                f'SELECT "{f}" FROM cur_{table} WHERE "{f}" IS NOT NULL LIMIT 1'
            ).fetchone()
            result[table].append(
                {
                    'field': f,
                    'type': typ,
                    'required': f in required,
                    'managed': f in store.META | {'id', 'created_at', 'updated_at'},
                    'vocabulary': enums.get(f),
                    'default': DEFAULTS[table].get(f),
                    'example': example[0] if example else None,
                }
            )
    return result


def fields_text(data):
    lines = []
    for table, fs in data.items():
        lines.append('# ' + table)
        for f in fs:
            ex = json.dumps(f['example'], ensure_ascii=False)
            lines.append(
                f'{f["field"]} ({f["type"]}{", managed" if f["managed"] else ""}): example {ex[:80]}'
                + (
                    '; vocabulary ' + ', '.join(map(str, f['vocabulary']))
                    if f['vocabulary']
                    else ''
                )
            )
    return '\n'.join(lines)
