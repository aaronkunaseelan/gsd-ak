"""gsd.brief.v1 and gsd.closures.v1: the producer's views for Steward's prepare job and 'used' outcome."""

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
from gsd import db as store, brief, worklist, write
from gsd.contract_validate import problems

BRIEF = json.loads((ROOT / 'contracts/gsd.brief.v1.json').read_text())
CLOSURES = json.loads((ROOT / 'contracts/gsd.closures.v1.json').read_text())


class Views(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'graph.sqlite'
        with sqlite3.connect(self.path) as db:
            store.migrate(db)

    def upsert(self, entity, rid=None, cite=None, when=None, **fields):
        op = {'upsert': entity, 'set': fields}
        if rid:
            op['id'] = rid
        if cite or fields.get('assigned_to') or fields.get('state') in ('done', 'canceled'):
            op['cite'] = cite or [{'ref': 'gmail:synthetic', 'role': 'decides', 'excerpt': 'Synthetic quote'}]
        with patch.object(store, 'now', return_value=store.iso(when or '2026-10-20T08:00Z')):
            with store.connect(self.path, True) as db:
                result = write.batch(db, {'changes': [{'ops': [op]}]}, 'test')
        self.assertTrue(result[0]['ok'], result)
        return result[0]['records'][0]

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != 'GSD_DB'}
        return subprocess.run([str(ROOT / 'bin/gsd'), *args, '--db', str(self.path), '--json'], env=env,
                              text=True, capture_output=True, check=False, cwd='/tmp')

    def test_brief_fields_people_citations_and_errors(self):
        project = self.upsert('project', name='Grants', status='active')
        person = self.upsert('person', display_name='Synthetic funder')
        item = self.upsert('item', title='Reply to the funder', project_id=project, summary='They asked for routes.',
                           next_actions='Send the two routes', waiting_on=person, due_at='2026-10-23', priority=80,
                           cite=[{'ref': 'gmail:synthetic/1', 'role': 'states', 'excerpt': 'Could you send the routes?'}])
        with store.connect(self.path, True) as db:
            write.batch(db, {'changes': [{'ops': [{'link': [item, person, 'involves_person']}]}]}, 'test')
            db.execute('INSERT INTO notion_map VALUES (?,?,NULL,NULL,NULL)', (item, '1111-2222'))
            store.change_set(db, 'latest', store.PIPELINE_ACTOR, 'pipeline', window_until=store.iso('2026-10-20T00:00:00Z'))
        other = self.upsert('item', title='Other', state='done')
        result = brief.command(SimpleNamespace(db=self.path, ids=[item, other[:8], 'zzz', '']))  # '' prefixes every id
        self.assertEqual(problems(BRIEF, result), [])
        self.assertEqual(set(result), set(BRIEF['properties']))
        self.assertEqual(result['cut']['change_set_id'], 'latest')
        first, second, unknown, ambiguous = result['items']
        self.assertEqual(set(first), set(BRIEF['properties']['items']['items']['properties']) - {'error'})
        self.assertEqual(first['id'], item)
        self.assertEqual((first['title'], first['project'], first['state'], first['next_actor']),
                         ('Reply to the funder', 'Grants', 'inbox', 'user'))
        self.assertEqual((first['summary'], first['next_actions']), ('They asked for routes.', 'Send the two routes'))
        self.assertEqual(first['waiting_on'], 'Synthetic funder')
        self.assertEqual(first['due_at'], store.iso('2026-10-23T00:00:00Z'))
        self.assertEqual(first['priority'], '80')
        self.assertEqual(first['created_at'], store.iso('2026-10-20T08:00Z'))
        self.assertEqual(first['notion_url'], 'https://www.notion.so/11112222')
        self.assertEqual(first['people'], [{'id': person, 'name': 'Synthetic funder', 'relation': 'involves_person'}])
        self.assertEqual(first['citations'], [{'ref': 'gmail:synthetic/1', 'role': 'states',
                                               'excerpt': 'Could you send the routes?', 'at': store.iso('2026-10-20T08:00Z')}])
        self.assertEqual((second['id'], second['state'], second['project'], second['waiting_on'], second['people']),
                         (other, 'done', None, None, []))
        self.assertEqual(unknown, {'id': 'zzz', 'error': 'unknown item'})
        self.assertEqual(ambiguous, {'id': '', 'error': 'ambiguous item'})
        cli = self.cli('brief', item)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertEqual(problems(BRIEF, json.loads(cli.stdout)), [])
        for identity, contract in (('gsd.brief.v1', BRIEF), ('gsd.closures.v1', CLOSURES)):
            result = self.cli('contract', identity)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), contract)
        verbs = json.loads(self.cli('verbs').stdout)
        self.assertTrue({'brief', 'closures'} <= {v['name'] for v in verbs})

    def test_closures_use_the_closure_time_since_a_boundary(self):
        project = self.upsert('project', name='Work', status='active')
        item = self.upsert('item', title='Task', project_id=project, when='2026-10-17T00:00:00Z')
        self.upsert('item', item, state='done', when='2026-10-18T12:00Z')
        self.upsert('item', item, title='Edited later', when='2026-10-20T11:00Z')
        old = self.upsert('item', title='Old', state='done', when='2026-10-18T11:59:59Z')
        self.upsert('item', old, title='Old but edited today', when='2026-10-20T11:00Z')
        canceled = self.upsert('item', title='Canceled', state='canceled', when='2026-10-19T00:00:00Z')
        self.upsert('item', title='Future', state='done', when='2026-10-20T12:00:01Z')
        with store.connect(self.path, True) as db:
            store.change_set(db, 'latest', store.PIPELINE_ACTOR, 'pipeline', window_until=store.iso('2026-10-20T00:00:00Z'))
        with patch.object(store, 'now', return_value=store.iso('2026-10-20T12:00Z')):
            result = worklist.closures_command(SimpleNamespace(db=self.path, since='2026-10-18T12:00Z'))
        self.assertEqual(problems(CLOSURES, result), [])
        self.assertEqual(set(result), set(CLOSURES['properties']))
        self.assertEqual((result['since'], result['at'], result['cut']['change_set_id']),
                         (store.iso('2026-10-18T12:00Z'), store.iso('2026-10-20T12:00Z'), 'latest'))
        self.assertEqual(result['closures'], [
            {'id': item, 'title': 'Task', 'project': 'Work', 'state': 'done', 'at': store.iso('2026-10-18T12:00Z')},
            {'id': canceled, 'title': 'Canceled', 'project': None, 'state': 'canceled', 'at': store.iso('2026-10-19T00:00:00Z')},
        ])
        with patch.object(store, 'now', return_value=store.iso('2026-10-20T12:00Z')):
            same = worklist.command(SimpleNamespace(db=self.path))['done']
        self.assertEqual(result['closures'], same)
        cli = self.cli('closures', '--since', '2026-10-18T12:00:00+00:00')
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertEqual(problems(CLOSURES, json.loads(cli.stdout)), [])
        self.assertEqual(self.cli('closures', '--since', 'yesterday').returncode, 1)
        self.assertEqual(self.cli('closures').returncode, 2)


if __name__ == '__main__':
    unittest.main()
