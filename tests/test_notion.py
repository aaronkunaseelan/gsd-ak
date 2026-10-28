"""Synthetic SQLite -> real HTTP server speaking the Notion REST protocol."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db, notion, read, write

RETIRED = {
    'parent_item_id',
    'parent_project_item_id',
    'thread_url',
    'latest_deliverable_url',
    'calendar_event_id',
    'schedule_status',
    'planned_start',
    'planned_end',
    'estimated_minutes',
    'effort_band',
    'notes',
}
DECIDES = [{'ref': 'gmail:synthetic', 'role': 'decides', 'excerpt': 'Synthetic decision'}]


class FakeNotion:
    def __init__(self):
        self.schemas = {table: notion.expected_schema(table, {t: t for t in notion.FIELDS})
                        for table in notion.FIELDS}
        self.pages, self.bodies, self.calls = {}, {}, []
        self.versions = []
        self.comments = {'outside': [{'id': 'comment-1', 'rich_text': notion.rich('Keep this')}]}
        self.children = {}
        self.fail = None
        self.drop_comments = False
        self.query_page_size = 100
        self.incomplete_bodies = set()
        self.incomplete_pagination = False
        self.before_write = None
        self.property_items = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                data = json.loads(
                    self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'null'
                )
                path = self.path.split('?')[0]
                method = self.command
                fake.calls.append((method, self.path, data, time.monotonic()))
                fake.versions.append(self.headers.get('Notion-Version'))
                fail = fake.fail
                trigger = fail and fail['method'] == method and fail['path'] == path
                if trigger and fail.get('skip', 0):
                    fail['skip'] -= 1
                    trigger = False
                old_pages = json.loads(json.dumps(fake.pages))
                old_bodies = dict(fake.bodies)
                old_schemas = json.loads(json.dumps(fake.schemas))
                if fake.before_write and method != 'GET' and not path.endswith('/query'):
                    fake.before_write()
                if trigger and method == 'GET':
                    fake.fail = None
                    return self.send(fail['status'], {})
                if trigger and fail.get('status') == 429:
                    fake.fail = None
                    return self.send(429, {}, {'Retry-After': str(fail.get('delay', 0.05))})
                if method == 'GET' and path.startswith('/data_sources/'):
                    return self.send(200, {'properties': fake.schemas[path.split('/')[2]]})
                if method == 'POST' and path.endswith('/query'):
                    source = path.split('/')[2]
                    pages = [
                        p
                        for p in fake.pages.values()
                        if p['parent']['data_source_id'] == source
                        and not p.get('in_trash')
                        and not p.get('archived')
                    ]
                    start = int((data or {}).get('start_cursor', 0))
                    end = start + fake.query_page_size
                    return self.send(
                        200,
                        {
                            'results': pages[start:end],
                            'has_more': end < len(pages),
                            'next_cursor': str(end)
                            if end < len(pages) and not fake.incomplete_pagination
                            else None,
                        },
                    )
                if method == 'GET' and path == '/comments':
                    block = self.path.split('block_id=')[1].split('&')[0]
                    return self.send(
                        200, {'results': fake.comments.get(block, []), 'has_more': False}
                    )
                if method == 'GET' and path.startswith('/blocks/'):
                    return self.send(
                        200,
                        {'results': fake.children.get(path.split('/')[2], []), 'has_more': False},
                    )
                if method == 'GET' and path.startswith('/pages/'):
                    page = path.split('/')[2]
                    if page not in fake.pages:
                        return self.send(404, {'object': 'error', 'code': 'object_not_found'})
                    if '/properties/' in path:
                        name = path.split('/properties/')[1]
                        items = fake.property_items[page, name]
                        cursor = (
                            int(self.path.split('start_cursor=')[1])
                            if 'start_cursor=' in self.path
                            else 0
                        )
                        end = cursor + fake.query_page_size
                        return self.send(
                            200,
                            {
                                'results': items[cursor:end],
                                'has_more': end < len(items),
                                'next_cursor': str(end),
                            },
                        )
                    if path.endswith('/markdown'):
                        return self.send(
                            200,
                            {
                                'markdown': fake.bodies[page],
                                'truncated': page in fake.incomplete_bodies,
                            },
                        )
                    return self.send(200, fake.pages[page])
                if method == 'PATCH' and path.startswith('/data_sources/'):
                    source = path.split('/')[2]
                    schema = fake.schemas[source]
                    for key, value in data['properties'].items():
                        name = next((n for n, p in schema.items() if p.get('id', n) == key), key)
                        old = schema.get(name, {})
                        if value is None:
                            if name not in schema:
                                return self.send(404, {})
                            if old.get('type') == 'title':
                                return self.send(400, {})
                            schema.pop(name, None)
                            if old.get('twin'):
                                # Notion removes a two-way relation's other side too.
                                fake.schemas[old['twin'][0]].pop(old['twin'][1], None)
                            for p in fake.pages.values():
                                if p['parent']['data_source_id'] == source:
                                    p['properties'].pop(name, None)
                        else:
                            new_name = value.get('name', name)
                            schema.pop(name, None)
                            schema[new_name] = old | value
                            for p in fake.pages.values():
                                if (
                                    p['parent']['data_source_id'] == source
                                    and name in p['properties']
                                ):
                                    current = p['properties'].pop(name)
                                    typ = schema[new_name]['type']
                                    p['properties'][new_name] = (
                                        current
                                        if next(iter(current)) == typ
                                        else notion.encode(typ, None)
                                    )
                    result = {'properties': schema}
                elif method == 'POST' and path == '/pages':
                    page = 'page-' + str(len(fake.pages) + 1)
                    source = data['parent']['data_source_id']
                    if not fake.valid_props(source, data['properties']):
                        return self.send(400, {})
                    fake.pages[page] = {
                        'id': page,
                        'parent': data['parent'],
                        'properties': json.loads(json.dumps(data['properties'])),
                        'last_edited_time': '2026-09-01T00:00:00Z',
                    }
                    fake.bodies[page] = data['markdown']
                    result = fake.pages[page]
                elif method == 'PATCH' and path.endswith('/markdown'):
                    page = path.split('/')[2]
                    if data['type'] == 'update_content':
                        for update in data['update_content']['content_updates']:
                            if fake.bodies[page].count(update['old_str']) != 1:
                                return self.send(400, {})
                            fake.bodies[page] = fake.bodies[page].replace(
                                update['old_str'], update['new_str']
                            )
                        if fake.drop_comments:
                            fake.comments.clear()
                    elif data['type'] == 'insert_content':
                        fake.bodies[page] += data['insert_content']['content']
                    elif data['type'] == 'replace_content':
                        if not data['replace_content'].get('allow_deleting_content'):
                            return self.send(400, {})
                        fake.bodies[page] = data['replace_content']['new_str']
                    else:
                        return self.send(400, {})
                    result = {}
                elif method == 'PATCH' and path.startswith('/pages/'):
                    page = path.split('/')[2]
                    source = fake.pages[page]['parent']['data_source_id']
                    if not fake.valid_props(source, data.get('properties', {})):
                        return self.send(400, {})
                    fake.pages[page]['properties'].update(data.get('properties', {}))
                    if 'in_trash' in data:
                        fake.pages[page]['in_trash'] = data['in_trash']
                    result = fake.pages[page]
                else:
                    return self.send(404, {})
                if trigger:
                    fake.fail = None
                    if fail.get('apply', True) is False:
                        fake.pages = old_pages
                        fake.bodies = old_bodies
                        fake.schemas = old_schemas
                    if fail.get('disconnect'):
                        self.close_connection = True
                        return
                    return self.send(fail['status'], {})
                return self.send(200, result)

            def send(self, status, data, headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            do_GET = do_POST = do_PATCH = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.client = notion.Client(
            'synthetic-only', f'http://127.0.0.1:{self.server.server_port}', interval=0
        )

    def valid_props(self, source, props):
        for name, value in props.items():
            if (
                name not in self.schemas[source]
                or next(iter(value)) != self.schemas[source][name]['type']
            ):
                return False
            if 'relation' in value:
                if any(v['id'] not in self.pages for v in value['relation']):
                    return False
        return True

    def close(self):
        self.server.shutdown()
        self.worker.join()
        self.server.server_close()


class SectionMarkerTests(unittest.TestCase):
    def test_section_grammar_round_trips_internal_dividers_and_fenced_headings(self):
        sections = {
            'Summary': 'Opening\n\n---\n\n# Internal title\n\n```md\n## Fenced\n---\n```',
            'Project Notes': 'First\n\n---\n\nLast\n\n---',
            'Tail': 'Ends with a divider\n\n---',
        }
        body = notion.markdown(sections)
        ranges = notion.section_ranges(body)
        self.assertEqual(set(ranges), set(sections))
        for heading, value in sections.items():
            self.assertEqual(ranges[heading], [notion.markdown({heading: value})])
        self.assertIn('### Internal title', body)
        self.assertIn('## Fenced', body)

    def test_discussion_wrappers_and_duplicate_normalisation(self):
        wrapped = '## <span discussion-urls="https://notion.so/discussion">Summary</span>'
        ranges = notion.section_ranges(wrapped + '\nOld\n\n## Summary\nNew')
        self.assertEqual(list(ranges), ['Summary'])
        self.assertEqual(len(ranges['Summary']), 2)
        self.assertTrue(ranges['Summary'][0].startswith(wrapped))
        self.assertEqual(notion.section_ranges('```\n' + wrapped + '\n```'), {})

    def test_rewrite_keeps_heading_exactly(self):
        heading = '## <span discussion-urls="url">Summary</span>  '
        class Client:
            def body(self, page):
                return heading + '\nOld'
            def update_section(self, page, old, new):
                self.changed = (old, new)
                return {}
        client = Client()
        notion.update_body(client, 'scratch', {'Summary': 'New'})
        self.assertEqual(client.changed, (heading + '\nOld', heading + '\n\nNew'))

    def test_find_created_with_commented_identity_heading(self):
        class Client:
            def paginate(self, *args):
                return [{'id': 'scratch'}]
            def body(self, page):
                return '## <span discussion-urls="url">gsd id</span>\n\nr1'
            def request(self, *args):
                return {'id': 'scratch', 'properties': {}}
        page, matched = notion.find_created(Client(), 'source', {'record_id': 'r1'}, {})
        self.assertEqual(page['id'], 'scratch')
        self.assertTrue(matched)
        client = Client()
        client.body = lambda page: '## gsd id'
        self.assertEqual(notion.find_created(client, 'source', {'record_id': 'r1'}, {}), (None, False))


class DateCanonicalTests(unittest.TestCase):
    def test_minute_precision_shared_by_encoder_and_readback(self):
        for value in ('2026-10-27 12:34:56+00', '2026-10-27T14:34:01+02:00',
                      '2026-10-27T12:34:59.123Z'):
            expected = notion.encode('date', value)
            self.assertEqual(expected, {'date': {'start': '2026-10-27T12:34Z'}})
            self.assertTrue(notion.matches(
                {'Due': {'type': 'date', 'date': {'start': '2026-10-27T12:34:00.000+00:00'}}},
                {'Due': expected}))
        self.assertEqual(notion.canonical_date('2026-10-27'), '2026-10-27')
        self.assertIsNone(notion.canonical_date(None))
        self.assertFalse(notion.matches({'Due': notion.encode('date', '2026-10-27T12:35Z')},
                                       {'Due': notion.encode('date', '2026-10-27T12:34Z')}))


class NotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'scratch.sqlite'
        self.cfg = self.root / '.config/gsd'
        self.cfg.mkdir(parents=True)
        (self.cfg / 'notion.token').write_text('synthetic-only')
        (self.cfg / 'notion.token').chmod(0o600)
        (self.cfg / 'notion-check-reviewed').touch()
        self.config_patch = patch.dict(os.environ, HOME=str(self.root), NOTION_API_TOKEN='')
        self.config_patch.start()
        with sqlite3.connect(self.path) as conn:
            db.migrate(conn)
        with db.connect(self.path, True) as conn:
            for table in notion.FIELDS:
                conn.execute('INSERT INTO notion_sources VALUES (?,?)', (table, table))
        self.fake = FakeNotion()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.fake.close)

    def add(self, kind, fields):
        with db.connect(self.path, True) as conn:
            result = write.batch(
                conn, {'changes': [{'ops': [{'upsert': kind, 'set': fields}]}]}, 'test'
            )
            self.assertTrue(result[0]['ok'], result)
            return db.rows(conn, db.KINDS[kind])[-1]['id']

    def change(self, kind, rid, fields, cite=None):
        op = {'upsert': kind, 'id': rid, 'set': fields} | ({'cite': cite} if cite else {})
        with db.connect(self.path, True) as conn:
            result = write.batch(conn, {'changes': [{'ops': [op]}]}, 'test')
            self.assertTrue(result[0]['ok'], result)

    def publish(self, check=False):
        return notion.publish(self.path, check=check, client=self.fake.client)

    def mappings(self):
        with db.connect(self.path) as conn:
            return {r['record_id']: dict(r) for r in conn.execute('SELECT * FROM notion_map')}

    def hardcut(self, plan=False):
        return notion.publish(
            self.path, client=self.fake.client, hardcut=True, plan=plan, yes=not plan
        )

    def remote_page(self, page, source='people', body='Unowned synthetic body'):
        self.fake.pages[page] = {
            'id': page,
            'parent': {'data_source_id': source},
            'properties': {'Name': notion.encode('title', page)},
        }
        self.fake.bodies[page] = body

    def remote_writes(self):
        return [c for c in self.fake.calls if c[0] != 'GET' and not c[1].endswith('/query')]

    def tombstone(self, rid):
        with db.connect(self.path, True) as conn:
            result = write.batch(
                conn, {'changes': [{'ops': [{'tombstone': rid}]}]}, 'test'
            )
            self.assertTrue(result[0]['ok'], result)

    def test_publish_archives_only_mapped_tombstones_and_is_idempotent(self):
        dead = self.add('item', {'title': 'Deleted'})
        live = self.add('item', {'title': 'Live'})
        self.publish()
        before = self.mappings()
        page = before[dead]['page_id']
        live_page = before[live]['page_id']
        live_snapshot = json.loads(json.dumps(self.fake.pages[live_page]))
        self.remote_page('owner-page', source='items')
        owner_snapshot = json.loads(json.dumps(self.fake.pages['owner-page']))
        self.tombstone(dead)
        self.fake.calls.clear()
        report = self.publish()
        self.assertEqual(report['archived'], 1)
        self.assertTrue(self.fake.pages[page]['in_trash'])
        self.assertNotIn(dead, self.mappings())
        self.assertEqual(self.mappings()[live], before[live])
        self.assertEqual(self.fake.pages[live_page], live_snapshot)
        self.assertEqual(self.fake.pages['owner-page'], owner_snapshot)
        writes = self.remote_writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:3], (
            'PATCH', '/pages/' + page, {'in_trash': True, 'notifications': notion.SILENT}
        ))
        self.assertEqual(sum(c[0] == 'GET' and c[1] == '/pages/' + page
                             for c in self.fake.calls), 2)
        self.fake.calls.clear()
        second = self.publish()
        self.assertEqual(second['archived'], 0)
        self.assertEqual(second['published'], 0)
        self.assertFalse(self.remote_writes())

    def test_tombstone_check_is_read_only(self):
        rid = self.add('item', {'title': 'Deleted'})
        self.publish()
        self.tombstone(rid)
        before = self.mappings()
        self.fake.calls.clear()
        report = self.publish(check=True)
        self.assertEqual(report['plan']['archive'], 1)
        self.assertEqual(report['archived'], 0)
        self.assertEqual(self.mappings(), before)
        self.assertFalse(self.remote_writes())

    def test_tombstone_missing_or_already_trashed_page_unmaps_without_write(self):
        for state in ('missing', 'in_trash', 'archived'):
            with self.subTest(state=state):
                rid = self.add('item', {'title': state})
                self.publish()
                page = self.mappings()[rid]['page_id']
                self.tombstone(rid)
                if state == 'missing':
                    del self.fake.pages[page]
                else:
                    self.fake.pages[page][state] = True
                self.fake.calls.clear()
                self.assertEqual(self.publish()['archived'], 1)
                self.assertNotIn(rid, self.mappings())
                self.assertFalse(self.remote_writes())

    def test_tombstone_uncertain_archive_confirms_before_unmapping(self):
        for applied in (True, False):
            with self.subTest(applied=applied):
                rid = self.add('item', {'title': 'Uncertain'})
                self.publish()
                page = self.mappings()[rid]['page_id']
                self.tombstone(rid)
                self.fake.fail = {
                    'method': 'PATCH', 'path': '/pages/' + page,
                    'status': 503, 'apply': applied,
                }
                self.fake.calls.clear()
                report = self.publish()
                self.assertEqual(report['archived'], int(applied))
                self.assertEqual(rid in self.mappings(), not applied)
                self.assertEqual(len(self.remote_writes()), 1)
                archive = next(p for p in report['pages'] if p['record_id'] == rid)
                self.assertEqual(bool(archive['errors']), not applied)
                if not applied:
                    self.assertEqual(self.publish()['archived'], 1)

    def test_tombstone_archive_error_keeps_mapping(self):
        rid = self.add('item', {'title': 'Forbidden'})
        self.publish()
        page = self.mappings()[rid]['page_id']
        self.tombstone(rid)
        self.fake.fail = {'method': 'GET', 'path': '/pages/' + page, 'status': 403}
        report = self.publish()
        self.assertEqual(report['archived'], 0)
        self.assertIn(rid, self.mappings())
        self.assertTrue(report['pages'][0]['errors'])

    def test_hardcut_rereads_schema_after_two_way_relation_delete(self):
        self.add('item', {'title': 'Item'})
        self.publish()
        self.fake.schemas['projects']['Back'] = {
            'type': 'relation', 'id': 'back-id', 'twin': ('items', 'Front')}
        self.fake.schemas['items']['Front'] = {
            'type': 'relation', 'id': 'front-id', 'twin': ('projects', 'Back')}
        (self.cfg / 'notion-check-reviewed').unlink()
        self.hardcut()
        self.assertNotIn('Back', self.fake.schemas['projects'])
        self.assertNotIn('Front', self.fake.schemas['items'])
        self.assertFalse(any(self.hardcut(plan=True)['schema'][t]['counts'][k]
                             for t in ('projects', 'items') for k in ('adds', 'deletes', 'retypes')))

    def test_hardcut_export_schema_pages_relations_and_idempotence(self):
        owner = self.add('person', {'display_name': 'Owner'})
        project = self.add('project', {'name': 'Project', 'owner_person_id': owner})
        item = self.add('item', {'title': 'Item', 'project_id': project})
        meeting = self.add('meeting', {'title': 'Meeting'})
        unmapped = self.add('person', {'display_name': 'Unmapped'})
        self.publish()
        before = self.mappings()
        gone = before[meeting]['page_id']
        del self.fake.pages[gone]
        with db.connect(self.path, True) as conn:
            conn.execute('DELETE FROM notion_map WHERE record_id=?', (unmapped,))
        duplicate = before[unmapped]['page_id']
        self.remote_page('orphan', source='items')
        owner_page = before[owner]['page_id']
        self.fake.bodies[owner_page] = '# Legacy\n\nDiscard everything, not just known sections.'
        people = self.fake.schemas['people']
        people['Legacy Title'] = people.pop('Name') | {'id': 'title'}
        for page in self.fake.pages.values():
            if page['parent']['data_source_id'] == 'people':
                page['properties']['Legacy Title'] = page['properties'].pop('Name')
        people['Status'] = {'type': 'multi_select', 'id': 'status-id'}
        del people['Other Emails']
        people['Computed'] = {'type': 'formula', 'id': 'extra-id'}
        # A correctly typed relation with the wrong destination must be repaired too.
        self.fake.schemas['projects']['Owner']['relation'] = {
            'data_source_id': 'items',
            'type': 'dual_property',
        }
        self.fake.schemas['items']['Project']['relation']['data_source_id'] = 'people'
        self.fake.query_page_size = 1
        self.fake.calls.clear()
        maps_before = self.mappings()
        plan = self.hardcut(plan=True)
        self.assertFalse(self.remote_writes())
        self.assertEqual(self.mappings(), maps_before)
        self.assertFalse((self.root / '.local/share/gsd/notion-export').exists())
        self.assertEqual(
            plan['schema']['people']['counts'],
            {'renames': 1, 'retypes': 1, 'adds': 1, 'deletes': 1},
        )
        self.assertEqual(plan['pages']['counts'], {'overwrites': 3, 'creates': 2, 'archives': 2})
        self.assertNotIn('Discard everything', json.dumps(plan))
        checks = []

        def export_exists():
            exports = list((self.root / '.local/share/gsd/notion-export').glob('*/complete.json'))
            checks.append(len(exports) == 1)
            if exports:
                checks.append((exports[0].parent / 'notion_map.json').is_file())

        self.fake.before_write = export_exists
        schema_before = json.loads(json.dumps(people))
        (self.cfg / 'notion-check-reviewed').unlink()
        report = self.hardcut()
        self.assertTrue(checks and all(checks))
        self.assertEqual(report['pages'], plan['pages'])
        self.assertEqual(report['schema'], plan['schema'])
        export = Path(report['export_path'])
        self.assertEqual(
            json.loads((export / 'notion_map.json').read_text()), list(maps_before.values())
        )
        self.assertEqual(
            json.loads((export / 'people/schema.json').read_text())['properties'], schema_before
        )
        bodies = [p.read_text() for p in export.glob('people/*.md')]
        self.assertIn('# Legacy\n\nDiscard everything, not just known sections.', bodies)
        for p in [export, *export.rglob('*')]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600)
        for table in notion.FIELDS:
            self.assertEqual(
                {k: v['type'] for k, v in self.fake.schemas[table].items()},
                {k: v['type'] for k, v in notion.expected_schema(table).items()},
            )
        schema_writes = [c for c in self.remote_writes() if c[1].startswith('/data_sources/')]
        self.assertEqual(len(schema_writes), 3)
        self.assertEqual(len({c[1] for c in schema_writes}), 3)
        maps = self.mappings()
        self.assertNotEqual(maps[meeting]['page_id'], gone)
        self.assertNotEqual(maps[unmapped]['page_id'], duplicate)
        self.assertTrue(self.fake.pages[duplicate]['in_trash'])
        self.assertTrue(self.fake.pages['orphan']['in_trash'])
        with db.connect(self.path) as conn:
            for row in notion.render(conn):
                self.assertEqual(self.fake.bodies[maps[row['record_id']]['page_id']], row['body'])
        props = self.fake.pages[maps[project]['page_id']]['properties']
        self.assertEqual(props['Owner'], {'relation': [{'id': maps[owner]['page_id']}]})
        props = self.fake.pages[maps[item]['page_id']]['properties']
        self.assertEqual(props['Project'], {'relation': [{'id': maps[project]['page_id']}]})
        self.assertEqual(
            self.fake.schemas['projects']['Owner']['relation']['data_source_id'], 'people'
        )
        self.assertIn('D16', (self.cfg / 'notion-check-reviewed').read_text())
        creates = [i for i, c in enumerate(self.fake.calls) if c[:2] == ('POST', '/pages')]
        relations = [
            i
            for i, c in enumerate(self.fake.calls)
            if c[0] == 'PATCH'
            and any('relation' in p for p in (c[2].get('properties') or {}).values() if p)
        ]
        # Schema relation patches precede creates; page relation patches must follow.
        relations = [i for i in relations if self.fake.calls[i][1].startswith('/pages/')]
        self.assertLess(max(creates), min(relations))
        self.fake.calls.clear()
        self.fake.before_write = None
        again = self.hardcut(plan=True)
        self.assertEqual(again['pages']['counts'], {'overwrites': 0, 'creates': 0, 'archives': 0})
        self.assertTrue(all(not any(s['counts'].values()) for s in again['schema'].values()))
        self.assertFalse(self.remote_writes())
        self.assertEqual(self.publish()['published'], 0)
        self.assertFalse(self.remote_writes())

    def test_hardcut_incomplete_export_and_disk_failure_never_write(self):
        rid = self.add('person', {'display_name': 'Before'})
        self.publish()
        page = self.mappings()[rid]['page_id']
        self.change('person', rid, {'summary': 'Changed'})
        before = self.mappings()
        self.fake.calls.clear()
        self.fake.incomplete_bodies.add(page)
        with self.assertRaisesRegex(ValueError, 'markdown incomplete'):
            self.hardcut()
        self.assertFalse(self.remote_writes())
        self.fake.incomplete_bodies.clear()
        with (
            patch('os.fsync', side_effect=OSError('synthetic disk failure')),
            self.assertRaisesRegex(OSError, 'synthetic disk failure'),
        ):
            self.hardcut()
        self.assertFalse(self.remote_writes())
        self.assertEqual(self.mappings(), before)

    def test_hardcut_export_paginates_property_items(self):
        self.remote_page('legacy')
        self.fake.pages['legacy']['properties']['Long'] = {
            'id': 'long-id',
            'type': 'rich_text',
            'rich_text': notion.rich('Truncated'),
        }
        self.fake.schemas['people']['Long'] = {'id': 'long-id', 'type': 'rich_text'}
        self.fake.property_items['legacy', 'long-id'] = [
            {'type': 'rich_text', 'rich_text': notion.rich(text)[0]}
            for text in ('One', 'Two', 'Three')
        ]
        self.fake.query_page_size = 1
        report = self.hardcut()
        saved = json.loads(next(Path(report['export_path']).glob('people/0*.json')).read_text())
        self.assertEqual(len(saved['property_items']['Long']), 3)
        self.assertEqual(
            notion.canonical_property(saved['page']['properties']['Long']), 'OneTwoThree'
        )
        self.assertTrue(self.fake.pages['legacy']['in_trash'])

    def test_hardcut_gate_yes_token_review_bypass_and_lock(self):
        import fcntl

        (self.cfg / 'notion-check-reviewed').unlink()
        with self.assertRaisesRegex(ValueError, 'explicit --yes'):
            notion.publish(self.path, hardcut=True, client=self.fake.client)
        for flag in ('plan', 'yes'):
            with self.assertRaisesRegex(ValueError, 'require --hardcut'):
                notion.publish(self.path, **{flag: True}, client=self.fake.client)
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            notion.publish(self.path, hardcut=True, plan=True, check=True, client=self.fake.client)
        self.assertEqual(self.hardcut(plan=True)['mode'], 'plan')
        with Path(str(self.path) + '.notion.lock').open('a') as lock, patch.object(notion, 'LOCK_WAIT', 0):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for plan in (False, True):
                with self.assertRaisesRegex(ValueError, 'another Notion publish'):
                    self.hardcut(plan=plan)
        (self.cfg / 'notion.token').unlink()
        with self.assertRaisesRegex(ValueError, 'token file'):
            self.hardcut()
        with self.assertRaisesRegex(ValueError, 'requires a token'):
            notion.publish(self.path, hardcut=True, plan=True)
        self.fake.calls.clear()
        result = subprocess.run(
            [sys.executable, '-m', 'gsd.cli', 'publish', 'notion', '--hardcut'],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--yes', result.stdout + result.stderr)
        self.assertFalse(self.fake.calls)

    def test_hardcut_schema_uncertain_write_readback(self):
        self.add('person', {'display_name': 'Person'})
        self.fake.schemas['people']['Extra'] = {'type': 'number'}
        self.fake.fail = {'method': 'PATCH', 'path': '/data_sources/people', 'status': 503}
        self.hardcut()
        self.assertNotIn('Extra', self.fake.schemas['people'])
        self.fake.schemas['people']['Extra'] = {'type': 'number'}
        self.fake.fail = {
            'method': 'PATCH',
            'path': '/data_sources/people',
            'status': 503,
            'apply': False,
        }
        self.fake.calls.clear()
        with self.assertRaisesRegex(ValueError, 'schema write; read-back did not confirm'):
            self.hardcut()
        self.assertEqual(
            sum(c[:2] == ('PATCH', '/data_sources/people') for c in self.fake.calls), 1
        )

    def test_hardcut_body_and_archive_uncertain_readback(self):
        rid = self.add('person', {'display_name': 'Person'})
        self.hardcut()
        page = self.mappings()[rid]['page_id']
        for applied in (False, True):
            self.fake.bodies[page] = 'Legacy'
            self.fake.fail = {
                'method': 'PATCH',
                'path': f'/pages/{page}/markdown',
                'status': 503,
                'apply': applied,
            }
            if applied:
                self.hardcut()
            else:
                with self.assertRaisesRegex(
                    ValueError, 'body replacement; read-back did not confirm'
                ):
                    self.hardcut()
                self.assertIsNone(self.mappings()[rid]['content_hash'])
        for applied in (False, True):
            self.remote_page('orphan')
            self.fake.fail = {
                'method': 'PATCH',
                'path': '/pages/orphan',
                'status': 503,
                'apply': applied,
            }
            if applied:
                self.hardcut()
                self.assertTrue(self.fake.pages['orphan']['in_trash'])
            else:
                with self.assertRaisesRegex(ValueError, 'archive; read-back did not confirm'):
                    self.hardcut()

    def test_hardcut_duplicate_trashed_and_wrong_source_mappings(self):
        first = self.add('person', {'display_name': 'First'})
        second = self.add('person', {'display_name': 'Second'})
        trashed = self.add('person', {'display_name': 'Trashed'})
        misplaced = self.add('person', {'display_name': 'Misplaced'})
        self.hardcut()
        before = self.mappings()
        with db.connect(self.path, True) as conn:
            conn.execute(
                'UPDATE notion_map SET page_id=? WHERE record_id=?',
                (before[first]['page_id'], second),
            )
        self.fake.pages[before[trashed]['page_id']]['in_trash'] = True
        self.fake.pages[before[misplaced]['page_id']]['parent']['data_source_id'] = 'projects'
        report = self.hardcut()
        self.assertEqual(report['pages']['counts']['creates'], 3)
        maps = self.mappings()
        self.assertEqual(len({m['page_id'] for m in maps.values()}), 4)
        self.assertTrue(self.fake.pages[before[second]['page_id']]['in_trash'])
        self.assertTrue(self.fake.pages[before[misplaced]['page_id']]['in_trash'])
        self.assertNotEqual(maps[trashed]['page_id'], before[trashed]['page_id'])
        self.assertFalse(any(self.hardcut(plan=True)['pages']['counts'].values()))

    def test_hardcut_title_collision_preserves_real_title(self):
        schema = self.fake.schemas['people']
        schema['Legacy'] = schema.pop('Name') | {'id': 'title'}
        schema['Name'] = {'id': 'not-title', 'type': 'rich_text'}
        plan = self.hardcut(plan=True)
        self.assertEqual(plan['schema']['people']['counts']['renames'], 1)
        self.assertEqual(plan['schema']['people']['counts']['deletes'], 1)
        self.hardcut()
        self.assertEqual(schema['Name']['type'], 'title')
        patch = next(
            c[2]['properties'] for c in self.remote_writes() if c[1] == '/data_sources/people'
        )
        self.assertEqual(patch['title'], {'name': 'Name'})
        self.assertIsNone(patch['not-title'])

    def test_hardcut_incomplete_pagination_never_writes(self):
        self.remote_page('one')
        self.remote_page('two')
        self.fake.query_page_size = 1
        self.fake.incomplete_pagination = True
        with self.assertRaisesRegex(ValueError, 'pagination incomplete'):
            self.hardcut()
        self.assertFalse(self.remote_writes())
        self.assertFalse((self.root / '.local/share/gsd/notion-export').exists())

    def test_hardcut_cli_subprocess_plan_cut_and_ordinary_publish(self):
        self.add('project', {'name': 'Synthetic CLI project'})
        (self.cfg / 'notion-check-reviewed').unlink()
        # Only supply the local transport base; the CLI, publisher, HTTP client
        # methods, SQLite and export filesystem all run for real in the child.
        script = f'''
from gsd import cli, notion
class LocalClient(notion.Client):
    def __init__(self, secret):
        super().__init__(secret, base={self.fake.client.base!r}, interval=0)
notion.Client = LocalClient
raise SystemExit(cli.main())
'''

        def command(*flags):
            result = subprocess.run(
                [sys.executable, '-c', script, 'publish', 'notion', '--db', str(self.path), *flags],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        plan = command('--hardcut', '--plan')
        self.assertEqual(plan['pages']['counts']['creates'], 1)
        self.assertFalse(self.remote_writes())
        report = command('--hardcut', '--yes')
        self.assertEqual(report['mode'], 'done')
        self.assertTrue(Path(report['export_path']).is_dir())
        self.assertEqual(plan['pages'], report['pages'])
        self.fake.calls.clear()
        self.assertFalse(any(command('--hardcut', '--plan')['pages']['counts'].values()))
        self.assertEqual(command()['published'], 0)
        self.assertFalse(self.remote_writes())

    def test_hardcut_stale_mapping_does_not_shield_page(self):
        self.remote_page('stale')
        with db.connect(self.path, True) as conn:
            conn.execute(
                'INSERT INTO notion_map VALUES (?,?,?,?,?)',
                ('deleted-record', 'stale', None, None, None),
            )
        self.hardcut()
        self.assertFalse(self.mappings())
        self.assertTrue(self.fake.pages['stale']['in_trash'])

    def test_two_passes_live_types_hashes_and_noop(self):
        owner = self.add(
            'person', {'display_name': 'Example', 'primary_email': 'example@example.org'}
        )
        project = self.add(
            'project',
            {
                'name': 'Synthetic project',
                'status': 'active',
                'owner_person_id': owner,
                'priority': 30,
                'next_action': 'Do it',
            },
        )
        item = self.add(
            'item',
            {
                'title': 'Example task',
                'project_id': project,
                'due_at': '2026-10-20',
            },
        )
        self.fake.schemas['projects']['Status'] = {'type': 'status'}
        report = self.publish()
        self.assertEqual(report['published'], 3)
        maps = self.mappings()
        props = self.fake.pages[maps[project]['page_id']]['properties']
        self.assertEqual(props['Priority'], {'number': 30})
        self.assertEqual(props['Status'], {'status': {'name': 'active'}})
        self.assertEqual(props['Owner'], {'relation': [{'id': maps[owner]['page_id']}]})
        props = self.fake.pages[maps[item]['page_id']]['properties']
        self.assertEqual(props['Due'], {'date': {'start': '2026-10-20'}})
        self.assertEqual(props['Record Status'], {'select': {'name': 'open'}})
        self.assertNotIn('Thread URL', props)
        creates = [i for i, c in enumerate(self.fake.calls) if c[:2] == ('POST', '/pages')]
        relations = [
            i
            for i, c in enumerate(self.fake.calls)
            if c[0] == 'PATCH'
            and c[2].get('properties')
            and any('relation' in v for v in c[2]['properties'].values())
        ]
        self.assertLess(max(creates), min(relations))
        for _, _, data, _ in (self.fake.calls[i] for i in creates):
            self.assertFalse(any('relation' in v for v in data['properties'].values()))
            self.assertEqual(data['notifications'], {'mode': 'silent'})
        self.assertTrue(all(m['content_hash'] for m in maps.values()))
        self.fake.calls.clear()
        self.assertEqual(self.publish()['published'], 0)
        self.assertTrue(all(c[0] == 'GET' for c in self.fake.calls))
        self.assertEqual(set(self.fake.versions), {notion.VERSION})

    def test_live_multi_select_and_relation_type_safety(self):
        # Only aliases that are email addresses reach Other Emails; nicknames stay in gsd.
        rid = self.add(
            'person',
            {
                'display_name': 'Example',
                'aliases': '["one@example.org","nickname","123@s.whatsapp.net","456@g.us","two@example.org"]',
            },
        )
        self.fake.schemas['people']['Other Emails'] = {'type': 'multi_select'}
        self.assertEqual(self.publish()['published'], 1)
        props = self.fake.pages[self.mappings()[rid]['page_id']]['properties']
        self.assertEqual(
            props['Other Emails'],
            {'multi_select': [{'name': 'one@example.org'}, {'name': 'two@example.org'}]},
        )
        self.fake.schemas['people']['Status'] = {'type': 'people'}
        self.assertIn('refused', self.publish())

    def test_record_status_derived_from_state(self):
        open_item = self.add('item', {'title': 'Open task'})
        done_item = self.add('item', {'title': 'Done task'})
        canceled_item = self.add('item', {'title': 'Canceled task'})
        self.change('item', done_item, {'state': 'done'}, cite=DECIDES)
        self.change('item', canceled_item, {'state': 'canceled'}, cite=DECIDES)
        self.assertEqual(self.publish()['published'], 3)
        maps = self.mappings()
        status = {
            rid: self.fake.pages[maps[rid]['page_id']]['properties']['Record Status']
            for rid in (open_item, done_item, canceled_item)
        }
        self.assertEqual(
            status,
            {
                open_item: {'select': {'name': 'open'}},
                done_item: {'select': {'name': 'done'}},
                canceled_item: {'select': {'name': 'cancelled'}},
            },
        )

    def test_aliases_visible_in_full_and_sparse_views(self):
        self.add(
            'person', {'display_name': 'Example', 'aliases': '["example.nick","a@example.org"]'}
        )
        with db.connect(self.path) as conn:
            full = read.view(conn)
            sparse = read.view(conn, sparse=True)
        for text in (full, sparse):
            self.assertIn('aliases: ', text)
            self.assertIn('example.nick', text)
            self.assertIn('a@example.org', text)

    def test_retired_columns_absent_from_views_and_notion(self):
        notion_fields = {field for rows in notion.FIELDS.values() for field, _, _ in rows}
        view_fields = set(
            ' '.join([*read.VIEW_FIELDS.values(), *read.SPARSE_FIELDS.values()]).split()
        )
        self.assertFalse(RETIRED & notion_fields)
        self.assertFalse(({'record_status', 'other_emails'} | RETIRED) & view_fields)

    def test_pacing_and_exclusive_publish_lock(self):
        import fcntl

        client = notion.Client('synthetic-only', self.fake.client.base)
        client.request('GET', '/data_sources/projects')
        client.request('GET', '/data_sources/projects')
        self.assertGreaterEqual(self.fake.calls[-1][3] - self.fake.calls[-2][3], 0.32)
        with Path(str(self.path) + '.notion.lock').open('a') as lock, patch.object(notion, 'LOCK_WAIT', 0):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'another Notion publish'):
                self.publish()

    def test_encoder_table(self):
        cases = [
            ('title', 'Name'),
            ('rich_text', 'Long'),
            ('select', 'Choice'),
            ('status', 'State'),
            ('multi_select', ['a', 'b']),
            ('date', '2026-10-20'),
            ('number', 0),
            ('url', 'https://example.org'),
            ('email', 'test@example.org'),
            ('checkbox', True),
            ('relation', []),
        ]
        props = {str(i): notion.encode(typ, value) for i, (typ, value) in enumerate(cases)}
        self.assertEqual(
            list(props.values()),
            [
                {'title': [{'type': 'text', 'text': {'content': 'Name'}}]},
                {'rich_text': [{'type': 'text', 'text': {'content': 'Long'}}]},
                {'select': {'name': 'Choice'}},
                {'status': {'name': 'State'}},
                {'multi_select': [{'name': 'a'}, {'name': 'b'}]},
                {'date': {'start': '2026-10-20'}},
                {'number': 0},
                {'url': 'https://example.org'},
                {'email': 'test@example.org'},
                {'checkbox': True},
                {'relation': []},
            ],
        )
        for typ in ('select', 'status', 'date', 'number', 'url', 'email'):
            self.assertIsNone(notion.encode(typ, None)[typ])
        self.assertEqual(len(notion.rich('x' * 4001)), 3)
        with self.assertRaises(ValueError):
            notion.encode('people', [])

    def test_schema_missing_mistyped_reported_no_writes(self):
        self.add('project', {'name': 'Example'})
        del self.fake.schemas['projects']['Next Actions']
        self.fake.schemas['projects']['Priority'] = {'type': 'rich_text'}
        report = self.publish(check=True)
        errors = report['schema_differences']['projects']['missing_or_wrong_type']
        self.assertEqual({e['property'] for e in errors}, {'Next Actions', 'Priority'})
        self.assertIn('refused', self.publish())
        self.assertFalse(self.remote_writes())

    def test_check_without_token_expected_table_and_no_mutation(self):
        self.add('project', {'name': 'Example'})
        (self.cfg / 'notion.token').unlink()
        with patch.dict('os.environ', {'HOME': str(self.root)}, clear=True):
            report = notion.publish(self.path, check=True)
        self.assertIn('expected schema from FIELDS', report['schema_basis'])
        self.assertFalse(self.mappings())
        for table, fields in notion.FIELDS.items():
            self.assertEqual(len(fields), len({f for f, _, _ in fields}), table)

    def test_check_reads_values_duplicates_and_source_inventory(self):
        rid = self.add('item', {'title': 'Item', 'due_at': '2026-10-27 12:34:56+00'})
        wrong = self.add('project', {'name': 'Wrong source'})
        trashed = self.add('person', {'display_name': 'Trash'})
        gone = self.add('meeting', {'title': 'Gone'})
        self.publish()
        maps = self.mappings()
        page = maps[rid]['page_id']
        self.fake.pages[page]['properties']['Due'] = {
            'type': 'date', 'date': {'start': '2026-10-27T12:34:00.000+00:00'}}
        self.fake.pages[page]['properties']['Name'] = notion.encode('title', 'Remote edit')
        self.fake.bodies[page] += '\n\n## <span discussion-urls="url">Summary</span>\nDuplicate'
        self.fake.pages[maps[wrong]['page_id']]['parent']['data_source_id'] = 'items'
        self.fake.pages[maps[trashed]['page_id']]['in_trash'] = True
        del self.fake.pages[maps[gone]['page_id']]
        self.remote_page('orphan', source='people')
        self.fake.calls.clear()
        report = self.publish(check=True)
        self.assertEqual(report['property_differences'], [
            {'record_id': rid, 'page_id': page, 'properties': ['Name'], 'errors': []}])
        self.assertEqual(report['duplicate_sections'], [{'page_id': page, 'sections': ['Summary']}])
        for name, expected in [('unmapped_pages', 'orphan'), ('missing_pages', maps[gone]['page_id']),
                               ('wrong_source_pages', maps[wrong]['page_id']),
                               ('trashed_pages', maps[trashed]['page_id'])]:
            self.assertEqual(report[name][0]['page_id'], expected)
            self.assertEqual(report['projection_check']['classes'][name]['count'], 1)
        self.assertEqual(sum(c[0] == 'POST' and c[1].endswith('/query') for c in self.fake.calls), 4)
        self.assertFalse(self.remote_writes())
        checks, metrics = notion.projection_telemetry(self.path)
        self.assertFalse(next(c for c in checks if c['name'] == 'notion_duplicate_sections')['ok'])
        self.assertTrue(all(len(c['reason']) <= 160 for c in checks))
        self.assertEqual(len(checks), len(metrics))
        ledger = (self.path.parent / 'notion-publish.jsonl').read_text().splitlines()[-1]
        self.assertNotIn('Remote edit', ledger)
        self.assertIn('projection_check', ledger)

    def test_check_unknown_without_token_or_failed_read(self):
        checks, metrics = notion.projection_telemetry(self.path)
        self.assertFalse(checks[0]['ok'])
        self.assertEqual(metrics, [])
        self.add('item', {'title': 'Item'})
        self.publish()
        self.publish(check=True)
        self.fake.fail = {'method': 'GET', 'path': '/data_sources/items', 'status': 403}
        # Schema reads precede reconcile: failures there must not retain a green check.
        with self.assertRaises(ValueError):
            self.publish(check=True)
        checks, _ = notion.projection_telemetry(self.path)
        self.assertFalse(checks[0]['ok'])

    def test_check_missing_data_source_is_not_healthy(self):
        with db.connect(self.path, True) as conn:
            conn.execute("DELETE FROM notion_sources WHERE kind='people'")
        report = self.publish(check=True)
        self.assertEqual(report['source_differences'], [{'page_id': 'people'}])
        checks, _ = notion.projection_telemetry(self.path)
        self.assertFalse(next(c for c in checks if c['name'] == 'notion_source_differences')['ok'])

    def test_check_lists_unreachable_mapped_pages(self):
        kept = self.add('person', {'display_name': 'Kept', 'primary_email': 'kept@example.org'})
        gone = self.add('person', {'display_name': 'Gone', 'primary_email': 'gone@example.org'})
        self.publish()
        page = self.mappings()[gone]['page_id']
        del self.fake.pages[page]
        report = self.publish(check=True)
        self.assertEqual([p['record_id'] for p in report['unreachable_pages']], [gone])
        self.assertNotIn(kept, [p['record_id'] for p in report['unreachable_pages']])

    def test_429_retry_after(self):
        self.fake.fail = {'method': 'POST', 'path': '/pages', 'status': 429, 'delay': 0.06}
        self.add('project', {'name': 'Example'})
        self.assertEqual(self.publish()['published'], 1)
        creates = [c for c in self.fake.calls if c[:2] == ('POST', '/pages')]
        self.assertEqual(len(creates), 2)
        self.assertGreaterEqual(creates[1][3] - creates[0][3], 0.055)
        self.assertEqual(len(self.fake.pages), 1)

    def test_uncertain_create_5xx_and_disconnect_readback_no_duplicate(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                self.add('project', {'name': 'Example ' + str(disconnect)})
                self.fake.fail = {
                    'method': 'POST',
                    'path': '/pages',
                    'status': 503,
                    'disconnect': disconnect,
                }
                before = len(self.fake.pages)
                start = len(self.fake.calls)
                self.publish()
                calls = self.fake.calls[start:]
                self.assertEqual(len(self.fake.pages), before + 1)
                self.assertEqual(sum(c[:2] == ('POST', '/pages') for c in calls), 1)
                create = next(i for i, c in enumerate(calls) if c[:2] == ('POST', '/pages'))
                self.assertTrue(
                    any(
                        c[:2] == ('POST', '/data_sources/projects/query')
                        for c in calls[create + 1 :]
                    )
                )

    def test_query_pagination_recovers_existing_create(self):
        for n in range(3):
            rid = self.add('project', {'name': 'Example ' + str(n)})
        self.publish()
        with db.connect(self.path, True) as conn:
            conn.execute('DELETE FROM notion_map WHERE record_id=?', (rid,))
        self.fake.query_page_size = 1
        self.fake.calls.clear()
        self.assertEqual(self.publish()['published'], 1)
        self.assertEqual(len(self.fake.pages), 3)
        self.assertIn(rid, self.mappings())
        self.assertFalse(any(c[:2] == ('POST', '/pages') for c in self.fake.calls))
        queries = [c for c in self.fake.calls if c[:2] == ('POST', '/data_sources/projects/query')]
        self.assertEqual(len(queries), 3)
        self.assertEqual([c[2].get('start_cursor') for c in queries], [None, '1', '2'])

    def test_legacy_no_markers_appends_without_replacing_body(self):
        rid = self.add('project', {'name': 'Example'})
        self.publish()
        page = self.mappings()[rid]['page_id']
        legacy = '# Legacy page\n\nUntouched original blocks, with comments.'
        self.fake.bodies[page] = legacy
        self.change('project', rid, {'summary': 'New summary'})
        self.fake.calls.clear()
        report = self.publish()
        self.assertEqual(report['appended_sections'][0]['page_id'], page)
        self.assertTrue(self.fake.bodies[page].startswith(legacy))
        writes = [
            c[2]['type'] for c in self.fake.calls if c[0] == 'PATCH' and c[1].endswith('/markdown')
        ]
        self.assertEqual(writes, ['insert_content'])

    def test_section_updates_append_preserve_unrelated_and_comments(self):
        rid = self.add(
            'project',
            {'name': 'Example', 'summary': 'Before', 'body_markdown': '# Nested heading\nNotes'},
        )
        self.publish()
        page = self.mappings()[rid]['page_id']
        unrelated = '\n\n---\n\n## Unrelated\n\nOwner block with inline comment.'
        self.fake.bodies[page] += unrelated
        self.fake.children[page] = [{'id': 'outside', 'has_children': False}]
        before_comments = dict(self.fake.comments)
        self.change('project', rid, {'summary': 'After'})
        self.fake.calls.clear()
        self.fake.fail = {'method': 'PATCH', 'path': '/pages/' + page + '/markdown', 'status': 503}
        self.publish()
        updates = [c[2] for c in self.fake.calls if c[0] == 'PATCH' and c[1].endswith('/markdown')]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['type'], 'update_content')
        self.assertEqual(
            updates[0]['update_content']['content_updates'][0]['old_str'], '## Summary\n\nBefore'
        )
        self.assertTrue(self.fake.bodies[page].endswith(unrelated))
        self.assertEqual(self.fake.comments, before_comments)
        self.assertIn('### Nested heading', self.fake.bodies[page])
        # D34: a section is rewritten from gsd's render even when text below an
        # interior divider is not gsd's; a lost marker is appended again.
        self.fake.bodies[page] = self.fake.bodies[page].replace(
            '## Sources\n\n_(empty)_', 'Legacy source block'
        )
        self.change('project', rid, {'summary': 'Third'})
        report = self.publish()
        self.assertEqual(report['pages'][0]['errors'], [])
        self.assertNotIn('Legacy source block', self.fake.bodies[page])
        self.assertEqual(len(notion.section_ranges(self.fake.bodies[page])['Sources']), 1)
        self.assertIsNotNone(self.mappings()[rid]['content_hash'])
        self.assertIn(unrelated, self.fake.bodies[page])
        self.assertTrue(notion.probe_comments(self.fake.client, page)['survived'])

    def test_unconfirmed_section_no_retry_or_hash_advance(self):
        rid = self.add('project', {'name': 'Example', 'summary': 'Before'})
        self.publish()
        self.change('project', rid, {'summary': 'After'})
        page = self.mappings()[rid]['page_id']
        self.fake.calls.clear()
        self.fake.fail = {
            'method': 'PATCH',
            'path': '/pages/' + page + '/markdown',
            'status': 503,
            'apply': False,
        }
        with self.assertRaisesRegex(ValueError, 'read-back did not confirm'):
            self.publish()
        self.assertIsNone(self.mappings()[rid]['content_hash'])
        self.assertEqual(
            sum(c[0] == 'PATCH' and c[1].endswith('/markdown') for c in self.fake.calls), 1
        )

    def test_notes_update_preserves_separate_commented_heading(self):
        rid = self.add('project', {'name': 'Boundary', 'body_markdown': 'Before'})
        self.publish()
        page = self.mappings()[rid]['page_id']
        unrelated = '# <span discussion-urls="synthetic-discussion">Journal</span>\n\nKeep marker'
        self.fake.bodies[page] = self.fake.bodies[page].replace(
            '## Project Notes\n\nBefore', '## Project Notes\n\nBefore\n\n---\n\n' + unrelated)
        self.change('project', rid, {'body_markdown': 'After'})
        self.publish()
        self.assertIn(unrelated, self.fake.bodies[page])

    def test_summary_internal_divider_round_trips_without_duplicate_tail(self):
        text = 'Opening\n\n---\n\nUnique ending'
        rid = self.add('project', {'name': 'Round trip', 'summary': text, 'body_markdown': text})
        self.publish()
        page = self.mappings()[rid]['page_id']
        self.assertEqual(self.fake.bodies[page].count('Unique ending'), 2)
        self.change('project', rid, {'summary': text + ' updated'})
        report = self.publish()
        self.assertEqual(report['pages'][0]['errors'], [])
        self.assertEqual(self.fake.bodies[page].count('Unique ending updated'), 1)
        self.assertEqual(self.fake.bodies[page].count('Unique ending'), 2)

    def test_date_readback_compares_range_end_and_timezone(self):
        expected = {'Due': notion.encode('date', '2026-10-28')}
        for end, zone in [('2026-11-01', None), (None, 'Europe/Athens')]:
            actual = {'Due': {'date': {'start': '2026-10-28', 'end': end, 'time_zone': zone}}}
            self.assertFalse(notion.matches(actual, expected))
        self.assertTrue(notion.matches(
            {'Due': {'date': {'start': '2026-10-28', 'end': None, 'time_zone': None}}}, expected))

    def test_wrong_relation_target_refuses_all_page_writes(self):
        self.add('item', {'title': 'Example'})
        self.fake.schemas['items']['Project']['relation'] = {'data_source_id': 'people'}
        _, differences = notion.publish_schemas(self.fake.client, {t: t for t in notion.FIELDS})
        self.assertTrue(differences['items']['missing_or_wrong_type'])
        report = self.publish()
        self.assertIn('refused', report)
        self.assertEqual(self.remote_writes(), [])

    def test_expired_projection_check_is_unknown(self):
        self.publish(check=True)
        log = self.path.parent / 'notion-publish.jsonl'
        entry = json.loads(log.read_text().splitlines()[-1])
        entry['timestamp'] = '2000-01-01T00:00:00Z'
        log.write_text(json.dumps(entry) + '\n')
        checks, metrics = notion.projection_telemetry(self.path)
        self.assertFalse(checks[0]['ok'])
        self.assertIn('unknown:', checks[0]['reason'])
        self.assertEqual(metrics, [])

    def test_check_reports_never_published_record(self):
        rid = self.add('project', {'name': 'Not yet published'})
        report = self.publish(check=True)
        self.assertEqual(report['plan']['project:create'], 1)
        checks, _ = notion.projection_telemetry(self.path)
        self.assertTrue(any(not row['ok'] for row in checks))
        self.assertEqual(report['missing_pages'][0]['record_id'], rid)

    def test_check_reports_pending_tombstone_archive(self):
        rid = self.add('project', {'name': 'Pending deletion'})
        self.publish()
        self.tombstone(rid)
        report = self.publish(check=True)
        self.assertEqual(report['plan']['archive'], 1)
        checks, _ = notion.projection_telemetry(self.path)
        self.assertTrue(any(not row['ok'] for row in checks))
        self.assertEqual(report['pending_archives'][0]['record_id'], rid)
        self.publish()
        report = self.publish(check=True)
        self.assertEqual(report['pending_archives'], [])
        self.assertTrue(all(row['ok'] for row in notion.projection_telemetry(self.path)[0]))

    def failed_ordinary_update(self):
        rid = self.add('project', {'name': 'Partial', 'priority': 1, 'summary': 'A'})
        self.publish()
        page = self.mappings()[rid]['page_id']
        self.change('project', rid, {'priority': 2, 'summary': 'B'})
        self.fake.incomplete_bodies.add(page)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.publish()
        self.fake.incomplete_bodies.clear()
        return rid, page

    def test_partial_update_invalidates_completed_hash(self):
        rid, _ = self.failed_ordinary_update()
        self.assertIsNone(self.mappings()[rid]['content_hash'])
        self.assertIsNone(self.mappings()[rid]['body_hash'])

    def test_graph_reversion_repairs_partially_updated_remote(self):
        rid, page = self.failed_ordinary_update()
        self.change('project', rid, {'priority': 1, 'summary': 'A'})
        self.publish()
        self.assertEqual(self.fake.pages[page]['properties']['Priority']['number'], 1)

    def test_partial_pass_two_keeps_confirmed_identity_null_hash(self):
        owner = self.add('person', {'display_name': 'Owner'})
        rid = self.add('project', {'name': 'Example', 'owner_person_id': owner})
        # First PATCH is non-relations, second is pass 2. Failed write does not
        # land, and its GET read-back must prevent hash advancement.
        self.fake.fail = {
            'method': 'PATCH',
            'path': '/pages/page-2',
            'status': 503,
            'apply': False,
            'skip': 1,
        }
        with self.assertRaisesRegex(ValueError, 'read-back did not confirm'):
            self.publish()
        self.assertTrue(self.mappings()[owner]['content_hash'])
        self.assertIsNone(self.mappings()[rid]['content_hash'])
        self.fake.calls.clear()
        self.assertEqual(self.publish()['published'], 1)
        self.assertFalse(any(c[:2] == ('POST', '/pages') for c in self.fake.calls))
        self.assertTrue(self.mappings()[rid]['content_hash'])

    def test_unconfirmed_create_stops_after_readback_with_pending_identity(self):
        rid = self.add('project', {'name': 'Example'})
        self.fake.fail = {'method': 'POST', 'path': '/pages', 'status': 503, 'apply': False}
        with self.assertRaisesRegex(ValueError, 'no create retry attempted'):
            self.publish()
        self.assertIsNone(self.mappings()[rid]['page_id'])
        self.assertIsNone(self.mappings()[rid]['content_hash'])
        self.assertEqual(sum(c[:2] == ('POST', '/pages') for c in self.fake.calls), 1)
        self.assertTrue(
            any(c[:2] == ('POST', '/data_sources/projects/query') for c in self.fake.calls)
        )

    def test_property_5xx_applied_readback(self):
        rid = self.add('project', {'name': 'Example'})
        self.fake.fail = {'method': 'PATCH', 'path': '/pages/page-1', 'status': 503}
        self.assertEqual(self.publish()['published'], 1)
        self.assertTrue(self.mappings()[rid]['content_hash'])
        prop_write = next(
            i for i, c in enumerate(self.fake.calls) if c[:2] == ('PATCH', '/pages/page-1')
        )
        self.assertEqual(self.fake.calls[prop_write + 1][:2], ('GET', '/pages/page-1'))

    def test_probe_requires_inline_not_only_page_comments(self):
        self.add('project', {'name': 'Example'})
        self.publish()
        self.fake.comments['page-1'] = [
            {'id': 'page-comment', 'rich_text': notion.rich('Page comment')}
        ]
        report = notion.probe_comments(self.fake.client, 'page-1')
        self.assertEqual(report['comments_before'], 1)
        self.assertIsNone(report['survived'])

    def test_gate_both_files_and_modes_cli(self):
        (self.cfg / 'notion-check-reviewed').unlink()
        with self.assertRaisesRegex(ValueError, 'owner review flag'):
            self.publish()
        (self.cfg / 'notion.token').unlink()
        with self.assertRaisesRegex(ValueError, 'token file'):
            self.publish()
        # CLI must refuse before opening the default/live graph.
        env = dict(os.environ, HOME=str(self.root))
        result = subprocess.run(
            [str(db.ROOT / 'bin/gsd'), 'publish', 'notion'], env=env, text=True, capture_output=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('token file', result.stdout + result.stderr)
        result = subprocess.run(
            [str(db.ROOT / 'bin/gsd'), 'publish', 'notion', '--help'],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertIn('--probe-comments', result.stdout)
        self.assertFalse(self.fake.calls)
        (self.cfg / 'notion-check-reviewed').touch()
        (self.cfg / 'notion.token').write_text('synthetic-only')
        (self.cfg / 'notion.token').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'mode 600'):
            self.publish()

    def test_probe_detects_loss_and_uncertain_prefix_edit(self):
        rid = self.add('project', {'name': 'Example', 'summary': 'Before'})
        self.publish()
        page = self.mappings()[rid]['page_id']
        self.change('project', rid, {'summary': 'Before appended'})
        self.fake.fail = {'method': 'PATCH', 'path': '/pages/' + page + '/markdown', 'status': 503}
        self.assertEqual(self.publish()['published'], 1)
        self.fake.children[page] = [{'id': 'outside', 'has_children': False}]
        self.fake.drop_comments = True
        self.assertFalse(notion.probe_comments(self.fake.client, page)['survived'])

    def test_meeting_july_layout_human_attendees_no_duplicate_actions(self):
        self.add(
            'meeting',
            {
                'title': 'Synthetic meeting',
                'start_date': '2026-10-20',
                'starts_at': '2026-10-20T10:00:00Z',
                'duration_minutes': 30,
                'attendees_json': json.dumps([{'name': 'Example', 'email': 'example@example.org'}]),
                'action_points': 'Follow up',
                'previous_action_points': 'Previous action',
                'body_markdown': '# Notes\nNarrative',
            },
        )
        with db.connect(self.path) as conn:
            row = notion.render(conn)[0]
        self.assertEqual(
            list(row['sections']),
            [
                'Summary',
                'Attendees',
                'Meeting Notes',
                'Tasks by Linked Project',
                'Linked items',
                'Sources',
                'gsd id',
            ],
        )
        self.assertIn('- Example <example@example.org>', row['sections']['Attendees'])
        self.assertIn('Follow up', row['sections']['Meeting Notes'])
        self.assertNotIn('Action Points', row['values'])
        self.assertEqual(row['values']['When'], '2026-10-20')
        self.assertNotIn('10:00:00', row['body'])
        self.assertEqual(row['body'].count('Follow up'), 1)


if __name__ == '__main__':
    unittest.main()
