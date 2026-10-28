"""R2: entirely synthetic stores and clients; no credentials or network."""

import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from collections import Counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db, notion, write


class FakeClient(notion.Client):
    def __init__(self):
        super().__init__('synthetic', interval=0)
        self.pages, self.bodies, self.calls = {}, {}, []
        self.warnings = {}
        self.uncertain = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        self.counts.setdefault(method, Counter())[notion.route_class(method, path)] += 1
        if path.startswith('/data_sources/'):
            source = path.split('/')[2]
            if path.endswith('/query'):
                return {'results': [p for p in self.pages.values()
                                    if p['parent']['data_source_id'] == source], 'has_more': False}
            return {'properties': notion.expected_schema(source, {t: t for t in notion.FIELDS})}
        if method == 'POST' and path == '/pages':
            page = 'page-' + str(len(self.pages) + 1)
            self.pages[page] = {'id': page, 'properties': data['properties'],
                                'parent': data['parent']}
            self.bodies[page] = data['markdown']
            if self.uncertain:
                self.uncertain = False
                raise notion.UncertainWrite('synthetic dropped create response')
            return self.pages[page]
        page = path.split('/')[2]
        if path.endswith('/markdown'):
            if method == 'GET':
                return {'markdown': self.bodies[page]}
            if data['type'] == 'update_content':
                for update in data['update_content']['content_updates']:
                    self.bodies[page] = self.bodies[page].replace(update['old_str'], update['new_str'])
            else:
                self.bodies[page] += data['insert_content']['content']
            return {'warnings': self.warnings.get(page, [])}
        if method == 'PATCH':
            self.pages[page]['properties'].update(data['properties'])
        return self.pages[page]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'scratch.sqlite'
        with sqlite3.connect(self.path) as conn:
            db.migrate(conn)
        with db.connect(self.path, True) as conn:
            for table in notion.FIELDS:
                conn.execute('INSERT INTO notion_sources VALUES (?,?)', (table, table))
        self.client = FakeClient()
        gate = patch.object(notion, 'gate')
        token = patch.object(notion, 'token', side_effect=AssertionError('must not read tokens'))
        gate.start()
        token.start()
        self.addCleanup(gate.stop)
        self.addCleanup(token.stop)

    def add(self, name):
        with db.connect(self.path, True) as conn:
            result = write.batch(conn, {'changes': [{'ops': [
                {'upsert': 'person', 'set': {'display_name': name}}
            ]}]}, 'test')
            self.assertTrue(result[0]['ok'], result)
            return db.rows(conn, 'people')[-1]['id']

    def publish(self, check=False):
        return notion.publish(self.path, client=self.client, check=check)

    def mappings(self):
        with db.connect(self.path) as conn:
            return {r['record_id']: dict(r) for r in conn.execute('SELECT * FROM notion_map')}

    def query_count(self):
        return sum(path.endswith('/query') for _, path, _ in self.client.calls)

    def test_mapping_migration_keeps_confirmed_and_pending_identities(self):
        with db.connect(self.path, True) as conn:
            conn.executescript('''DROP TABLE notion_map;
CREATE TABLE notion_map (
 record_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, content_hash TEXT,
 body_hash TEXT, published_at TEXT
) STRICT;
INSERT INTO notion_map VALUES ('confirmed', 'page-1', 'hash', 'body', 'time');
PRAGMA user_version=4;''')
            db.migrate(conn)
            self.assertEqual(conn.execute('SELECT page_id FROM notion_map').fetchone()[0], 'page-1')
            conn.execute("INSERT INTO notion_map(record_id) VALUES ('uncertain')")
            conn.commit()
            db.migrate(conn)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notion_map').fetchone()[0], 2)
            self.assertIsNone(conn.execute(
                "SELECT page_id FROM notion_map WHERE record_id='uncertain'"
            ).fetchone()[0])

    def test_uncertain_create_is_reconciled_before_another_create(self):
        rid = self.add('Eventually visible')
        request = self.client.request
        delayed, posts = [], []

        def delay(method, path, data=None):
            if (method, path) == ('POST', '/pages'):
                posts.append(data)
                if len(posts) == 1:
                    delayed.append(data)
                    raise notion.UncertainWrite('synthetic timeout')
            return request(method, path, data)

        with patch.object(self.client, 'request', side_effect=delay):
            with self.assertRaisesRegex(ValueError, 'uncertain Notion create'):
                self.publish()
            # Invocation two must not turn delayed visibility into another POST.
            try:
                self.publish()
            except ValueError as exc:
                self.assertIn('uncertain Notion create', str(exc))
            self.assertEqual(len(posts), 1)
            pending = self.mappings()[rid]
            self.assertIsNone(pending['page_id'])
            self.assertIsNone(pending['content_hash'])
            report = self.publish(check=True)
            self.assertTrue(report['missing_pages'])
            self.assertFalse(all(c['ok'] for c in notion.projection_telemetry(self.path)[0]))
            request('POST', '/pages', delayed[0])
            self.publish()
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.client.pages), 1)
        self.assertTrue(self.mappings()[rid]['page_id'])
        self.assertTrue(self.mappings()[rid]['content_hash'])

    def test_two_creates_query_once_and_never_read_mapped_markdown(self):
        for n in range(7):
            self.add('Mapped ' + str(n))
        self.publish()
        mapped = {r['page_id'] for r in self.mappings().values()}
        self.client.calls.clear()
        self.add('First new')
        self.add('Second new')
        self.assertEqual(self.publish()['published'], 2)
        self.assertEqual(self.query_count(), 1)
        self.assertFalse(any(method == 'GET' and path == f'/pages/{page}/markdown'
                             for method, path, _ in self.client.calls for page in mapped))

    def test_orphan_adopted_full_properties_read_and_removed_from_inventory(self):
        rid = self.add('Orphan')
        self.add('Second')
        with db.connect(self.path) as conn:
            row = next(r for r in notion.render(conn) if r['record_id'] == rid)
        self.client.pages['orphan'] = {'id': 'orphan', 'parent': {'data_source_id': 'people'},
                                        'properties': {}}
        self.client.bodies['orphan'] = row['body']
        self.assertEqual(self.publish()['published'], 2)
        self.assertEqual(self.mappings()[rid]['page_id'], 'orphan')
        self.assertEqual(self.query_count(), 1)
        self.assertEqual(sum(method == 'POST' and path == '/pages'
                             for method, path, _ in self.client.calls), 1)
        self.assertIn(('GET', '/pages/orphan', None), self.client.calls)
        # One identity read plus one ordinary edit read, not a third read for Second.
        self.assertEqual(sum(method == 'GET' and path == '/pages/orphan/markdown'
                             for method, path, _ in self.client.calls), 2)

    def test_duplicate_unmapped_id_refused(self):
        rid = self.add('Duplicate')
        for page in ('one', 'two'):
            self.client.pages[page] = {'id': page, 'parent': {'data_source_id': 'people'},
                                        'properties': {}}
            self.client.bodies[page] = f'## gsd id\n\n{rid}'
        with self.assertRaisesRegex(ValueError, 'multiple Notion pages'):
            self.publish()
        self.assertFalse(self.mappings())
        self.assertFalse(any(path == '/pages' for _, path, _ in self.client.calls))

    def test_uncertain_create_queries_fresh_and_excludes_mapped_pages(self):
        mapped = self.add('Mapped')
        self.publish()
        mapped_page = self.mappings()[mapped]['page_id']
        self.client.calls.clear()
        rid = self.add('Uncertain')
        self.add('After uncertain')
        self.client.uncertain = True
        self.assertEqual(self.publish()['published'], 2)
        self.assertIn(rid, self.mappings())
        self.assertEqual(self.query_count(), 2)
        self.assertEqual(sum(method == 'POST' and path == '/pages'
                             for method, path, _ in self.client.calls), 2)
        recovered_page = self.mappings()[rid]['page_id']
        self.assertEqual(sum(method == 'GET' and path == f'/pages/{recovered_page}/markdown'
                             for method, path, _ in self.client.calls), 2)
        self.assertNotIn(('GET', f'/pages/{mapped_page}/markdown', None), self.client.calls)

    def test_mapped_uuid_spelling_is_normalized(self):
        page = '12345678-1234-1234-1234-123456789abc'
        self.client.pages[page] = {'id': page, 'parent': {'data_source_id': 'people'}}
        found = notion.find_created(self.client, 'people', {'record_id': 'unused'}, {},
                                     mapped_ids=[page.replace('-', '')])
        self.assertEqual(found, (None, False))
        self.assertFalse(any('/markdown' in path for _, path, _ in self.client.calls))

    def test_warning_incomplete_other_page_completes_and_next_run_retries(self):
        for operation in ('update', 'append'):
            with self.subTest(operation=operation):
                first = self.add('Warning ' + operation)
                second = self.add('Okay ' + operation)
                self.publish()
                maps = self.mappings()
                page = maps[first]['page_id']
                if operation == 'append':
                    self.client.bodies[page] = ''
                else:
                    self.client.bodies[page] = self.client.bodies[page].replace(
                        '## Summary\n\n_(empty)_', '## Summary\n\nOld text')
                with db.connect(self.path, True) as conn:
                    # Force a repair of both existing identities.
                    conn.execute('UPDATE notion_map SET content_hash=NULL,body_hash=NULL '
                                 'WHERE record_id IN (?,?)', (first, second))
                self.client.warnings[page] = [
                    {'code': 'no_match', 'message': 'PRIVATE warning message'},
                    {'code': 'unsupported_tag', 'message': 'PRIVATE content'},
                ]
                report = self.publish()
                plan = next(p for p in report['pages'] if p['record_id'] == first)
                self.assertIn('notion warning: no_match', plan['errors'])
                self.assertIn('notion warning: unsupported_tag', plan['errors'])
                self.assertNotIn('PRIVATE', json.dumps(report))
                self.assertIsNone(self.mappings()[first]['content_hash'])
                self.assertIsNotNone(self.mappings()[second]['content_hash'])
                self.assertEqual(report['published'], 1)
                self.client.warnings.clear()
                self.assertEqual(self.publish()['published'], 1)
                self.assertIsNotNone(self.mappings()[first]['content_hash'])

    def test_report_and_private_jsonl_only_aggregate_fields(self):
        rid = self.add('PRIVATE title https://example.invalid/secret')
        log = self.path.parent / 'notion-publish.jsonl'
        self.publish(check=True)
        checked = json.loads(log.read_text())
        self.assertTrue(checked['projection_check']['known'])
        report = self.publish()
        entry = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(set(entry), {'timestamp', 'requests', 'plan', 'published', 'archived'})
        self.assertEqual(entry['archived'], 0)
        self.assertEqual(entry['requests'], report['requests'])
        self.assertEqual(entry['requests']['total'], len(self.client.calls))
        self.assertEqual(entry['plan'], report['plan'])
        self.assertEqual(entry['published'], 1)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        for forbidden in (rid, 'page-1', 'PRIVATE', 'example.invalid', 'synthetic'):
            self.assertNotIn(forbidden, log.read_text())
        log.chmod(0o644)
        self.publish()
        self.assertEqual(len(log.read_text().splitlines()), 3)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)


class CounterTests(unittest.TestCase):
    def test_attempts_routes_statuses_sleep_and_elapsed(self):
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        failures = [urllib.error.HTTPError('synthetic', 429, '', {'Retry-After': '2'}, None),
                    urllib.error.HTTPError('synthetic', 503, '', {}, None)]
        def urlopen(*args, **kwargs):
            if failures:
                raise failures.pop(0)
            return io.BytesIO(b'{}')
        with (patch.object(notion.time, 'monotonic', side_effect=lambda: clock[0]),
              patch.object(notion.time, 'sleep', side_effect=sleep),
              patch.object(notion.urllib.request, 'urlopen', side_effect=urlopen)):
            client = notion.Client('synthetic', interval=1)
            routes = [('GET', '/data_sources/private'),
                      ('POST', '/data_sources/private/query'),
                      ('GET', '/pages/private'), ('PATCH', '/pages/private'),
                      ('POST', '/pages'), ('GET', '/pages/private/markdown'),
                      ('PATCH', '/pages/private/markdown'), ('GET', '/comments?block_id=private')]
            for method, path in routes:
                client.request(method, path)
            stats = client.requests()
        self.assertEqual(stats, {
            'total': 10,
            'by_method': {'GET': {'schema': 3, 'page_get': 1, 'markdown_get': 1, 'other': 1},
                          'POST': {'query': 1, 'page_create': 1},
                          'PATCH': {'page_patch': 1, 'markdown_patch': 1}},
            'http_429': 1, 'http_5xx': 1, 'retry_after_seconds': 2,
            'pacing_seconds': 7, 'elapsed_seconds': 11,
        })
        self.assertNotIn('private', json.dumps(stats))

    def test_uncertain_write_5xx_counted_without_retry(self):
        with (patch.object(notion.time, 'sleep'),
              patch.object(notion.urllib.request, 'urlopen', side_effect=
                           urllib.error.HTTPError('synthetic', 529, '', {}, None)) as transport):
            client = notion.Client('synthetic', interval=0)
            with self.assertRaises(notion.UncertainWrite):
                client.request('POST', '/pages', {})
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(client.requests()['http_5xx'], 1)
        self.assertEqual(client.requests()['total'], 1)


if __name__ == '__main__':
    unittest.main()
