"""Notion inbox and status page: synthetic stores and clients; no credentials or network."""

import fcntl
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db, inbox, intent, notion, status_page, write

NOW = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)


def comment(cid, page=None, block=None, text='note', edited='2026-10-18T13:00:00.000Z'):
    parent = {'type': 'block_id', 'block_id': block} if block else {'type': 'page_id', 'page_id': page}
    return {'id': cid, 'discussion_id': 'd-' + cid, 'parent': parent, 'created_by': {'id': 'owner'},
            'created_time': '2026-10-18T12:59:00.000Z', 'last_edited_time': edited,
            'rich_text': [{'plain_text': text}]}


class FakeClient(notion.Client):
    def __init__(self):
        super().__init__('synthetic', interval=0)
        self.calls, self.comments, self.edited, self.blocks = [], {}, [], {}
        self.md, self.no_capability, self.uncertain, self.by_id = '', False, False, {}

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        self.counts.setdefault(method, Counter())[notion.route_class(method, path)] += 1
        if path.startswith('/comments/'):
            found = self.by_id.get(path.split('/')[2])
            if found is None:
                raise ValueError('Notion HTTP 404')
            return found
        if path.startswith('/comments'):
            if self.no_capability:
                raise ValueError('Notion HTTP 403')
            block = path.split('block_id=')[1].split('&')[0]
            return {'results': self.comments.get(block, []), 'has_more': False}
        if path.endswith('/query'):
            return {'results': [{'id': p} for p in self.edited], 'has_more': False}
        if path.startswith('/data_sources/'):
            return {'parent': {'database_id': 'db1'}}
        if path.startswith('/databases/'):
            return {'parent': {'page_id': 'root'}}
        if path == '/pages/root/markdown':
            if method == 'GET':
                return {'markdown': self.md}
            if data['type'] == 'update_content':
                for update in data['update_content']['content_updates']:
                    self.md = self.md.replace(update['old_str'], update['new_str'])
            else:
                self.md += data['insert_content']['content']
            return {}
        if path.startswith('/blocks/') and path.endswith('/children'):
            if self.uncertain:
                raise notion.UncertainWrite('synthetic')
            return {}
        if path.startswith('/blocks/'):
            block = path.split('/')[2]
            if method == 'DELETE':
                self.blocks[block]['in_trash'] = True
                return {}
            return self.blocks[block]
        raise AssertionError(path)


class Inbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'gsd.sqlite'
        with sqlite3.connect(self.path) as conn:
            db.migrate(conn)
        with db.connect(self.path, True) as conn:
            result = write.batch(conn, {'changes': [{'ops': [
                {'upsert': 'item', 'set': {'title': 'Synthetic task', 'kind': 'task'}}
            ]}]}, 'test')
            self.assertTrue(result[0]['ok'], result)
            self.item = db.rows(conn, 'items')[-1]['id']
        with db.connect(self.path, True) as conn:
            for table in notion.FIELDS:
                conn.execute('INSERT INTO notion_sources VALUES (?,?)', (table, table))
            conn.execute('INSERT INTO notion_map(record_id, page_id) VALUES (?,?)', (self.item, 'p1'))
            conn.execute("INSERT INTO notion_map(record_id, page_id) VALUES ('other','p2')")
        self.client = FakeClient()
        for target, value in ((notion, 'gate'), (status_page, 'steward_view'), (intent, 'qmd_status')):
            patcher = patch.object(target, value, side_effect=ValueError('offline') if value == 'qmd_status' else None,
                                   return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        telemetry = patch.object(inbox, 'TELEMETRY', Path(self.tmp.name) / 'telemetry.json')
        telemetry.start()
        self.addCleanup(telemetry.stop)

    def read(self, now=NOW, sweep=False):
        with db.connect(self.path) as gdb:
            return inbox.read_comments(self.client, gdb, inbox.connect(self.path), now, sweep)

    def comments(self):
        conn = inbox.connect(self.path)
        return {r['id']: dict(r) for r in conn.execute('SELECT * FROM comments')}

    def test_page_level_comments_sweep_daily_and_edits_come_back(self):
        self.client.comments = {'root': [comment('c1', page='root')], 'p1': [comment('c2', page='p1')]}
        self.assertEqual(self.read(), {'pages': 3, 'new': 2, 'swept': True})
        self.client.calls.clear()
        self.assertFalse(self.read(NOW + timedelta(minutes=15))['swept'])
        self.assertEqual(self.client.calls, [])
        first = self.comments()['c2']['seen_at']
        self.client.comments['p1'] = [comment('c2', page='p1', text='edited', edited='2026-10-18T14:10:00.000Z')]
        self.assertEqual(self.read(NOW + timedelta(hours=25))["new"], 1)
        self.assertGreater(self.comments()['c2']['seen_at'], first)
        self.assertEqual(self.comments()['c2']['text'], 'edited')
        self.assertTrue(self.read(NOW + timedelta(hours=25, minutes=5), sweep=True)['swept'])

    def test_webhook_events_fetch_line_comments_once_and_advance_the_cursor(self):
        self.client.by_id = {'c9': comment('c9', block='b9', text='on a line')}
        events = [{'seq': 1, 'type': 'comment.created', 'entity_id': 'c9', 'page_id': 'p1'},
                  {'seq': 2, 'type': 'comment.created', 'entity_id': 'gone', 'page_id': 'p1'},
                  {'seq': 3, 'type': 'comment.deleted', 'entity_id': 'c8', 'page_id': 'p1'}]
        asked = []

        def fetch(after):
            asked.append(after)
            return [e for e in events if e['seq'] > after]
        conn = inbox.connect(self.path)
        self.assertEqual(inbox.read_events(self.client, conn, fetch), {'events': 1})
        self.assertEqual(self.comments()['c9']['block_id'], 'b9')
        self.assertEqual(self.comments()['c9']['page_id'], 'p1')
        self.assertEqual(inbox.read_events(self.client, conn, fetch), {'events': 0})
        self.assertEqual(asked, [0, 3])

    def test_pull_skips_while_publish_holds_the_lock_and_reports_missing_capability(self):
        with open(str(self.path) + '.notion.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(inbox.pull(self.path, client=self.client, now=NOW),
                                 {'status': 'busy', 'reason': 'busy: Notion publish lock held'})
            self.assertEqual(self.client.calls, [])
        self.client.no_capability = True
        result = inbox.pull(self.path, client=self.client, now=NOW)
        self.assertEqual(result['comments'], 'no_capability')
        self.assertEqual(result['status_page'], 'published')
        self.assertIn('## Last run', self.client.md)
        self.assertIn('Comment reading is off', self.client.md)
        telemetry = json.loads((Path(self.tmp.name) / 'telemetry.json').read_text())
        self.assertEqual(telemetry['state'], 'red')
        self.client.calls.clear()
        again = inbox.pull(self.path, client=self.client, now=NOW + timedelta(minutes=15))
        self.assertEqual(again['status_page'], 'unchanged')
        self.assertFalse([c for c in self.client.calls if 'markdown' in c[1]])
        self.client.no_capability = False
        inbox.pull(self.path, client=self.client, now=NOW + timedelta(minutes=30))
        patches = [c[2] for c in self.client.calls if c[0] == 'PATCH' and 'markdown' in c[1]]
        self.assertEqual(len(patches), 1)  # only Your comments changed
        self.assertIn('## Your comments', patches[0]['update_content']['content_updates'][0]['new_str'])

    def test_window_gives_a_step_the_comments_first_seen_inside_it(self):
        self.client.comments = {'p1': [comment('c2', block='b1', text='Due Friday,\n not Monday')]}
        self.read()
        selected = {'refs': {}, 'views': {}, 'manifest': {}}
        seen = self.comments()['c2']['seen_at']
        inbox.window(selected, self.path, '2026-10-18T00:00:00.000000Z', seen)
        self.assertEqual(selected['comments'], 1)
        self.assertEqual(selected['refs'], {'nc1': 'notion-comment:c2'})
        self.assertIn(f'item {self.item} · Synthetic task · on a line', selected['views']['comments.md'])
        self.assertIn('Due Friday, not Monday', selected['views']['comments.md'])
        later = {'refs': {}, 'views': {}, 'manifest': {}}
        inbox.window(later, self.path, seen, '2026-10-19T00:00:00.000000Z')
        self.assertNotIn('comments', later)
        self.assertEqual(write.qmd_ref(sqlite3.connect(':memory:'), 'notion-comment:c2'), 'notion-comment:c2')

    def test_clear_recreates_read_leaf_blocks_and_keeps_the_rest(self):
        para = {'rich_text': [{'plain_text': 'x', 'text': {'content': 'x'}}], 'color': 'default'}
        self.client.blocks = {
            'b1': {'type': 'paragraph', 'paragraph': para, 'parent': {'page_id': 'p1'}, 'has_children': False},
            'b2': {'type': 'heading_2', 'heading_2': para, 'parent': {'page_id': 'p1'}, 'has_children': False},
            'b3': {'type': 'paragraph', 'paragraph': para, 'parent': {'page_id': 'p1'}, 'has_children': False},
            'b4': {'type': 'paragraph', 'paragraph': para, 'parent': {'page_id': 'root'}, 'has_children': False},
        }
        self.client.comments = {
            'p1': [comment('c1', block='b1'), comment('c2', block='b2'), comment('c3', block='b3')],
            'root': [comment('c4', block='b4'), comment('c5', page='root')],
        }
        self.read()
        self.client.comments.update(b1=[comment('c1', block='b1')], b2=[comment('c2', block='b2')],
                                    b3=[comment('c3', block='b3'), comment('late', block='b3')])
        with db.connect(self.path) as gdb:
            conn = inbox.connect(self.path)
            self.assertEqual(inbox.clear(self.client, gdb, conn, '2000-01-01'), 0)  # nothing read by a run yet
            self.assertEqual(inbox.clear(self.client, gdb, conn, '2100-01-01'), 1)
        state = {k: v['cleared'] for k, v in self.comments().items()}
        self.assertTrue(state['c1'].startswith('cleared'))
        self.assertEqual(state['c2'], 'kept: heading_2')
        self.assertIsNone(state['c3'])  # a late reply: waits for the next run
        self.assertIsNone(state['late'])
        self.assertEqual(state['c4'], 'kept: not a gsd page')
        self.assertIsNone(state['c5'])  # page-level comments are never cleared
        append = [c for c in self.client.calls if c[1] == '/blocks/p1/children']
        self.assertEqual(append[0][2]['position'], {'type': 'after_block', 'after_block': {'id': 'b1'}})
        self.assertEqual(append[0][2]['children'][0]['paragraph'], para)
        self.assertTrue(self.client.blocks['b1']['in_trash'])
        self.assertFalse(self.client.blocks['b3'].get('in_trash'))

    def test_already_ingested_unread_reply_defers_clear_and_recovery(self):
        conn = inbox.connect(self.path)
        for cid, seen in (('c1', '2026-10-18T10:00:00Z'), ('c2', '2026-10-18T12:00:00Z')):
            inbox.store_comment(conn, comment(cid, block='b1'), 'p1', seen)
        self.client.comments['b1'] = [comment('c1', block='b1'), comment('c2', block='b1')]
        self.client.blocks['b1'] = {'type': 'paragraph', 'paragraph': {'rich_text': []},
                                    'parent': {'page_id': 'p1'}, 'has_children': False}
        with db.connect(self.path) as gdb:
            self.assertEqual(inbox.clear(self.client, gdb, conn, '2026-10-18T11:00:00Z'), 0)
            self.assertFalse(self.client.blocks['b1'].get('in_trash'))
            inbox.put(conn, 'clearing', json.dumps({'old': 'b1', 'cut': '2026-10-18T11:00:00Z'}))
            self.assertEqual(inbox.clear(self.client, gdb, conn, '2026-10-18T11:00:00Z'), 0)
            self.assertFalse(self.client.blocks['b1'].get('in_trash'))
        self.assertTrue(all(r['cleared'] is None for r in self.comments().values()))
        with db.connect(self.path) as gdb:
            inbox.clear(self.client, gdb, conn, '2026-10-18T13:00:00Z')
        self.assertTrue(self.client.blocks['b1']['in_trash'])
        self.assertTrue(all(r['cleared'].startswith('cleared') for r in self.comments().values()))

    def test_clear_never_deletes_after_an_uncertain_copy(self):
        para = {'rich_text': [{'plain_text': 'x'}]}
        self.client.blocks = {'b1': {'type': 'paragraph', 'paragraph': para, 'parent': {'page_id': 'p1'}, 'has_children': False}}
        self.client.comments = {'p1': [comment('c1', block='b1')]}
        self.read()
        self.client.comments['b1'] = [comment('c1', block='b1')]
        self.client.uncertain = True
        with db.connect(self.path) as gdb:
            self.assertEqual(inbox.clear(self.client, gdb, inbox.connect(self.path), '2100-01-01'), 0)
        self.assertEqual(self.comments()['c1']['cleared'], 'kept: uncertain copy')
        self.assertFalse(any(m == 'DELETE' for m, _, _ in self.client.calls))


class StatusPage(unittest.TestCase):
    def doc(self):
        return {'blocks': [
            {'kind': 'stock', 'open': 225, 'you': 12, 'agents': 3, 'others': 210, 'past_due': 4, 'due_7d': 5,
             'fresh_through': '2026-10-18T11:25:00.000000Z'},
            {'kind': 'rows', 'items': [{'name': 'Project [A]', 'you': 4, 'link': 'https://www.notion.so/x'},
                                       {'name': 'Quiet', 'you': 0, 'link': None}]},
            {'kind': 'chain', 'hops': [{'name': 'qmd · Gmail', 'note': 'complete to 14:20'},
                                       {'name': 'gsd cut', 'note': 'next cut'}]},
        ]}

    def record(self, ended, landed=16, error=None, minutes=13):
        return {'ended': ended, 'counts': {'records_landed': landed}, 'minutes': minutes,
                'weighted_tokens': 1.2e6, 'error': error, 'terminated': False, 'empty_reason': None}

    def test_render_last_next_now_week_and_recent(self):
        history = [self.record(NOW - timedelta(hours=12), error='loop: x; publish notion: y'),
                   self.record(NOW - timedelta(minutes=30))]
        history[0]['steward_run_id'] = 'failed'
        history[1]['steward_run_id'] = 'succeeded'
        steward = {
            'runs': [
                {'status': 'skipped', 'started_at': (NOW - timedelta(minutes=10)).isoformat(), 'reason': 'host-load guard'},
                {'id': 'run-succeeded', 'status': 'succeeded', 'started_at': (NOW - timedelta(minutes=43)).isoformat(),
                 'ended_at': (NOW - timedelta(minutes=29)).isoformat(), 'reason': 'the tools menu'},
                {'id': 'run-failed', 'status': 'failed', 'started_at': (NOW - timedelta(hours=12, minutes=13)).isoformat(),
                 'ended_at': (NOW - timedelta(hours=12)).isoformat(), 'reason': 'default: ready_by'},
            ],
            'next': {'at': '2026-10-19T02:40:00+00:00', 'est_min': 12.6},
        }
        sections = status_page.render(self.doc(), history, steward, [{'timestamp': NOW.isoformat(), 'requests': {'total': 4}}], None, NOW)
        self.assertEqual(list(sections), ['Last run', 'Now', 'Last 7 days', 'Recent runs'])
        self.assertIn('icon="✅"', sections['Last run'])
        self.assertIn('16 records landed', sections['Last run'])
        self.assertIn('· manual', sections['Last run'])
        self.assertIn('**Mon 05:40 EEST** (about 13 min)', sections['Last run'])
        self.assertIn('**12** waiting on you', sections['Now'])
        self.assertIn('[Project (A)](https://www.notion.so/x) 4', sections['Now'])
        self.assertNotIn('Quiet', sections['Now'])
        self.assertIn('Gmail complete to 14:20', sections['Now'])
        self.assertIn('**2** runs: 1 ✓ · 1 ✗', sections['Last 7 days'])
        recent = sections['Recent runs'].splitlines()
        self.assertTrue(recent[0].startswith('- ⏭'))
        self.assertIn('❌', recent[2])
        self.assertIn('loop: failed; publish notion: failed', recent[2])
        for text in sections.values():
            self.assertNotIn('\n---', text)
            self.assertNotIn('\n## ', text)

    def test_spark_scales_and_flat(self):
        self.assertEqual(status_page.spark([0, 7]), '▁█')
        self.assertEqual(status_page.spark([3, 3]), '▁▁')
        self.assertEqual(status_page.spark([]), '—')


if __name__ == '__main__':
    unittest.main()
