"""One-way Notion projection, with an export-first D16 hard cut.
Ordinary writes are section-local; hardcut replaces gsd-owned schemas and bodies.
Uncertain writes are read back, never blindly retried.
"""

import datetime
import email.utils
import fcntl
import hashlib
import http.client
import json
import os
import re
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from collections import Counter
from . import db as store
from . import owner, read, write
from .items import is_open

VERSION = '2026-03-11'
PROJECTION_CHECK_MAX_AGE = datetime.timedelta(hours=26)
PORT = Path.home() / '.local/share/gsd/port'
# One row per public gsd field:
# (field, property or body heading, allowed REST types; empty = body).
# First type is the expected offline schema; live schema selects the encoder.
# Ingest keys/timestamps and execution plumbing deliberately aren't projected.
FIELDS = {
    'projects': [
        ('name', 'Name', 'title'),
        ('status', 'Status', 'select status'),
        ('owner_person_id', 'Owner', 'relation'),
        ('priority', 'Priority', 'number'),
        ('horizon', 'Horizon', 'select'),
        ('next_action', 'Next Actions', 'rich_text'),
        ('summary', 'Summary', ''),
        ('desired_outcome', 'State of Play', ''),
        ('current_state', 'State of Play', ''),
        ('risk_summary', 'State of Play', ''),
        ('body_markdown', 'Project Notes', ''),
        ('metadata_json', 'Metadata', ''),
    ],
    'items': [
        ('title', 'Name', 'title'),
        ('kind', 'Kind', 'select'),
        ('state', 'State', 'select status'),
        # Derived from state, not stored: see record_status().
        ('record_status', 'Record Status', 'select status'),
        ('project_id', 'Project', 'relation'),
        ('assigned_to', 'Assigned To', 'rich_text'),
        ('next_actor', 'Next Actor', 'select'),
        ('priority', 'Priority', 'number'),
        ('urgency', 'Urgency', 'select'),
        ('due_at', 'Due', 'date'),
        ('next_review_at', 'Next Review', 'date'),
        ('closed_at', 'Closed At', 'date'),
        ('next_actions', 'Next Actions', 'rich_text'),
        ('summary', 'Summary', ''),
        ('description', 'Details', ''),
        ('desired_outcome', 'Details', ''),
        ('waiting_on', 'Details', ''),
        ('deadline_reason', 'Details', ''),
        ('body_markdown', 'Item Notes', ''),
    ],
    'people': [
        ('display_name', 'Name', 'title'),
        ('primary_email', 'Email', 'email'),
        # Only aliases that are email addresses reach Notion; see render_fields.
        ('aliases', 'Other Emails', 'rich_text multi_select'),
        ('organization', 'Organization', 'rich_text'),
        ('role_title', 'Role', 'rich_text'),
        ('relationship_type', 'Relationship', 'rich_text select multi_select'),
        ('expected_response_window', 'Response Window', 'rich_text'),
        ('status', 'Status', 'select status'),
        ('summary', 'Summary', ''),
        ('body_markdown', 'Person Notes', ''),
        ('metadata_json', 'Metadata', ''),
    ],
    'meetings': [
        ('title', 'Name', 'title'),
        ('status', 'Status', 'select status'),
        ('start_date', 'When', 'date'),
        ('duration_minutes', 'Number of Minutes', 'number'),
        ('logistics_details', 'Logistics', 'rich_text'),
        ('starts_at', 'Logistics', 'rich_text'),
        ('ends_at', 'Logistics', 'rich_text'),
        ('end_date', 'Logistics', 'rich_text'),
        ('location', 'Logistics', 'rich_text'),
        ('is_all_day', 'Logistics', 'rich_text'),
        ('summary', 'Summary', ''),
        ('attendees_json', 'Attendees', ''),
        ('body_markdown', 'Meeting Notes', ''),
        ('action_points', 'Meeting Notes', ''),
        ('previous_action_points', 'Meeting Notes', ''),
        ('metadata_json', 'Metadata', ''),
    ],
}
SILENT = {'mode': 'silent'}
EMPTY = '_(empty)_'
EMAIL = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')


def record_status(state):
    return 'open' if is_open(state) else {'done': 'done', 'canceled': 'cancelled'}[state]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    ).hexdigest()


def rich(text):
    text = str(text if text is not None else '')
    return [
        {'type': 'text', 'text': {'content': text[i : i + 2000]}} for i in range(0, len(text), 2000)
    ]


def sequence(value):
    if value in (None, ''):
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = value
        value = decoded
    return value if isinstance(value, list) else [value]


def canonical_date(value):
    """Notion date properties retain calendar dates and UTC minute instants."""
    if value in (None, ''):
        return None
    value = str(value)
    if len(value) == 10:
        return datetime.date.fromisoformat(value).isoformat()
    instant = datetime.datetime.fromisoformat(store.iso(value).replace('Z', '+00:00'))
    return instant.isoformat(timespec='minutes').replace('+00:00', 'Z')


def encode(typ, value):
    """Only writable property types; people/rollup/formula are never synthesized."""
    if typ in ('title', 'rich_text'):
        return {typ: rich(value)}
    if typ in ('select', 'status'):
        return {typ: {'name': str(value)} if value not in (None, '') else None}
    if typ == 'multi_select':
        return {typ: [{'name': str(v)} for v in sequence(value)]}
    if typ == 'date':
        return {typ: {'start': canonical_date(value)} if value not in (None, '') else None}
    if typ in ('number', 'url', 'email'):
        return {typ: value if value != '' else None}
    if typ == 'checkbox':
        if value not in (None, False, True, 0, 1):
            raise ValueError('invalid checkbox value')
        return {typ: bool(value)}
    if typ == 'relation':
        return {typ: [{'id': v} for v in sequence(value)]}
    raise ValueError('unsupported Notion property type: ' + typ)


def expected_schema(table, sources=None):
    schema = {}
    for field, name, types in FIELDS[table]:
        if not types:
            continue
        typ = types.split()[0]
        schema[name] = {'type': typ, typ: {}}
        if typ == 'relation' and sources is not None:
            target = store.KINDS[write.REFERENCES[field]]
            schema[name][typ] = {
                'data_source_id': sources.get(target, ''),
                'type': 'single_property', 'single_property': {},
            }
    return schema


def markdown(sections):
    return '\n\n---\n\n'.join(f'## {k}\n\n{demote(str(v)).rstrip()}' for k, v in sections.items())


def markdown_lines(text):
    """One fenced-code grammar for rendering, section boundaries and attribution."""
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            mark = stripped[:3]
            fence = None if fence == mark else mark if fence is None else fence
        yield offset, line, fence is None
        offset += len(line)


def demote(text):
    return ''.join(re.sub(r'^#{1,2} ', '### ', line) if outside else line
                   for _, line, outside in markdown_lines(text))


def actions(value):
    if not value:
        return value
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    return (
        '\n'.join('— ' + re.sub(r'^(?:[-*—]|\d+[.)])\s+', '', line) for line in lines)
        if len(lines) > 1
        else value
    )


def attendee_text(value):
    return '\n'.join(
        '- '
        + (
            str(a.get('name') or a.get('display_name') or a.get('email') or 'Unknown attendee')
            + (
                f' <{a["email"]}>'
                if a.get('email') and (a.get('name') or a.get('display_name'))
                else ''
            )
        )
        if isinstance(a, dict)
        else '- ' + str(a)
        for a in sequence(value)
    )


def render(db):
    state = read.graph(db)
    out = []
    names = {r['id']: store.name(r) for r in state['records']}
    for record in state['records']:
        rid, kind = record['id'], record['record_kind']
        table = store.KINDS[kind]
        row = next(r for r in state[table] if r['id'] == rid)
        sections, values, allowed = render_fields(row, table)
        sections = linked_sections(state, rid, kind, names, sections)
        body = markdown(sections)
        # Hash canonical relation record ids, not page ids, so two passes are stable.
        out.append(
            {
                'record_id': rid,
                'kind': kind,
                'values': values,
                'allowed': allowed,
                'sections': sections,
                'body': body,
                'body_hash': digest(body),
                'content_hash': digest([values, body]),
            }
        )
    return out


def render_fields(row, table):
    sections, values, allowed = {'Summary': EMPTY}, {}, {}
    for field, target, types in FIELDS[table]:
        value = record_status(row.get('state')) if field == 'record_status' else row.get(field)
        if types:
            allowed[target] = types.split()
            if target == 'Logistics':
                # Exact timing belongs only here, never in When or the body.
                if value not in (None, ''):
                    values[target] = (
                        values.get(target, '')
                        + '\n'
                        + f'{field.replace("_", " ").title()}: {value}'
                    ).strip()
            else:
                if field == 'aliases':
                    value = (
                        ', '.join(
                            str(v)
                            for v in sequence(value)
                            if EMAIL.fullmatch(str(v))
                            and not str(v)
                            .lower()
                            .endswith(('@s.whatsapp.net', '@g.us', '@lid', '@c.us'))
                        )
                        or None
                    )
                elif target == 'Next Actions':
                    value = actions(value)
                values[target] = value
            continue
        sections.setdefault(target, '')
        if field == 'metadata_json':
            if value in (None, '', '{}'):
                sections.pop(target, None)
                continue
            value = json.dumps(json.loads(value), ensure_ascii=False, sort_keys=True, indent=2)
        elif field == 'attendees_json':
            value = attendee_text(value)
        if value not in (None, ''):
            if field == 'summary':
                sections[target] = str(value)
            elif field == 'body_markdown':
                sections[target] = demote(str(value))
            else:
                part = (
                    str(value)
                    if field in ('attendees_json', 'metadata_json')
                    else f'### {field.replace("_", " ").title()}\n\n{demote(str(value))}'
                )
                sections[target] += ('\n\n' if sections[target] else '') + part
    return sections, values, allowed


def linked_sections(state, rid, kind, names, sections):
    # Metadata is the optional final section, not part of the compiled fields.
    metadata = sections.pop('Metadata', None)
    links = sorted(
        (l for l in state['record_links'] if l['from_record_id'] == rid),
        key=lambda l: (l['relation_kind'], l['to_record_id']),
    )
    if kind == 'meeting':
        project_ids = {
            l['to_record_id']
            for l in links
            if l['to_record_id'] in {p['id'] for p in state['projects']}
        }
        items = sorted(
            (i for i in state['items'] if i.get('project_id') in project_ids),
            key=lambda i: i['id'],
        )
        sections['Tasks by Linked Project'] = '\n'.join(
            f'- {names.get(i["project_id"], i["project_id"])}: {store.name(i)} [{i["id"]}]'
            for i in items
        )
    sections['Linked items'] = '\n'.join(
        f'- {l["relation_kind"]}: {names.get(l["to_record_id"], l["to_record_id"])} [{l["to_record_id"]}]'
        for l in links
    )
    refs = [
        {k: v for k, v in s.items() if k not in store.META and v is not None}
        for s in state['source_refs']
        if s['record_id'] == rid
    ]
    sections['Sources'] = '\n'.join(
        '- ' + json.dumps(s, ensure_ascii=False, sort_keys=True)
        for s in sorted(refs, key=lambda s: s['source_ref_id'])
    )
    sections['gsd id'] = rid
    if metadata:
        sections['Metadata'] = metadata
    sections = {k: v or EMPTY for k, v in sections.items()}
    return sections


def properties(row, schema, page_ids, relations=False):
    props, errors = {}, []
    for name, types in row['allowed'].items():
        actual = schema.get(name, {}).get('type')
        if actual not in types:
            errors.append({'property': name, 'expected': types, 'actual': actual or 'missing'})
            continue
        if (actual == 'relation') != relations:
            continue
        value = row['values'].get(name)
        if actual == 'relation':
            ids = sequence(value)
            missing = [rid for rid in ids if rid not in page_ids]
            if missing:
                errors.append({'property': name, 'unmapped_relations': missing})
                continue
            value = [page_ids[rid] for rid in ids]
        elif actual == 'multi_select' and name == 'Other Emails':
            value = value.split(', ') if value else []
        props[name] = encode(actual, value)
    return props, errors


def seed(db, port=PORT):
    for row in json.loads((Path(port) / 'notion.page_map.json').read_text())['rows']:
        db.execute(
            'INSERT OR IGNORE INTO notion_map VALUES (?,?,?,?,?)',
            (
                row['record_id'],
                row['notion_page_id'],
                row['content_hash'],
                row['body_hash'],
                row['synced_at'],
            ),
        )
    for row in json.loads((Path(port) / 'notion.object_map.json').read_text())['rows']:
        if row['key'].startswith('ds:'):
            db.execute(
                'INSERT OR IGNORE INTO notion_sources VALUES (?,?)',
                (row['key'][3:], row['notion_id']),
            )


LOCK_WAIT = 900


def config():
    return Path.home() / '.config/gsd'


def token():
    path = config() / 'notion.token'
    if path.exists():
        if path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('Notion token file must have mode 600')
        return path.read_text().strip() or None
    return os.environ.get('NOTION_API_TOKEN') or None


def gate(review=True):
    if not (config() / 'notion.token').is_file():
        raise ValueError('Notion writes refused: token file ~/.config/gsd/notion.token is missing')
    if review and not (config() / 'notion-check-reviewed').is_file():
        raise ValueError(
            'Notion writes refused: owner review flag ~/.config/gsd/notion-check-reviewed is missing; review --check first'
        )
    if not token():
        raise ValueError('Notion writes refused: token file is empty')


class UncertainWrite(ValueError):
    pass


def retry_delay(retry):
    try:
        delay = float(retry)
    except ValueError:
        try:
            delay = email.utils.parsedate_to_datetime(retry).timestamp() - time.time()
        except (ValueError, TypeError):
            delay = 1
    return max(0, delay)


def route_class(method, path):
    path = path.split('?', 1)[0]
    if re.fullmatch(r'/data_sources/[^/]+', path):
        return 'schema'
    if re.fullmatch(r'/data_sources/[^/]+/query', path):
        return 'query'
    if path == '/pages' and method == 'POST':
        return 'page_create'
    if re.fullmatch(r'/pages/[^/]+/markdown', path):
        return {'GET': 'markdown_get', 'PATCH': 'markdown_patch'}.get(method, 'other')
    if re.fullmatch(r'/pages/[^/]+', path):
        return {'GET': 'page_get', 'PATCH': 'page_patch'}.get(method, 'other')
    return 'other'


class Client:
    def __init__(self, secret, base='https://api.notion.com/v1', interval=0.34):
        self.secret, self.base, self.last, self.interval = secret, base, 0, interval
        self.started = time.monotonic()
        self.counts = {}
        self.http_429 = self.http_5xx = 0
        self.retry_after_seconds = self.pacing_seconds = 0.0

    def requests(self):
        return {
            'total': sum(sum(routes.values()) for routes in self.counts.values()),
            'by_method': {method: dict(routes) for method, routes in self.counts.items()},
            'http_429': self.http_429,
            'http_5xx': self.http_5xx,
            'retry_after_seconds': self.retry_after_seconds,
            'pacing_seconds': self.pacing_seconds,
            'elapsed_seconds': time.monotonic() - self.started,
        }

    def request(self, method, path, data=None):
        # A query POST is read-only. All other writes reconcile at their call site.
        is_read = method == 'GET' or path.endswith('/query')
        for attempt in range(5):
            delay = max(0, self.last + self.interval - time.monotonic())
            time.sleep(delay)
            self.pacing_seconds += delay
            self.last = time.monotonic()
            self.counts.setdefault(method, Counter())[route_class(method, path)] += 1
            req = urllib.request.Request(
                self.base + path,
                method=method,
                data=None if data is None else json.dumps(data).encode(),
                headers={
                    'Authorization': 'Bearer ' + self.secret,
                    'Notion-Version': VERSION,
                    'Content-Type': 'application/json',
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.http_429 += 1
                    delay = retry_delay(e.headers.get('Retry-After', '1'))
                    time.sleep(delay)
                    self.retry_after_seconds += delay
                    continue
                if 500 <= e.code < 600:
                    self.http_5xx += 1
                if e.code >= 500 or e.code == 408:
                    if is_read:
                        time.sleep(attempt + 1)
                        continue
                    raise UncertainWrite(
                        f'uncertain Notion write (HTTP {e.code}); read-back required'
                    ) from None
                raise ValueError(f'Notion HTTP {e.code}') from None
            except (OSError, ValueError, http.client.HTTPException):
                # HTTPError is handled above; JSON decode/connection failure may
                # follow an applied write. Never leak URLs, bodies or secrets.
                if is_read:
                    time.sleep(attempt + 1)
                    continue
                raise UncertainWrite(
                    'uncertain Notion write (transport/response); read-back required'
                ) from None
        raise ValueError('Notion request exhausted read/rate-limit retries')

    def paginate(self, path, method='GET', data=None, *, responses=None):
        cursor, seen = None, set()
        while True:
            if method == 'POST':
                result = self.request(
                    method, path, dict(data or {}, **({'start_cursor': cursor} if cursor else {}))
                )
            else:
                result = self.request(
                    method,
                    path
                    + (('&' if '?' in path else '?') + 'start_cursor=' + cursor if cursor else ''),
                )
            if responses is not None:
                responses.append(result)
            yield from result['results']
            if not result.get('has_more'):
                break
            next_cursor = result.get('next_cursor')
            if not next_cursor or next_cursor in seen:
                raise ValueError('Notion pagination incomplete; refusing partial results')
            seen.add(next_cursor)
            cursor = next_cursor

    def body(self, page):
        result = self.request('GET', f'/pages/{page}/markdown')
        if result.get('truncated') or result.get('unknown_block_ids'):
            raise ValueError('Notion markdown incomplete; refusing a partial-body edit')
        return result['markdown']

    def update_section(self, page, old, new):
        try:
            return self.request(
                'PATCH',
                f'/pages/{page}/markdown',
                {
                    'type': 'update_content',
                    'update_content': {'content_updates': [{'old_str': old, 'new_str': new}]},
                },
            )
        except UncertainWrite:
            current = self.body(page)
            if new in current and old not in current.replace(new, '', 1):
                return {'recovered': True}
            raise ValueError(
                'uncertain Notion section write; read-back did not confirm success'
            ) from None

    def append(self, page, text):
        try:
            return self.request(
                'PATCH',
                f'/pages/{page}/markdown',
                {
                    'type': 'insert_content',
                    'insert_content': {'content': text, 'position': {'type': 'end'}},
                },
            )
        except UncertainWrite:
            if text in self.body(page):
                return {'recovered': True}
            raise ValueError('uncertain Notion append; read-back did not confirm success') from None

    def set_properties(self, page, props):
        if not props:
            return
        try:
            self.request('PATCH', f'/pages/{page}', {'properties': props, 'notifications': SILENT})
        except UncertainWrite:
            actual = self.request('GET', f'/pages/{page}')['properties']
            if not matches(actual, props):
                raise ValueError(
                    'uncertain Notion properties write; read-back did not confirm success'
                ) from None


def canonical_property(value):
    typ = value.get('type') or next(iter(value))
    data = value.get(typ)
    if typ in ('rich_text', 'title'):
        return ''.join(v.get('plain_text', v.get('text', {}).get('content', '')) for v in data)
    if typ in ('select', 'status'):
        return data.get('name') if data else None
    if typ in ('relation', 'multi_select'):
        return sorted(v['id'].replace('-', '') if typ == 'relation' else v['name'] for v in data)
    if typ == 'date':
        if not data:
            return None
        return (canonical_date(data.get('start')), canonical_date(data.get('end')),
                data.get('time_zone'))
    return data


def matches(actual, expected):
    return all(
        k in actual and canonical_property(actual[k]) == canonical_property(v)
        for k, v in expected.items()
    )


def section_ranges(body):
    # Every H1/H2 outside fenced code is a boundary. Generated section bodies
    # demote those headings, so internal dividers remain ordinary content.
    starts = []
    for offset, line, outside in markdown_lines(body):
        if outside and line.startswith(('# ', '## ')):
            heading = re.sub(r'<span\s+discussion-urls=[^>]*>(.*?)</span>',
                             r'\1', line[3:].strip()) if line.startswith('## ') else None
            starts.append((offset, heading))
    result = {}
    for i, (start, heading) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        if heading is None:
            continue
        text = body[start:end].rstrip()
        if i + 1 < len(starts):
            # Strip only the inter-section separator immediately before the
            # next heading, never a divider within the section's content.
            text = re.sub(r'\n[ \t]*---[ \t]*$', '', text).rstrip()
        result.setdefault(heading, []).append(text)
    return result


def markdown_warnings(response, errors):
    for warning in response.get('warnings', []):
        errors.append('notion warning: ' + warning['code'])


def update_body(client, page, sections, *, errors=None):
    errors = errors if errors is not None else []
    current = client.body(page)
    ranges = section_ranges(current)
    sections = dict(sections)
    if 'Metadata' in ranges and 'Metadata' not in sections:
        # Clear stale metadata in place; retain the wrapper rather than delete
        # blocks which might carry comments. New empty pages omit it entirely.
        sections['Metadata'] = EMPTY
    for heading in sections:
        if len(ranges.get(heading, [])) > 1:
            raise ValueError('ambiguous Notion section marker: ' + heading)
    missing = []
    for heading, value in sections.items():
        old = ranges.get(heading, [])
        if not old:
            missing.append(heading)
        else:
            # Discussion wrappers identify the existing heading/comment anchor.
            # Keep the heading byte-for-byte; only its body is generated.
            new = old[0].split('\n', 1)[0] + '\n' + markdown({heading: value}).partition('\n')[2]
            if old[0] != new:
                markdown_warnings(client.update_section(page, old[0], new), errors)
    if missing:
        markdown_warnings(
            client.append(page, '\n\n---\n\n' + markdown({k: sections[k] for k in missing})),
            errors,
        )
    return missing


def find_created(client, source, row, props, *, mapped_ids=(), preflight=None, fresh=False):
    """Inspect only unknown identities, reusing one source inventory per publish.
    No negative query is proof that a timed-out create did not land later.
    """
    mapped = {identity(page) for page in mapped_ids if page}
    if preflight is None:
        preflight = {}
    if fresh or source not in preflight:
        preflight[source] = [
            page['id']
            for page in client.paginate('/data_sources/' + source + '/query', 'POST')
            if identity(page['id']) not in mapped
        ]
    found = []
    for page in preflight[source]:
        if identity(page) in mapped:
            continue
        body = client.body(page)
        markers = section_ranges(body).get('gsd id', [])
        if len(markers) == 1 and markers[0].partition('\n')[2].strip() == row['record_id']:
            found.append(page)
    if len(found) > 1:
        raise ValueError('multiple Notion pages carry this gsd id; reconcile manually')
    if found:
        # Read full page properties (query responses can be truncated).
        page = client.request('GET', '/pages/' + found[0])
        return page, matches(page['properties'], props)
    return None, False


def save_map(path, row, page, complete=False):
    with store.connect(path, True) as db:
        db.execute(
            'INSERT OR REPLACE INTO notion_map VALUES (?,?,?,?,?)',
            (
                row['record_id'],
                page,
                row['content_hash'] if complete else None,
                row['body_hash'] if complete else None,
                store.now(),
            ),
        )


def probe_comments(client, page):
    """Owner-named page only; never writes, resolves or deletes any comment."""

    def snapshot():
        result = {}

        def visit(block):
            for comment in client.paginate('/comments?block_id=' + block):
                result[comment['id']] = (block, digest(comment.get('rich_text', [])))
            for child in client.paginate('/blocks/' + block + '/children'):
                if child.get('has_children'):
                    visit(child['id'])
                else:
                    for comment in client.paginate('/comments?block_id=' + child['id']):
                        result[comment['id']] = (child['id'], digest(comment.get('rich_text', [])))

        visit(page)
        return result

    before = snapshot()
    inline_before = {k: v for k, v in before.items() if v[0] != page}
    marker = '## gsd comment probe ' + uuid.uuid4().hex
    old, new = marker + '\n\nBefore edit.', marker + '\n\nAfter section-local edit.'
    client.append(page, '\n\n' + old)
    client.update_section(page, old, new)
    after = snapshot()
    survived = all(after.get(k) == v for k, v in before.items())
    return {
        'page_id': page,
        'comments_before': len(before),
        'comments_after': len(after),
        'inline_comments_before': len(inline_before),
        'survived': survived if inline_before else None,
        'result': ('survived' if survived else 'FAILED: comments missing or changed')
        if inline_before
        else 'inconclusive: add an inline comment on an unrelated block and rerun',
        'marker': marker,
        'note': 'Unresolved comments visible to this integration only; inspect resolved comments in the UI.',
    }


def publish(
    path,
    check=False,
    do_seed=False,
    probe=None,
    *,
    client=None,
    hardcut=False,
    plan=False,
    yes=False,
):
    if (plan or yes) and not hardcut:
        raise ValueError('--plan and --yes require --hardcut')
    if sum(map(bool, (check, do_seed, probe, hardcut))) > 1:
        raise ValueError('--check, --seed, --probe-comments and --hardcut are mutually exclusive')
    if hardcut and not plan and not yes:
        raise ValueError('Notion hardcut requires explicit --yes (or use --plan)')
    if do_seed:
        return _publish(path, check, do_seed, probe, client=client)
    if not check and not (hardcut and plan):
        gate(review=not hardcut)
    # Timer and manual publishes must not race a missing-page query/create.
    lock_path = Path(str(Path(path).expanduser().resolve()) + '.notion.lock')
    with lock_path.open('a') as lock:
        os.chmod(lock_path, 0o600)
        # The inbox's comment sweep holds the lock for minutes; wait for it rather than fail the run.
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('another Notion publish is running for this store') from None
                time.sleep(2)
        if hardcut:
            if client is None:
                secret = token()
                if not secret:
                    raise ValueError('Notion hardcut requires a token, including --plan')
                client = Client(secret)
            return hardcut_publish(path, client, plan)
        try:
            return _publish(path, check, do_seed, probe, client=client)
        except (ValueError, OSError):
            if check:
                publish_log(path, {'requests': client.requests() if client else None, 'plan': {},
                                   'projection_check': {'known': False, 'reason': 'remote check failed; inspect --check error'}})
            raise


def _publish(path, check=False, do_seed=False, probe=None, *, client=None):
    if sum((bool(check), bool(do_seed), bool(probe))) > 1:
        raise ValueError('--check, --seed and --probe-comments are mutually exclusive')
    if do_seed:
        with store.connect(path, True) as db:
            if not store.has_table(db, 'notion_map'):
                raise ValueError('run gsd migrate first')
            seed(db)
        return {'seeded': True}
    if not check:
        gate()
    if client is None:
        secret = token()
        if secret:
            client = Client(secret)
    if probe:
        try:
            page_id = str(uuid.UUID(probe))
        except ValueError:
            raise ValueError('--probe-comments requires a Notion page UUID') from None
        return probe_comments(client, page_id)
    with store.connect(path) as db:
        rendered = render(db)
        mapped = (
            {r['record_id']: dict(r) for r in db.execute('SELECT * FROM notion_map')}
            if store.has_table(db, 'notion_map')
            else {}
        )
        sources = (
            dict(db.execute('SELECT kind,data_source_id FROM notion_sources'))
            if store.has_table(db, 'notion_sources')
            else {}
        )
        tombstones = {
            rid: old for rid, old in mapped.items()
            if (latest := store.current(db, 'records', (rid,), include_deleted=True))
            and latest['op'] == 'delete'
        }
    schemas, differences = publish_schemas(client, sources)
    counts, pages, ids = publish_plan(rendered, mapped, schemas, sources)
    archives = [
        {'record_id': rid, 'page_id': old['page_id'], 'action': 'archive', 'errors': []}
        for rid, old in tombstones.items()
    ]
    if archives:
        counts['archive'] = len(archives)
    report = {
        'api_version': VERSION,
        'total': len(rendered),
        'mapped': len(mapped),
        'plan': dict(counts),
        'pages': pages + archives,
        'archived': 0,
        'schema_basis': 'live data-source schemas'
        if client
        else 'expected schema from FIELDS table (no token; not remotely verified)',
        'schema_differences': differences,
        'automations': 'Not exposed by public REST API: owner must inspect database automations; silent mode does NOT suppress automation notifications',
        'notifications': 'Page create/property updates use notifications.mode=silent. Markdown endpoint documents no suppression option; notifications cannot be guaranteed silent.',
        'inline_comments': 'Not verified live. First live step: --probe-comments PAGE_ID with an existing inline comment on another block.',
        'appended_sections': [],
    }
    if check:
        try:
            reconcile(client, report, rendered, mapped, schemas, sources)
            report['projection_check'] = projection_summary(report) if client else {'known': False, 'reason': report['remote_checks']}
            return report
        finally:
            report['requests'] = client.requests() if client else None
            if 'projection_check' in report:
                publish_log(path, report)
    try:
        if any(p['errors'] for p in pages) or any(
            diff['missing_or_wrong_type'] for diff in differences.values()
        ):
            report['refused'] = (
                'Resolve reported schema/source differences before publishing; no writes made'
            )
            return report
        changed = publish_pages(path, client, rendered, pages, schemas, sources, ids, report)
        report['published'] = sum(not p['errors'] for _, p in changed)
        for entry in archives:
            if not entry['page_id']:
                entry['errors'].append('uncertain Notion create; reconcile identity before archive')
                continue
            try:
                archive_page(client, entry['page_id'])
            except ValueError as e:
                entry['errors'].append(str(e))
                continue
            # Keep the mapping until absence/trash is confirmed, including after
            # an uncertain write. A failed run can safely resume next publish.
            with store.connect(path, True) as db:
                db.execute('DELETE FROM notion_map WHERE record_id=?', (entry['record_id'],))
            report['archived'] += 1
        return report
    finally:
        report['requests'] = client.requests()
        publish_log(path, report)


def publish_log(path, report):
    # An allowlist, not a sanitized copy of the report: never persist page data.
    entry = {
        'timestamp': store.now(),
        'requests': report['requests'],
        'plan': report['plan'],
        'published': report.get('published', 0),
        'archived': report.get('archived', 0),
    }
    if 'projection_check' in report:
        entry['projection_check'] = report['projection_check']
    dest = Path(path).expanduser().resolve().parent / 'notion-publish.jsonl'
    with open(dest, 'a', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(json.dumps(entry, sort_keys=True) + '\n')


def hardcut_body(client, page, body):
    if client.body(page) == body:
        return
    try:
        client.request(
            'PATCH',
            f'/pages/{page}/markdown',
            {
                'type': 'replace_content',
                'replace_content': {'new_str': body, 'allow_deleting_content': True},
            },
        )
    except UncertainWrite:
        if client.body(page) != body:
            raise ValueError(
                'uncertain Notion body replacement; read-back did not confirm success'
            ) from None


def identity(value):
    return value.replace('-', '')


def hardcut_schema(table, actual, sources):
    """Only the title has an unambiguous rename; other legacy fields are replaced.
    One patch per source, single-sided relations (no extra reciprocal fields).
    """
    desired = expected_schema(table, sources)
    patch, ops = {}, {k: [] for k in ('renames', 'retypes', 'adds', 'deletes')}
    actual = dict(actual)
    titles = [name for name, prop in actual.items() if prop['type'] == 'title']
    if len(titles) != 1:
        raise ValueError('Notion source must have exactly one title property')
    old_title = titles[0]
    if old_title != 'Name':
        if 'Name' in actual:
            # Use immutable ids so deleting a non-title Name and renaming the
            # real title cannot target the same property in this single patch.
            collision = actual.pop('Name')
            if not collision.get('id') or not actual[old_title].get('id'):
                raise ValueError('Notion title rename collision requires property ids')
            patch[collision['id']] = None
            ops['deletes'].append({'property_id': collision['id'], 'name': 'Name'})
        prop = actual.pop(old_title)
        patch[prop.get('id', old_title)] = {'name': 'Name'}
        ops['renames'].append(
            {'property_id': prop.get('id', old_title), 'from': old_title, 'to': 'Name'}
        )
        actual['Name'] = prop
    for name, prop in actual.items():
        key = prop.get('id', name)
        if name not in desired:
            patch[key] = None
            ops['deletes'].append({'property_id': key, 'name': name})
    for name, prop in desired.items():
        old = actual.get(name)
        key = old.get('id', name) if old else name
        if old is None:
            patch[key] = prop
            ops['adds'].append({'name': name})
        elif old['type'] != prop['type'] or (
            prop['type'] == 'relation'
            and (
                identity(old.get('relation', {}).get('data_source_id', ''))
                != identity(prop['relation']['data_source_id'])
                or old.get('relation', {}).get('type') != 'single_property'
            )
        ):
            patch[key] = prop
            ops['retypes'].append({'property_id': key, 'name': name, 'type': prop['type']})
    return (
        desired,
        patch,
        {'data_source_id': sources[table], 'counts': {k: len(v) for k, v in ops.items()}, **ops},
    )


def hardcut_snapshot(client, sources):
    schemas, inventory = {}, {}
    for table, source in sources.items():
        schemas[table] = client.request('GET', '/data_sources/' + source)
        inventory[table] = {}
        for result in client.paginate('/data_sources/' + source + '/query', 'POST'):
            page_id = result['id']
            page = client.request('GET', '/pages/' + page_id)
            if page.get('in_trash') or page.get('archived'):
                continue
            items = {}
            # Page/query properties can truncate at 25 references. Preserve the
            # complete property-item stream as well as the raw page for undo.
            for name, prop in page['properties'].items():
                typ = prop.get('type') or next(iter(prop))
                if prop.get('id') and typ in ('title', 'rich_text', 'relation', 'people', 'rollup'):
                    url = (
                        '/pages/'
                        + page_id
                        + '/properties/'
                        + urllib.parse.quote(urllib.parse.unquote(prop['id']), safe='')
                    )
                    items[name] = []
                    values = list(client.paginate(url, responses=items[name]))
                    if typ != 'rollup':
                        prop[typ] = [item[typ] for item in values]
                        prop.pop('has_more', None)
                elif prop.get('has_more'):
                    raise ValueError('Notion property incomplete; refusing hardcut')
            inventory[table][identity(page_id)] = {
                'page': page,
                'markdown': client.body(page_id),
                'property_items': items,
            }
    return schemas, inventory


def hardcut_export(schemas, inventory, mapped):
    root = Path.home() / '.local/share/gsd/notion-export'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    dest = root / stamp
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)

    def save(path, value, markdown=False):
        data = value if markdown else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
        with open(path, 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())

    save(dest / 'notion_map.json', list(mapped.values()))
    for table, schema in schemas.items():
        folder = dest / table
        folder.mkdir(mode=0o700)
        folder.chmod(0o700)
        save(folder / 'schema.json', schema)
        for n, entry in enumerate(inventory[table].values()):
            # Numeric filenames never interpret a remote id as a filesystem path.
            save(
                folder / f'{n:06d}.json',
                {
                    'page': entry['page'],
                    'property_items': entry['property_items'],
                },
            )
            save(folder / f'{n:06d}.md', entry['markdown'], markdown=True)
    save(dest / 'complete.json', {'complete': True, 'pages': sum(map(len, inventory.values()))})
    for folder in [*(dest / table for table in schemas), dest, root]:
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return str(dest)


def hardcut_publish(path, client, plan):
    with store.connect(path) as db:
        if not store.has_table(db, 'notion_map') or not store.has_table(db, 'notion_sources'):
            raise ValueError('run gsd migrate and publish notion --seed first')
        rendered = render(db)
        mapped = {r['record_id']: dict(r) for r in db.execute('SELECT * FROM notion_map')}
        sources = dict(db.execute('SELECT kind,data_source_id FROM notion_sources'))
    if set(sources) != set(FIELDS) or len({identity(v) for v in sources.values()}) != len(FIELDS):
        raise ValueError('hardcut requires four distinct configured data sources; run --seed')
    raw_schemas, inventory = hardcut_snapshot(client, sources)
    schemas, patches, schema_report = {}, {}, {}
    for table in FIELDS:
        schemas[table], patches[table], schema_report[table] = hardcut_schema(
            table, raw_schemas[table]['properties'], sources
        )
    ids, used = {}, set()
    for row in rendered:
        old = mapped.get(row['record_id'])
        table = store.KINDS[row['kind']]
        entry = inventory[table].get(identity(old['page_id'])) if old and old['page_id'] else None
        if entry and identity(old['page_id']) not in used:
            ids[row['record_id']] = entry['page']['id']
            used.add(identity(old['page_id']))
    # Reuse the ordinary publisher's hash contract so the next ordinary run is a no-op.
    publish_plan(rendered, mapped, schemas, sources)
    actions = {k: [] for k in ('overwrites', 'creates', 'archives')}
    pages = []
    prospective_ids = {
        row['record_id']: ids.get(row['record_id'], row['record_id']) for row in rendered
    }
    for row in rendered:
        rid, table = row['record_id'], store.KINDS[row['kind']]
        props, errors = properties(row, schemas[table], prospective_ids)
        relations, rel_errors = properties(row, schemas[table], prospective_ids, relations=True)
        if errors or rel_errors:
            raise ValueError('hardcut has invalid properties or dangling gsd relations')
        page_id = ids.get(rid)
        entry = inventory[table].get(identity(page_id)) if page_id else None
        action = 'unchanged'
        if not entry:
            action = 'create'
        elif (
            patches[table]
            or not matches(entry['page']['properties'], props | relations)
            or entry['markdown'] != row['body']
        ):
            action = 'overwrite'
        if action != 'unchanged':
            actions[action + 's'].append({'record_id': rid, 'page_id': page_id})
        pages.append({'action': action, 'errors': []})
    for table in FIELDS:
        actions['archives'].extend(
            {'page_id': entry['page']['id'], 'data_source_id': sources[table]}
            for key, entry in inventory[table].items()
            if key not in used
        )
    report = {
        'mode': 'plan' if plan else 'done',
        'schema': schema_report,
        'pages': {'counts': {k: len(v) for k, v in actions.items()}, **actions},
    }
    if plan:
        return report
    # No local mapping or remote mutation precedes the complete, durable undo record.
    report['export_path'] = hardcut_export(raw_schemas, inventory, mapped)
    for table in patches:
        # Deleting one side of a two-way relation removes its twin in another
        # source, so each source's patch is computed from a fresh read.
        actual = client.request('GET', '/data_sources/' + sources[table])['properties']
        patch = hardcut_schema(table, actual, sources)[1]
        if patch:
            try:
                client.request('PATCH', '/data_sources/' + sources[table], {'properties': patch})
            except UncertainWrite:
                actual = client.request('GET', '/data_sources/' + sources[table])['properties']
                if hardcut_schema(table, actual, sources)[1]:
                    raise ValueError(
                        'uncertain Notion schema write; read-back did not confirm success'
                    ) from None
    publish_pages(path, client, rendered, pages, schemas, sources, ids, {}, hardcut=True)
    # Even already-correct pages need hashes when the initial cut started with an
    # unseeded hash. Remove stale mappings so deleted records cannot shield extras.
    with store.connect(path, True) as db:
        active = {row['record_id'] for row in rendered}
        for rid in mapped.keys() - active:
            db.execute('DELETE FROM notion_map WHERE record_id=?', (rid,))
    for row in rendered:
        save_map(path, row, ids[row['record_id']], complete=True)
    for entry in actions['archives']:
        archive_page(client, entry['page_id'])
    cfg = config()
    cfg.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = cfg / 'notion-check-reviewed'
    with open(marker, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as out:
        out.write('D16: export-first Notion hardcut completed.\n')
    marker.chmod(0o600)
    return report


def archive_page(client, page):
    """Archive only a known page, confirming success before callers unmap it."""
    def trashed():
        try:
            meta = client.request('GET', '/pages/' + page)
        except ValueError as e:
            if 'HTTP 404' not in str(e):
                raise
            return True
        return bool(meta.get('in_trash') or meta.get('archived'))

    if trashed():
        return
    uncertain = False
    try:
        client.request('PATCH', '/pages/' + page, {'in_trash': True, 'notifications': SILENT})
    except UncertainWrite:
        uncertain = True
    if not trashed():
        raise ValueError(
            ('uncertain Notion archive' if uncertain else 'Notion archive')
            + '; read-back did not confirm success'
        )


def publish_schemas(client, sources):
    schemas, differences = {}, {}
    for table in FIELDS:
        schemas[table] = (
            client.request('GET', '/data_sources/' + sources[table])['properties']
            if client and table in sources
            else expected_schema(table, sources)
        )
        expected = expected_schema(table, sources)
        allowed = {name: types.split() for _, name, types in FIELDS[table] if types}
        differences[table] = {
            'extra': sorted(set(schemas[table]) - set(expected)),
            'missing_or_wrong_type': [
                {
                    'property': name,
                    'expected': types,
                    'actual': schemas[table].get(name, {}).get('type') or 'missing',
                }
                for name, types in allowed.items()
                if schemas[table].get(name, {}).get('type') not in types
                or (expected[name]['type'] == 'relation' and
                    identity(schemas[table].get(name, {}).get('relation', {}).get('data_source_id', ''))
                    != identity(expected[name]['relation']['data_source_id']))
            ],
        }
    return schemas, differences


def publish_plan(rendered, mapped, schemas, sources):
    counts, pages = Counter(), []
    ids = {rid: r['page_id'] for rid, r in mapped.items() if r['page_id']}
    for row in rendered:
        table = store.KINDS[row['kind']]
        _, errors = properties(row, schemas[table], ids)
        row['content_hash'] = digest(
            [
                row['values'],
                row['body'],
                {name: schemas[table].get(name, {}).get('type') for name in row['allowed']},
            ]
        )
        old = mapped.get(row['record_id'])
        action = (
            'create'
            if not old
            else 'unchanged'
            if old['content_hash'] == row['content_hash']
            else 'update'
        )
        if table not in sources:
            errors.append({'data_source': 'missing; run --seed'})
        counts[row['kind'] + ':' + action] += 1
        pages.append(
            {'record_id': row['record_id'], 'kind': row['kind'], 'action': action, 'errors': errors}
        )
    return counts, pages, ids


def projection_summary(report):
    """Bounded findings only: no property values or page bodies in the log."""
    classes = {name: report[name] for name in (
        'property_differences', 'duplicate_sections', 'unmapped_pages',
        'missing_pages', 'wrong_source_pages', 'trashed_pages',
        'unreachable_pages', 'appended_sections', 'source_differences', 'pending_archives')}
    classes['schema_differences'] = [
        {'page_id': table} for table, diff in report['schema_differences'].items()
        if diff['extra'] or diff['missing_or_wrong_type']]
    return {'known': True, 'classes': {
        name: {'count': len(rows), 'ids': list(dict.fromkeys(r['page_id'] for r in rows if r['page_id']))[:2]}
        for name, rows in classes.items()}}


def latest_projection_check(path):
    """The observation envelope belongs to the publisher, not its consumers."""
    log = Path(path).expanduser().resolve().parent / 'notion-publish.jsonl'
    latest = None
    if log.exists():
        with log.open() as stream:
            for line in stream:
                entry = json.loads(line)
                if 'projection_check' in entry:
                    latest = entry
    return latest


def projection_check_due(path, now):
    """One check after the morning steady publish, using the existing ledger."""
    local = now.astimezone(owner.zone())
    if local.hour >= 12:
        return False
    try:
        latest = latest_projection_check(path)
        return not latest or datetime.datetime.fromisoformat(
            store.iso(latest['timestamp'])
        ).astimezone(owner.zone()).date() < local.date()
    except (OSError, ValueError, TypeError, KeyError):
        return True


def projection_telemetry(path):
    """An old observation is unknown even if all its checks once passed."""
    try:
        latest = latest_projection_check(path)
        if latest:
            age = datetime.datetime.now(datetime.UTC) - datetime.datetime.fromisoformat(
                store.iso(latest['timestamp'])
            )
            if age < datetime.timedelta(0) or age > PROJECTION_CHECK_MAX_AGE:
                return [{'name': 'notion_projection', 'ok': False,
                         'reason': 'unknown: Notion projection check expired or future-dated; run publish notion --check'}], []
    except (OSError, ValueError, TypeError, KeyError):
        return [{'name': 'notion_projection', 'ok': False, 'reason': 'unknown: unreadable Notion publication ledger'}], []
    if not latest or not latest['projection_check']['known']:
        reason = latest['projection_check']['reason'] if latest else 'no publish notion --check recorded'
        return [{'name': 'notion_projection', 'ok': False, 'reason': 'unknown: ' + reason}], []
    checks, metrics = [], []
    for name, result in latest['projection_check']['classes'].items():
        checks.append({'name': 'notion_' + name, 'ok': result['count'] == 0,
                       'reason': f"{result['count']} at {latest['timestamp']}; ids: " + ','.join(result['ids'])})
        metrics.append({'name': 'notion_' + name, 'labels': {}, 'value': result['count'], 'unit': 'count'})
    return checks, metrics


def reconcile(client, report, rendered, mapped, schemas, sources):
    if client:
        report['edited_since_2026_08_06'], report['unreachable_pages'] = [], []
        rendered_by_id = {r['record_id']: r for r in rendered}
        ids = {rid: old['page_id'] for rid, old in mapped.items() if old['page_id']}
        for name in ('property_differences', 'duplicate_sections', 'unmapped_pages',
                     'missing_pages', 'wrong_source_pages', 'trashed_pages', 'pending_archives'):
            report[name] = []
        report['missing_pages'] = [
            {'record_id': rid, 'page_id': None, 'why': 'no mapped page'}
            for rid in rendered_by_id.keys() - ids.keys()
        ]
        archives = {p['record_id'] for p in report['pages'] if p['action'] == 'archive'}
        report['source_differences'] = [{'page_id': table} for table in FIELDS if table not in sources]
        inventory = {}
        for table, source in sources.items():
            inventory[table] = {identity(p['id']) for p in client.paginate(
                '/data_sources/' + source + '/query', 'POST')}
            for page in sorted(inventory[table] - {identity(p) for p in ids.values()}):
                report['unmapped_pages'].append({'page_id': page, 'data_source': source})
        for rid, old in mapped.items():
            page = old['page_id']
            if not page:
                if rid in archives:
                    report['pending_archives'].append({'record_id': rid, 'page_id': None})
                continue
            # A mapped page that is gone, trashed or no longer shared is a reconcile finding, not a crash.
            try:
                meta = client.request('GET', '/pages/' + page)
            except ValueError as e:
                if 'HTTP 404' not in str(e):
                    raise
                entry = {'record_id': rid, 'page_id': page, 'why': 'not found or not shared'}
                report['unreachable_pages'].append(entry)
                report['missing_pages'].append(entry)
                continue
            if meta.get('in_trash') or meta.get('archived'):
                report['trashed_pages'].append({'record_id': rid, 'page_id': page})
                report['unreachable_pages'].append(
                    {'record_id': rid, 'page_id': page, 'why': 'in trash'}
                )
                continue
            if rid in archives:
                report['pending_archives'].append({'record_id': rid, 'page_id': page})
            if meta.get('last_edited_time', '') > owner.get('notion_import_cutoff'):
                report['edited_since_2026_08_06'].append(page)
            if rid in rendered_by_id:
                row = rendered_by_id[rid]
                table = store.KINDS[row['kind']]
                source = sources.get(table)
                if not source or identity(meta.get('parent', {}).get('data_source_id', '')) != identity(source):
                    report['wrong_source_pages'].append({'record_id': rid, 'page_id': page})
                elif identity(page) not in inventory.get(table, set()):
                    report['missing_pages'].append({'record_id': rid, 'page_id': page, 'why': 'absent from data-source inventory'})
                expected, errors = properties(row, schemas[table], ids)
                relations, relation_errors = properties(row, schemas[table], ids, relations=True)
                expected.update(relations)
                different = [name for name, value in expected.items()
                             if not matches(meta.get('properties', {}), {name: value})]
                if different or errors or relation_errors:
                    report['property_differences'].append({'record_id': rid, 'page_id': page,
                                                           'properties': different,
                                                           'errors': errors + relation_errors})
                ranges = section_ranges(client.body(page))
                duplicates = [heading for heading, entries in ranges.items() if len(entries) > 1]
                if duplicates:
                    report['duplicate_sections'].append({'page_id': page, 'sections': duplicates})
                missing = [k for k in row['sections'] if k not in ranges]
                if missing:
                    report['appended_sections'].append({'page_id': page, 'sections': missing})
    else:
        report['remote_checks'] = 'No token: remote reconcile unavailable'
    return report


def publish_pages(path, client, rendered, pages, schemas, sources, ids, report, *, hardcut=False):
    changed = [(r, p) for r, p in zip(rendered, pages) if p['action'] != 'unchanged']
    preflight = {}
    # Pass 1: all identities first. Keep confirmed mappings even if pass 2 fails;
    # null hashes mean the page still needs completion, never that it is synced.
    for row, page_plan in changed:
        props, _ = properties(row, schemas[store.KINDS[row['kind']]], ids)
        page = ids.get(row['record_id'])
        if not page:
            source = sources[store.KINDS[row['kind']]]
            page = create_page(
                path, client, source, row, props, recover=not hardcut,
                mapped_ids=ids.values(), preflight=preflight,
            )
            ids[row['record_id']] = page
            save_map(path, row, page)
        else:
            save_map(path, row, page)  # Invalidate completion before any remote mutation.
        client.set_properties(page, props)
        if hardcut:
            hardcut_body(client, page, row['body'])
        else:
            missing = update_body(client, page, row['sections'], errors=page_plan['errors'])
            if missing:
                report['appended_sections'].append({'page_id': page, 'sections': missing})
    # Pass 2: encode relations only after every target has a confirmed page id.
    for row, plan in changed:
        if plan['errors']:
            continue
        props, errors = properties(row, schemas[store.KINDS[row['kind']]], ids, relations=True)
        if errors:
            plan['errors'] = errors
            continue
        page = ids[row['record_id']]
        client.set_properties(page, props)
        save_map(path, row, page, complete=True)
        report['published'] = report.get('published', 0) + 1
    return changed


def create_page(path, client, source, row, props, *, recover=True, mapped_ids=(), preflight=None):
    found, _ = (
        find_created(client, source, row, props, mapped_ids=mapped_ids, preflight=preflight)
        if recover else (None, False)
    )
    if found:
        page = found['id']
    else:
        with store.connect(path) as db:
            pending = db.execute(
                'SELECT 1 FROM notion_map WHERE record_id=? AND page_id IS NULL',
                (row['record_id'],),
            ).fetchone()
        if pending:
            raise ValueError('uncertain Notion create; awaiting remote reconciliation; no create retry attempted')
        # Persist before POST: a process exit after sending is uncertain too.
        save_map(path, row, None)
        try:
            page = client.request(
                'POST',
                '/pages',
                {
                    'parent': {'type': 'data_source_id', 'data_source_id': source},
                    'properties': props,
                    'markdown': row['body'],
                    'notifications': SILENT,
                },
            )['id']
        except UncertainWrite:
            found, confirmed = find_created(
                client, source, row, props, mapped_ids=mapped_ids,
                preflight=preflight, fresh=True,
            )
            if not found or not confirmed:
                raise ValueError(
                    'uncertain Notion create; read-back did not confirm success; no create retry attempted'
                ) from None
            page = found['id']
        except ValueError:
            # A definite refusal is not an unresolved write. UncertainWrite is
            # handled above and leaves its pending identity durable.
            with store.connect(path, True) as db:
                db.execute('DELETE FROM notion_map WHERE record_id=? AND page_id IS NULL',
                           (row['record_id'],))
            raise
    if preflight is not None and source in preflight:
        preflight[source] = [p for p in preflight[source] if identity(p) != identity(page)]
    return page
