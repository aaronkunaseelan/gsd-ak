"""Real SQLite, real CLI subprocesses, synthetic content only."""

import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store
from gsd import draft, write


class CLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(seed_tmp.cleanup)
        cls.seed = Path(seed_tmp.name) / 'seed.sqlite'
        conn = sqlite3.connect(cls.seed)
        try:
            store.migrate(conn)
        finally:
            # Close/checkpoint before copying; every test gets its own WAL store.
            conn.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / 'base.sqlite'
        # Migration through the CLI is exercised by the schema migration test.
        shutil.copyfile(self.seed, self.db)

    def run_cli(self, *args, data=None, path=None, ok=True):
        env = dict(os.environ)
        env.pop('GSD_DB', None)
        env.pop('GSD_ACTOR', None)
        p = subprocess.run(
            [str(ROOT / 'bin/gsd'), *args, '--db', str(path or self.db), '--json'],
            input=json.dumps(data) if data is not None else '',
            text=True,
            capture_output=True,
            env=env,
            cwd='/tmp',
        )
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        try:
            return json.loads(p.stdout)
        except ValueError:
            self.fail(p.stdout + p.stderr)

    def item(self, title='Synthetic item', **fields):
        return self.run_cli('upsert', 'item', data={'title': title, **fields})[0]['records'][0]

    def snapshot(self):
        path = self.base / 'step.sqlite'
        self.run_cli(
            'snapshot',
            '--out',
            str(path),
            '--step-id',
            'step',
            '--since',
            '2026-08-13T00:00:00Z',
            '--until',
            '2026-08-17T00:00:00Z',
        )
        return path

    def changes(self, changes, path, ok=True):
        return self.run_cli('write', path=path, data={'changes': changes}, ok=ok)

    def decide(self, path, cid, decision, **kw):
        return self.run_cli(
            'decide', path=path, data={'decisions': [{'id': cid, 'decision': decision, **kw}]}
        )

    def show(self, rid, path=None):
        return self.run_cli('show', rid, path=path)[0]

    def test_schema_migration_matches_fresh_and_is_idempotent(self):
        # Keep creation of a fresh store on the real CLI path as well as upgrades.
        self.db.unlink()
        self.run_cli('migrate')
        old = self.base / 'old.sqlite'
        schema = (ROOT / 'schema.sql').read_text().split('-- D7:')[0]
        with sqlite3.connect(old) as db:
            db.executescript(schema)
        self.run_cli('migrate', path=old)
        self.run_cli('migrate', path=old)

        def signature(path):
            with sqlite3.connect(path) as db:
                return db.execute('PRAGMA user_version').fetchone(), db.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()

        self.assertEqual(signature(self.db), signature(old))
        with store.connect(self.db, True) as db:
            rid = self.item()
            r = store.current(db, 'items', (rid,))
            r['kind'] = 'arbitrary_sql_kind'
            store.append(db, 'items', r, 'sql', 'test', 'owner')
        self.assertEqual(self.show(rid)['fields']['kind'], 'arbitrary_sql_kind')

    def test_read_and_single_verbs(self):
        p = self.run_cli('upsert', 'project', '--set', 'name=Demo project')[0]['records'][0]
        rid = self.item(summary='Uncut ' + 'x' * 400, project_id=p)
        person = self.run_cli('upsert', 'person', data={'display_name': 'Demo person'})[0][
            'records'
        ][0]
        meeting = self.run_cli('upsert', 'meeting', data={'title': 'Demo meeting'})[0]['records'][0]
        # Assignment is the assigned_to column, gated by a same-op commits citation.
        self.run_cli(
            'upsert',
            'item',
            '--id',
            rid[:8],
            '--set',
            'assigned_to=Demo person',
            '--cite',
            'gmail:demo/att/1:commits:Agreed to take it',
        )
        self.assertEqual(self.show(rid)['fields']['assigned_to'], 'Demo person')
        view = self.run_cli('view')
        self.assertIn('x' * 400, view)
        self.assertNotIn('x' * 400, self.run_cli('view', '--sparse'))
        self.assertLess(view.index('Demo project'), view.index('Synthetic item'))
        fields = self.run_cli('fields')
        kind = next(f for f in fields['items'] if f['field'] == 'kind')
        self.assertEqual(kind['vocabulary'], ['task', 'question'])
        self.assertEqual(kind['example'], 'task')
        self.run_cli(
            'upsert',
            'item',
            '--id',
            rid[:8],
            '--set',
            'body_markdown=Notes',
            'summary=null',
            '--cite',
            'gmail:demo/att/1:states:Evidence',
        )
        whole = self.show(rid)
        self.assertNotIn('summary', whole['fields'])
        self.assertEqual(whole['citations'][0]['evidence_qmd_event_id'], 'gmail:demo/att/1')
        self.run_cli('upsert', 'item', '--id', rid, '--set', 'assigned_to=')
        self.assertFalse(self.show(rid)['fields'].get('assigned_to'))
        self.run_cli('tombstone', meeting)
        self.assertEqual(self.show(meeting)['matches'], [])
        self.item('Synthetic duplicate')
        self.assertEqual(len(self.show('Synthetic')['matches']), 2)
        verbs = self.run_cli('verbs')
        self.assertGreaterEqual(len(verbs), 8)
        self.assertEqual(
            {v['name'] for v in verbs},
            {
                'migrate',
                'view',
                'show',
                'brief',
                'closures',
                'rules',
                'fields',
                'verbs',
                'upsert',
                'link',
                'end-link',
                'tombstone',
                'write',
                'snapshot',
                'decide',
                'status',
                'commit',
                'cursor',
                'archive-check',
                'archive-recover',
                'publish',
                'backup',
                'step',
                'steady',
                'runs',
                'export',
                'worklist',
                'contract',
                'intent',
                'inbox',
            },
        )
        # Read handles cannot write even when the filesystem permits it.
        with store.connect(self.db) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO change_sets VALUES ('x','x','x','agent',NULL,NULL,NULL)")

    def test_batch_errors_atomic_and_retry(self):
        rid = self.item()
        result = self.changes(
            [
                {
                    'id': 'valid',
                    'ops': [{'upsert': 'item', 'id': rid, 'set': {'body_markdown': 'landed'}}],
                },
                {
                    'id': 'bad',
                    'ops': [
                        {'upsert': 'item', 'id': rid, 'set': {'body_markdown': 'must roll back'}},
                        {
                            'upsert': 'item',
                            'id': rid,
                            'set': {'state': 'invalid', 'priority': 101, 'oops': True},
                        },
                    ],
                },
                {
                    'id': 'bad2',
                    'ops': [
                        {
                            'upsert': 'item',
                            'set': {
                                'title': 'bad',
                                'kind': 'memo',
                                'state': 'invalid',
                                'priority': -1,
                            },
                        }
                    ],
                },
                {
                    'id': 'bad3',
                    'ops': [{'upsert': 'project', 'set': {'name': 'bad', 'owner_person_id': rid}}],
                },
                {
                    'id': 'bad4',
                    'ops': [
                        {
                            'upsert': 'item',
                            'set': {'title': 'bad'},
                            'cite': [{'ref': 'not-a-qmd-ref'}],
                        }
                    ],
                },
            ],
            self.db,
            ok=False,
        )
        self.assertTrue(result[0]['ok'])
        self.assertTrue(all(not r['ok'] for r in result[1:]))
        self.assertEqual(
            {e['field'].split('.')[-1] for e in result[2]['errors']}, {'kind', 'state', 'priority'}
        )
        self.assertEqual(self.show(rid)['fields']['body_markdown'], 'landed')
        with store.connect(self.db) as db:
            self.assertFalse(
                db.execute("SELECT 1 FROM records WHERE change_set_id='bad2'").fetchone()
            )
            self.assertFalse(
                db.execute("SELECT 1 FROM change_sets WHERE change_set_id='bad'").fetchone()
            )
        self.changes(
            [{'id': 'bad', 'ops': [{'upsert': 'item', 'id': rid, 'set': {'priority': 55}}]}],
            self.db,
        )
        self.changes(
            [{'id': 'notes', 'why': 'not a draft', 'ops': [{'tombstone': rid}]}], self.db, ok=False
        )
        self.changes([{'id': 'bad', 'ops': [{'tombstone': rid}]}], self.db, ok=False)

    def test_snapshot_decisions_delta_commit_and_questions(self):
        rid = self.item(summary='original', body_markdown='base')
        peer = self.item('Peer')
        d = self.snapshot()
        with store.connect(d, True) as db:
            db.execute('INSERT INTO draft_ref VALUES (?,?)', ('m1', 'gmail:synthetic/att/1'))
        self.changes(
            [
                {
                    'id': 'a1',
                    'why': 'Rejected field',
                    'ops': [
                        {
                            'upsert': 'item',
                            'id': rid,
                            'set': {'summary': 'rejected'},
                            'cite': [{'ref': 'gmail:reject'}],
                        },
                        {'link': [rid, peer, 'rejected_link']},
                    ],
                },
                {
                    'id': 'a2',
                    'why': 'Keep body only',
                    'confidence': 0.8,
                    'importance': 0.6,
                    'ops': [
                        {
                            'upsert': 'item',
                            'id': rid,
                            'set': {'body_markdown': 'accepted'},
                            'cite': [{'ref': 'm1'}],
                        },
                        {'link': [rid, peer, 'relates_to']},
                    ],
                },
                {
                    # D14: no owner question; the unsettled field is left unassigned and says so.
                    'id': 'a3',
                    'why': 'Date unsettled by the evidence; left open with date options',
                    'ops': [
                        {
                            'upsert': 'item',
                            'set': {'title': 'Owner decision target', 'waiting_on': 'date options'},
                        }
                    ],
                },
            ],
            d,
        )
        self.run_cli('commit', str(d), '--into', str(self.db), ok=False)
        self.run_cli('decide', 'a2', 'amend', '--set', 'body_markdown=corrected', path=d, ok=False)
        self.decide(d, 'a1', 'reject', quote={'ref': 'gmail:reject', 'text': 'Do not do this'})
        self.decide(
            d,
            'a2',
            'amend',
            quote={'ref': 'm1', 'text': 'Correction'},
            set={'body_markdown': 'corrected'},
            by='reviewer',
        )
        self.decide(d, 'a3', 'accept')
        with store.connect(d) as db:
            r = db.execute(
                "SELECT actor,change_set_id FROM items WHERE verb='decide-amend'"
            ).fetchone()
            self.assertEqual(tuple(r), ('reviewer', 'a2'))
            columns = {c[1] for c in db.execute('PRAGMA table_info(draft_change)')}
            self.assertFalse(columns & {'needs_owner', 'question'})
        status = self.run_cli('status', '--brief', path=d)
        self.assertEqual(status['counts']['undecided'], 0)
        landed = self.run_cli('commit', str(d), '--into', str(self.db))
        self.assertEqual(landed['refused'], [])
        self.assertIn('skipped', landed['archive'])
        whole = self.show(rid)
        self.assertEqual(whole['fields']['summary'], 'original')
        self.assertEqual(whole['fields']['body_markdown'], 'corrected')
        self.assertEqual([c['source_key'] for c in whole['citations']], ['gmail:synthetic/att/1'])
        self.assertEqual([l['relation_kind'] for l in whole['links']], ['relates_to'])
        # D14: a commit never creates a question item, and its result has no questions key.
        self.assertNotIn('questions', landed)
        with store.connect(self.db) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM items WHERE kind='question'").fetchone()[0], 0
            )
        self.assertIsNone(self.run_cli('cursor'))  # F-53: a hand-landed draft never moves the pipeline cut
        with store.connect(self.db) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM items WHERE id=? AND change_set_id=?",
                (rid, landed['change_set_id']),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        self.run_cli('commit', str(d), '--into', str(self.db), ok=False)

    def test_multi_entity_amend_quote_atomicity_and_commit(self):
        rid = self.item('First', summary='base')
        peer = self.item('Second', summary='base')
        outside = self.item('Untouched')
        d = self.snapshot()
        self.changes(
            [
                {
                    'id': 'a1',
                    'ops': [
                        {
                            'upsert': 'item',
                            'id': r,
                            'set': {'summary': 'proposed'},
                            'cite': [{'ref': 'gmail:synthetic'}],
                        }
                        for r in (rid, peer)
                    ],
                }
            ],
            d,
        )
        quote = {'ref': 'gmail:synthetic', 'text': 'Direct correction'}
        corrections = {rid: {'summary': 'corrected'}, peer: {'summary': 'also corrected'}}
        # The same quote rule applies even when several records are corrected.
        for fields, q in [
            (corrections, None),
            ({rid: {'summary': 'must roll back'}, peer: {'unknown_field': 'bad'}}, quote),
            ({rid: {'summary': 'must roll back'}, outside: {'summary': 'not in change'}}, quote),
            ({rid: {'summary': 'must roll back'}, peer: {}}, quote),
            ({'summary': 'ambiguous flat form'}, quote),
        ]:
            with self.subTest(fields=fields, quote=q):
                payload = {'id': 'a1', 'decision': 'amend', 'set': fields}
                if q:
                    payload['quote'] = q
                result = self.run_cli('decide', path=d, data={'decisions': [payload]}, ok=False)
                self.assertFalse(result[0]['ok'])
                self.assertEqual(self.show(rid, d)['fields']['summary'], 'proposed')
                self.assertEqual(self.show(peer, d)['fields']['summary'], 'proposed')
        self.assertTrue(self.decide(d, 'a1', 'amend', quote=quote, set=corrections)[0]['ok'])
        self.run_cli('commit', str(d), '--into', str(self.db))
        self.assertEqual(self.show(rid)['fields']['summary'], 'corrected')
        self.assertEqual(self.show(peer)['fields']['summary'], 'also corrected')
        self.assertEqual(self.run_cli('status', path=d)['counts']['amend'], 1)

    def test_conflict_refuses_record_but_commits_rest(self):
        moved = self.item('Moved')
        stable = self.item('Stable')
        d = self.snapshot()
        self.changes(
            [
                {
                    'id': 'a1',
                    'ops': [{'upsert': 'item', 'id': moved, 'set': {'body_markdown': 'draft'}}],
                },
                {
                    'id': 'a2',
                    'ops': [{'upsert': 'item', 'id': stable, 'set': {'body_markdown': 'land'}}],
                },
            ],
            d,
        )
        for cid in ('a1', 'a2'):
            self.decide(d, cid, 'accept')
        self.run_cli('upsert', 'item', '--id', moved, data={'body_markdown': 'target moved'})
        landed = self.run_cli('commit', str(d), '--into', str(self.db))
        self.assertEqual([r['id'] for r in landed['refused']], [moved])
        self.assertEqual(landed['landed_changes'], ['a2'])
        self.assertEqual(self.show(moved)['fields']['body_markdown'], 'target moved')
        self.assertEqual(self.show(stable)['fields']['body_markdown'], 'land')

    def test_rejected_creation_dependency_and_local_citation_ids(self):
        rid = self.item('Existing')
        d = self.snapshot()
        new = str(uuid.uuid4())
        self.changes(
            [
                {
                    'id': 'a1',
                    'ops': [{'upsert': 'item', 'id': new, 'set': {'title': 'Rejected new'}}],
                },
                {
                    'id': 'a2',
                    'ops': [
                        {
                            'upsert': 'item',
                            'id': new,
                            'set': {'body_markdown': 'cannot land without creation'},
                        }
                    ],
                },
                {
                    'id': 'a3',
                    'ops': [
                        {
                            'upsert': 'item',
                            'id': rid,
                            'set': {'body_markdown': 'draft citation'},
                            'cite': [{'ref': 'wa:synthetic'}],
                        }
                    ],
                },
            ],
            d,
        )
        self.decide(d, 'a1', 'reject', quote={'ref': 'gmail:test', 'text': 'No'})
        self.decide(d, 'a2', 'accept')
        self.decide(d, 'a3', 'accept')
        # Independent target citation allocates the same local integer but changes no entity.
        with store.connect(self.db, True) as db:
            data = write.base('source_refs', 'owner') | {
                'source_ref_id': 1,
                'record_id': rid,
                'source_system': 'gmail',
                'source_key': 'gmail:target',
            }
            store.append(db, 'source_refs', data, 'target', 'test', 'owner')
        result = self.run_cli('commit', str(d), '--into', str(self.db))
        self.assertEqual([r['id'] for r in result['refused']], [new])
        self.assertEqual(len(self.show(rid)['citations']), 2)

    def test_link_deltas_hold_and_reviewer_additions(self):
        a, b = self.item('A'), self.item('B')
        self.run_cli('link', a, b, 'existing')
        d = self.snapshot()
        self.changes(
            [
                {'id': 'a1', 'ops': [{'end_link': [a, b, 'existing']}]},
                {'id': 'a2', 'ops': [{'link': [a, b, 'existing']}]},
                {
                    'id': 'a3',
                    'ops': [{'upsert': 'item', 'id': a, 'set': {'body_markdown': 'held'}}],
                },
                {'id': 'b1', 'ops': [{'upsert': 'item', 'set': {'title': 'Reviewer addition'}}]},
            ],
            d,
        )
        self.decide(d, 'a1', 'reject', quote={'ref': 'fathom:synthetic', 'text': 'Keep link'})
        self.decide(d, 'a2', 'accept')
        self.decide(d, 'a3', 'hold')
        self.decide(d, 'b1', 'accept', by='reviewer')
        r = self.run_cli('commit', str(d), '--into', str(self.db))
        self.assertEqual(r['refused'], [])
        self.assertEqual(len(self.show(a)['links']), 1)
        # A held change never lands: its body text is absent from the target.
        self.assertNotIn('body_markdown', self.show(a)['fields'])
        self.assertTrue(self.show('Reviewer addition')['fields'])

    def test_retired_links_refused_with_column_to_set(self):
        rid = self.item('Owned')
        person = self.run_cli('upsert', 'person', data={'display_name': 'Owner'})[0]['records'][0]
        project = self.run_cli('upsert', 'project', data={'name': 'Demo'})[0]['records'][0]
        d = self.snapshot()
        refused = self.changes(
            [
                {'id': 'p1', 'ops': [{'link': [rid, project, 'belongs_to_project']}]},
                {'id': 'p2', 'ops': [{'link': [rid, person, 'assigned_to_person']}]},
            ],
            d,
            ok=False,
        )
        self.assertFalse(any(r['ok'] for r in refused))
        self.assertIn('set project_id', refused[0]['errors'][0]['why'])
        self.assertIn('set assigned_to', refused[1]['errors'][0]['why'])
        self.assertEqual(self.run_cli('status', path=d)['changes'], [])

    def test_literal_newlines_rejected_at_write_boundary(self):
        with store.connect(self.db, True) as db:
            for kind, fields in [
                ('item', {'title': 'X', 'body_markdown': r'### Details\nText'}),
                ('project', {'name': 'X', 'summary': r'one\ntwo'}),
                ('meeting', {'title': 'X', 'body_markdown': r'one\ntwo'}),
                ('person', {'display_name': 'X', 'metadata_json': json.dumps({'note': r'one\ntwo'})}),
            ]:
                result = write.batch(db, {'changes': [{'ops': [{'upsert': kind, 'set': fields}]}]})[0]
                self.assertFalse(result['ok'], result)
                self.assertIn('literal backslash-n', str(result['errors']))
        rid = self.item(body_markdown='one\ntwo')
        self.assertEqual(self.show(rid)['fields']['body_markdown'], 'one\ntwo')
        with store.connect(self.db, True) as db:
            result = write.batch(db, {'changes': [{'ops': [{'upsert': 'person', 'set': {
                'display_name': 'X', 'metadata_json': json.dumps({'note': 'one\ntwo'})}}]}]})[0]
            self.assertTrue(result['ok'], result)

    def test_validation_tables_and_shapes(self):
        cases = [
            ({'upsert': 'unknown', 'set': {}}, 'upsert'),
            ({'upsert': [], 'set': {}}, 'upsert'),
            ({'upsert': 'item', 'set': {'title': 'X', 'state': 'bad'}}, 'state'),
            ({'upsert': 'item', 'set': {'title': 'X', 'kind': 'memo'}}, 'kind'),
            ({'upsert': 'item', 'set': {'title': 'X', 'priority': 101}}, 'priority'),
            (
                {'upsert': 'meeting', 'set': {'title': 'X', 'duration_minutes': -1}},
                'duration_minutes',
            ),
            ({'upsert': 'item', 'set': {'title': 'X', 'priority': '50'}}, 'priority'),
            ({'upsert': 'item', 'set': {'title': None}}, 'title'),
            ({'upsert': 'meeting', 'set': {'title': 'X', 'is_all_day': 2}}, 'is_all_day'),
            (
                {'upsert': 'person', 'set': {'display_name': 'X', 'metadata_json': '{'}},
                'metadata_json',
            ),
            (
                {'upsert': 'item', 'set': {'title': 'X'}, 'cite': [{'ref': 'gmail:demo/att/invalid space'}]},
                'ref',
            ),
            (
                {
                    'upsert': 'item',
                    'set': {'title': 'X'},
                    'cite': [{'ref': 'gmail:demo', 'role': 'no'}],
                },
                'role',
            ),
            ({'link': ['bad', 'bad', 'relates_to']}, 'id'),
        ]
        with store.connect(self.db, True) as db:
            for i, (op, field) in enumerate(cases):
                with self.subTest(op=op):
                    r = write.batch(db, {'changes': [{'id': f'bad{i}', 'ops': [op]}]})[0]
                    self.assertFalse(r['ok'])
                    self.assertIn(field, [e['field'].split('.')[-1] for e in r['errors']])
        rid = self.item()
        d = self.snapshot()
        for kw in (
            {'confidence': 1.1},
            {'importance': -0.1},
            # D14 refusals: an old prompt fails loudly rather than silently losing the question.
            {'needs_owner': True},
            {'needs_owner': True, 'question': 10},
            {'question': 'Which date?'},
        ):
            r = self.changes(
                [{'id': 'invalid_notes', **kw, 'ops': [{'upsert': 'item', 'id': rid, 'set': {}}]}],
                d,
                ok=False,
            )
            self.assertFalse(r[0]['ok'])
        retired = self.changes(
            [{'id': 'q1', 'needs_owner': True, 'question': 'Which date?', 'ops': []}], d, ok=False
        )
        self.assertIn('retired (D14)', retired[0]['errors'][0]['why'])

    def test_status_group_and_brief_budget(self):
        d = self.snapshot()
        changes = [
            {
                'id': f'a{i}',
                'why': 'Reason ' * 60,
                'grp': 'g1',
                'ops': [{'upsert': 'item', 'set': {'title': f'Synthetic {i}'}}],
            }
            for i in range(60)
        ]
        self.changes(changes, d)
        with store.connect(d) as db:
            data = draft.status(db, 'g1')
            brief = draft.status_text(data, True)
        self.assertLess(len(brief), 8000)
        self.assertLess(len(json.dumps(self.run_cli('status', '--brief', path=d))), 8000)
        self.assertEqual(len(data['undecided']), 60)
        self.assertEqual(self.run_cli('status', '--group', 'other', path=d)['changes'], [])

    def test_live_lock_archive_contract_on_synthetic_target(self):
        rid = self.item()
        d = self.snapshot()
        self.changes(
            [
                {
                    'id': 'a1',
                    'ops': [{'upsert': 'item', 'id': rid, 'set': {'body_markdown': 'accepted'}}],
                }
            ],
            d,
        )
        self.decide(d, 'a1', 'accept')
        receipt = self.base / 'archive.json'
        rclone = self.base / 'rclone'
        rclone.write_text((ROOT / 'tests/fake_rclone.py').read_text())
        rclone.chmod(0o700)
        # Only this synthetic target is live; the fake copies into a local directory.
        with (
            patch.object(store, 'LIVE', self.db),
            patch.dict(os.environ, {
                'PATH': str(self.base) + os.pathsep + os.environ.get('PATH', ''),
                'FAKE_REMOTE': str(self.base / 'remote'),
                'FAKE_RCLONE_LOG': str(receipt),
            }),
        ):
            result = draft.commit(d, self.db)
        self.assertEqual(result['archive'], 'B2')
        self.assertTrue((self.base / 'gsd.lock').exists())
        staged = self.base / 'archive/step.sqlite'
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual(staged.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(staged.read_bytes(), (self.base / 'remote/steps/step.sqlite').read_bytes())
        with store.connect(staged) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM draft_change').fetchone()[0], 1)
        args = json.loads(receipt.read_text())
        self.assertEqual(args[:3], ['copy', '--immutable', '--checksum'])
        self.assertEqual(args[-1], 'test_remote:example-backups/gsd/steps/')

    def test_no_implicit_writes_and_launcher_any_directory(self):
        env = dict(os.environ)
        env.pop('GSD_DB', None)
        p = subprocess.run(
            [str(ROOT / 'bin/gsd'), 'upsert', 'item', '--set', 'title=Never live'],
            input='',
            text=True,
            capture_output=True,
            env=env,
            cwd='/tmp',
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('writes require', p.stdout)
        p = subprocess.run(
            [str(ROOT / 'bin/gsd'), 'verbs', '--json'], capture_output=True, text=True, cwd='/tmp'
        )
        self.assertEqual(p.returncode, 0)
        self.assertGreaterEqual(len(json.loads(p.stdout)), 8)


if __name__ == '__main__':
    unittest.main()
