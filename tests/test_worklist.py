"""Contract output from synthetic SQLite, including a concurrent WAL writer."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store, worklist, write
from gsd.contract_validate import problems

CONTRACT = json.loads((ROOT / 'contracts/gsd.worklist.v1.json').read_text())


class Worklist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'graph.sqlite'
        with sqlite3.connect(self.path) as db:
            store.migrate(db)
            db.execute('PRAGMA journal_mode=WAL')

    def upsert(self, entity, rid=None, **fields):
        op = {'upsert': entity, 'set': fields}
        if rid:
            op['id'] = rid
        if fields.get('assigned_to') or fields.get('state') in ('done', 'canceled'):
            op['cite'] = [{'ref': 'gmail:synthetic', 'role': 'decides', 'excerpt': 'Synthetic quote'}]
        with store.connect(self.path, True) as db:
            result = write.batch(db, {'changes': [{'ops': [op]}]}, 'test')
        self.assertTrue(result[0]['ok'], result)
        return result[0]['records'][0]

    def payload(self):
        result = worklist.command(SimpleNamespace(db=self.path))
        self.assertEqual(problems(CONTRACT, result), [])
        self.assertEqual(set(result), set(CONTRACT['properties']))
        for kind in ('projects', 'items', 'done'):
            for row in result[kind]:
                self.assertEqual(set(row), set(CONTRACT['properties'][kind]['items']['properties']))
        self.assertEqual(set(result['cut']), set(CONTRACT['properties']['cut']['properties']))
        return result

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != 'GSD_DB'}
        return subprocess.run([str(ROOT / 'bin/gsd'), *args], env=env, text=True,
                              capture_output=True, check=False, cwd='/tmp')

    def test_contract_rejects_extra_key_when_closed(self):
        payload = self.payload()
        schema = {**CONTRACT, 'additionalProperties': False}
        self.assertEqual(problems(schema, payload), [])
        payload['unexpected'] = True
        self.assertEqual(problems(schema, payload), ['$.unexpected: unexpected'])

    def test_empty_and_contract_and_advertised_verbs(self):
        payload = self.payload()
        self.assertEqual(payload['cut'], dict.fromkeys(('until', 'change_set_id', 'committed_at')))
        self.assertEqual(payload['projects'], [])
        self.assertEqual(payload['items'], [])
        self.assertEqual(payload['done'], [])
        for flags in ([], ['--json']):
            result = self.cli('contract', 'gsd.worklist.v1', *flags, '--db', '/nonexistent')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, (ROOT / 'contracts/gsd.worklist.v1.json').read_text())
        self.assertEqual(self.cli('contract', '../schema').returncode, 1)
        verbs = json.loads(self.cli('verbs', '--json').stdout)
        self.assertTrue({'worklist', 'contract'} <= {v['name'] for v in verbs})
        result = self.cli('worklist', '--json', '--db', str(self.path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(problems(CONTRACT, json.loads(result.stdout)), [])

    def test_open_current_rows_projects_owners_dates_and_notion(self):
        life = self.upsert('project', name='Personal', status='active',
                           metadata_json='{"scope":"Owner commitments"}')
        project = self.upsert('project', name='Work', status='active')
        self.upsert('project', name='Inactive', status='archived')
        person = self.upsert('person', display_name='Synthetic owner')
        assigned = self.upsert('item', title='Assigned', project_id=project, assigned_to=person,
                               due_at='2026-10-19T10:00:00+02:00', urgency='soon',
                               priority=75, next_review_at='2026-10-18')
        question = self.upsert('item', title='Question', kind='question', assigned_to='', due_at=None)
        for state in ('ready', 'running', 'blocked',
                      'done', 'canceled'):
            self.upsert('item', title=state, state=state)
        deleted = self.upsert('item', title='Tombstoned')
        self.upsert('item', assigned, title='Current title')
        with store.connect(self.path, True) as db:
            self.assertTrue(write.batch(db, {'changes': [{'ops': [{'tombstone': deleted}]}]}, 'test')[0]['ok'])
            for rid, page in ((life, '1111-2222'), (assigned, '3333-4444')):
                db.execute('INSERT INTO notion_map VALUES (?,?,NULL,NULL,NULL)', (rid, page))
            store.change_set(db, 'old', store.PIPELINE_ACTOR, 'pipeline', window_until=store.iso('2026-10-16T00:00:00Z'))
            store.change_set(db, 'latest', store.PIPELINE_ACTOR, 'pipeline', window_until=store.iso('2026-10-17T00:00:00Z'))
            store.change_set(db, 'owner', 'test', 'owner', window_until=store.iso('2026-10-20T00:00:00Z'))
        result = self.payload()
        self.assertEqual(len(result['projects']), 2)
        projects = {p['id']: p for p in result['projects']}
        self.assertNotIn('life_area', projects[life])
        self.assertNotIn('life_area', projects[project])
        self.assertEqual(projects[life]['notion_url'], 'https://www.notion.so/11112222')
        self.assertIsNone(projects[project]['notion_url'])
        items = {i['id']: i for i in result['items']}
        self.assertEqual(len(items), 5)
        self.assertNotIn(deleted, items)
        item = items[assigned]
        self.assertEqual(item['title'], 'Current title')
        self.assertEqual(item['assigned_to'], person)
        self.assertEqual(item['assigned_name'], 'Synthetic owner')
        self.assertEqual(item['priority'], '75')
        self.assertEqual(item['due_at'], store.iso('2026-10-19T08:00Z'))
        self.assertEqual(item['next_review_at'], store.iso('2026-10-18T00:00:00Z'))
        self.assertEqual(item['notion_url'], 'https://www.notion.so/33334444')
        for key in ('assigned_to', 'assigned_name', 'due_at', 'notion_url', 'project_id'):
            self.assertIsNone(items[question][key])
        self.assertEqual(items[question]['kind'], 'question')
        self.assertEqual(result['cut']['until'], store.iso('2026-10-17T00:00:00Z'))
        self.assertEqual(result['cut']['change_set_id'], 'latest')
        cursor = self.cli('cursor', '--json', '--db', str(self.path))
        self.assertEqual(json.loads(cursor.stdout), result['cut']['until'])
        with store.connect(self.path) as db:
            at = db.execute("SELECT at FROM change_sets WHERE change_set_id='latest'").fetchone()[0]
        self.assertEqual(result['cut']['committed_at'], at)

    def test_priorities_actor_order_dates_and_projects(self):
        project = self.upsert('project', name='Work', status='archived')
        first = self.upsert('item', title='First tie', project_id=project,
                            next_actor='user', priority=90, due_at='2026-10-21')
        second = self.upsert('item', title='Second tie', next_actor='user',
                             priority=90, due_at='2026-10-21')
        undated = self.upsert('item', title='Undated', next_actor='user', priority=90)
        lower = self.upsert('item', title='Lower', next_actor='user', priority=20,
                            due_at='2026-10-19')
        self.upsert('item', title='Overdue external', next_actor='external',
                    due_at='2026-10-20T11:59:59Z')
        start = self.upsert('item', title='Due now external', next_actor='external',
                            due_at='2026-10-20T12:00Z')
        boundary = self.upsert('item', title='Boundary external', next_actor='external',
                               due_at='2026-10-22T12:00Z')
        for actor, due in (('external', '2026-10-22T12:00:01Z'), ('external', None),
                           ('agent', '2026-10-21'), ('none', '2026-10-21')):
            self.upsert('item', title='Excluded', next_actor=actor, due_at=due)
        self.upsert('item', title='Closed', next_actor='user', state='done')
        deleted = self.upsert('item', title='Deleted', next_actor='user')
        with store.connect(self.path, True) as db:
            write.batch(db, {'changes': [{'ops': [{'tombstone': deleted}]}]}, 'test')
        with patch.object(store, 'now', return_value=store.iso('2026-10-20T12:00Z')):
            result = self.payload()['priorities']
        self.assertEqual([r['id'] for r in result['mine']], [first, second, undated, lower])
        self.assertEqual(result['mine'][0], dict(id=first, title='First tie', project='Work',
                         due_at=store.iso('2026-10-21T00:00:00Z'), priority='90'))
        self.assertEqual([r['id'] for r in result['waiting_on_others']], [start, boundary])

    def test_done_uses_state_transitions_not_latest_edit(self):
        def at(when, entity, rid=None, **fields):
            with patch.object(store, 'now', return_value=store.iso(when)):
                return self.upsert(entity, rid, **fields)

        project = self.upsert('project', name='Work', status='archived')
        item = at('2026-10-17T00:00:00Z', 'item', title='Task', project_id=project)
        at('2026-10-18T12:00Z', 'item', item, state='done')  # inclusive 48h boundary
        at('2026-10-20T11:00Z', 'item', item, title='Edited later')
        canceled = at('2026-10-19T00:00:00Z', 'item', title='Canceled', state='canceled')
        at('2026-10-19T01:00Z', 'item', canceled, state='ready')
        at('2026-10-20T10:00Z', 'item', canceled, state='done')
        old = at('2026-10-18T11:59:59Z', 'item', title='Old', state='done')
        at('2026-10-20T11:00Z', 'item', old, title='Old but edited today')
        at('2026-10-20T12:00:01Z', 'item', title='Future', state='done')
        with patch.object(store, 'now', return_value=store.iso('2026-10-20T12:00Z')):
            result = self.payload()
        self.assertEqual(result['done'], [
            {'id': item, 'title': 'Task', 'project': 'Work', 'state': 'done',
             'at': store.iso('2026-10-18T12:00Z')},
            {'id': canceled, 'title': 'Canceled', 'project': None, 'state': 'canceled',
             'at': store.iso('2026-10-19T00:00:00Z')},
            {'id': canceled, 'title': 'Canceled', 'project': None, 'state': 'done',
             'at': store.iso('2026-10-20T10:00Z')},
        ])

    def test_done_ignores_tombstones_but_keeps_closed_creation(self):
        item = self.upsert('item', title='Task', state='done')
        with store.connect(self.path, True) as db:
            write.batch(db, {'changes': [{'ops': [{'tombstone': item}]}]}, 'test')
        result = self.payload()
        self.assertEqual([r['state'] for r in result['done']], ['done'])
        self.assertEqual(result['items'], [])

    def test_legacy_assignee_resolves_only_unambiguous_people(self):
        person = self.upsert('person', display_name='Synthetic Person')
        for ref in (person, person[:12], 'Synthetic Person', 'unknown', 'Duplicate'):
            self.upsert('item', title=ref, assigned_to=ref)
        self.upsert('person', display_name='Duplicate')
        self.upsert('person', display_name='Duplicate')
        rows = self.payload()['items']
        for row in rows:
            expected = person if row['title'] in (person, person[:12], 'Synthetic Person') else None
            self.assertEqual(row['assigned_to'], expected)
            self.assertEqual(row['assigned_name'], 'Synthetic Person' if expected else None)

    def test_one_transaction_keeps_cut_and_rows_consistent_during_writer(self):
        project = self.upsert('project', name='Before', status='active')
        item = self.upsert('item', title='Before', project_id=project)
        rows = store.rows
        changed = False

        def concurrent(db, table):
            nonlocal changed
            self.assertTrue(db.in_transaction)
            if not changed:
                changed = True
                self.upsert('project', project, name='After')
                self.upsert('item', item, title='After')
                with store.connect(self.path, True) as writer:
                    store.change_set(writer, 'concurrent', store.PIPELINE_ACTOR, 'pipeline',
                                     window_until=store.iso('2026-10-17T00:00:00Z'))
                    writer.execute('INSERT INTO notion_map VALUES (?,?,NULL,NULL,NULL)',
                                   (item, '1234-5678'))
            return rows(db, table)

        with patch.object(store, 'rows', concurrent):
            before = self.payload()
        self.assertIsNone(before['cut']['until'])
        self.assertEqual(before['projects'][0]['name'], 'Before')
        self.assertEqual(before['items'][0]['title'], 'Before')
        self.assertIsNone(before['items'][0]['notion_url'])
        after = self.payload()
        self.assertEqual(after['cut']['change_set_id'], 'concurrent')
        self.assertEqual(after['projects'][0]['name'], 'After')
        self.assertEqual(after['items'][0]['title'], 'After')
        self.assertEqual(after['items'][0]['notion_url'], 'https://www.notion.so/12345678')


if __name__ == '__main__':
    unittest.main()
