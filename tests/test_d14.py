"""D14 migration, strict evidence gates and project re-statement on synthetic stores."""

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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store, write, step, read, draft, packets


class D14(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'base.sqlite'
        with sqlite3.connect(self.path) as db:
            store.migrate(db)

    def change(self, db, fields, rid=None, cites=None):
        op = {'upsert': 'item', 'set': fields, 'cite': cites or []}
        if rid:
            op['id'] = rid
        return write.batch(db, {'changes': [{'ops': [op]}]})[0]

    def test_migration_folds_all_current_values_and_is_idempotent(self):
        legacy = self.root / 'legacy.sqlite'
        rid = str(uuid.uuid4())
        with sqlite3.connect(legacy) as db:
            db.row_factory = sqlite3.Row
            db.executescript((ROOT / 'schema.sql').read_text())
            row = write.base('records', 'owner')
            row.update(id=rid, record_kind='item', canonical_title='Synthetic')
            store.append(db, 'records', row, 'old', 'port', 'owner')
            row = write.base('items', 'owner')
            row.update(
                id=rid,
                title='Synthetic',
                record_status='open',
                body_markdown='Prior',
                effort_band='M',
                notes='line one\nline two',
                estimated_minutes=0,
            )
            store.append(db, 'items', row, 'old', 'port', 'owner')
            db.commit()
            counts = store.migrate(db)
            self.assertEqual(
                {k: v for k, v in counts.items() if v},
                {'record_status': 1, 'effort_band': 1, 'notes': 1, 'estimated_minutes': 1},
            )
            current = store.current(db, 'items', (rid,))
            self.assertTrue(current['body_markdown'].startswith('Prior\nRetired fields:'))
            self.assertIn('effort_band="M"', current['body_markdown'])
            self.assertIn('line one\\nline two', current['body_markdown'])
            self.assertEqual(current['change_set_id'], 'migration:d14')
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 2)
            self.assertFalse(set(store.RETIRED_ITEMS) & store.columns(db, 'items').keys())
            self.assertIn('aliases', store.columns(db, 'people'))
            self.assertNotIn('other_emails', store.columns(db, 'people'))
            maximum = store.maxima(db)
            self.assertFalse(any(store.migrate(db).values()))
            self.assertEqual(store.maxima(db), maximum)

    def test_terminal_transitions_need_decides_in_same_op(self):
        with store.connect(self.path, True) as db:
            rid = self.change(db, {'title': 'Open'})['records'][0]
            for state in ('done', 'canceled'):
                for cite in (
                    [],
                    [{'ref': 'gmail:test', 'role': 'states', 'excerpt': 'yes'}],
                    [{'ref': 'gmail:test', 'role': 'decides', 'excerpt': '  '}],
                ):
                    result = self.change(db, {'state': state}, rid, cite)
                    self.assertFalse(result['ok'])
                    self.assertIn('leave the item open', result['errors'][0]['why'])
                self.assertTrue(
                    self.change(
                        db,
                        {'state': state},
                        rid,
                        [{'ref': 'gmail:test', 'role': 'decides', 'excerpt': 'Close it'}],
                    )['ok']
                )
                self.assertTrue(self.change(db, {'summary': 'unchanged terminal state'}, rid)['ok'])
                self.assertTrue(self.change(db, {'state': 'ready'}, rid)['ok'])

    def test_assignment_gate_accepts_both_roles_and_clearing_is_free(self):
        with store.connect(self.path, True) as db:
            rid = self.change(db, {'title': 'Unassigned'})['records'][0]
            for role in ('commits', 'decides'):
                result = self.change(db, {'assigned_to': 'Candidate'}, rid)
                self.assertFalse(result['ok'])
                self.assertIn('waiting_on', result['errors'][0]['why'])
                self.assertFalse(
                    self.change(
                        db, {'assigned_to': 'Candidate'}, rid, [{'ref': 'gmail:test', 'role': role}]
                    )['ok']
                )
                self.assertTrue(
                    self.change(
                        db,
                        {'assigned_to': 'Candidate'},
                        rid,
                        [{'ref': 'gmail:test', 'role': role, 'excerpt': 'I will do it'}],
                    )['ok']
                )
                self.assertTrue(self.change(db, {'assigned_to': 'Candidate'}, rid)['ok'])
                self.assertTrue(self.change(db, {'assigned_to': None}, rid)['ok'])
            self.assertFalse(self.change(db, {'title': 'New', 'assigned_to': 'Candidate'})['ok'])
            self.assertFalse(self.change(db, {'title': 'New', 'state': 'done'})['ok'])

    def test_retired_payload_and_links_fail_loudly(self):
        with store.connect(self.path, True) as db:
            rid = self.change(db, {'title': 'Unassigned'})['records'][0]
            for f in ('needs_owner', 'question'):
                result = write.batch(
                    db,
                    {'changes': [{f: False, 'ops': [{'upsert': 'item', 'set': {'title': 'No'}}]}]},
                )
                self.assertFalse(result[0]['ok'])
                self.assertIn('retired', result[0]['errors'][0]['why'])
            for relation, column in [
                ('belongs_to_project', 'project_id'),
                ('assigned_to_person', 'assigned_to'),
            ]:
                result = write.batch(db, {'changes': [{'ops': [{'link': [rid, rid, relation]}]}]})
                self.assertFalse(result[0]['ok'])
                self.assertIn(column, result[0]['errors'][0]['why'])
            self.assertTrue(self.change(db, {'title': 'Direct question', 'kind': 'question'})['ok'])

    def test_meeting_project_links_remain_writable_readable_and_seed_restate(self):
        with store.connect(self.path, True) as db:
            result = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'project',
                                    'set': {'name': 'Project', 'status': 'active'},
                                },
                                {
                                    'upsert': 'meeting',
                                    'set': {'title': 'Meeting'},
                                    'cite': [{'ref': 'fathom:synthetic:123'}],
                                },
                            ]
                        }
                    ]
                },
            )[0]
            project, meeting = result['records']
            linked = write.batch(
                db, {'changes': [{'ops': [{'link': [meeting, project, 'belongs_to_project']}]}]}
            )[0]
            self.assertTrue(linked['ok'])
            self.assertIn('belongs_to_project', read.view(db))
            self.assertEqual(read.show(db, [meeting])[0]['links'][0]['to_record_id'], project)
            task = step.restate_agents(db)[0]['task']
            self.assertIn(meeting, task)
            self.assertIn('fathom:synthetic:123', task)

    def test_restate_dry_no_qmd_selection_one_agent_per_active_project(self):
        with store.connect(self.path, True) as db:
            result = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {'upsert': 'project', 'set': {'name': 'First', 'status': 'active'}},
                                {
                                    'upsert': 'project',
                                    'set': {'name': 'Second', 'status': 'active'},
                                },
                                {'upsert': 'project', 'set': {'name': 'Closed', 'status': 'done'}},
                                {
                                    'upsert': 'person',
                                    'set': {
                                        'display_name': 'Synthetic Person',
                                        'aliases': '["person@example.test"]',
                                    },
                                },
                            ]
                        }
                    ]
                },
            )
            self.assertTrue(result[0]['ok'])
        run = self.root / 'run'
        a = SimpleNamespace(
            since='2026-07-16T00:00:00Z',
            until='2026-08-13T00:00:00Z',
            dry=True,
            explicit_db=True,
            db=self.path,
            a='test',
            review='test',
            run=run,
            restate=True,
            extra=[],
        )
        with (
            patch('gsd.selection.select', side_effect=AssertionError('must not select')),
            patch('gsd.step.launch', side_effect=AssertionError('dry must not launch')),
        ):
            result = step.run(a)
        manifest = result['manifest']
        self.assertEqual(manifest['mode'], 'restate')
        self.assertEqual(len(manifest['agents']), 2)
        shared = (run / 'shared.md').read_text()
        self.assertIn('aliases:', shared)
        self.assertNotIn('# sparse window', shared)
        for name in manifest['agents']:
            self.assertTrue(name.startswith('a-'))
            user = (run / f'prompt-{name}.md').read_text()
            self.assertIn('Record ids:', user)
            self.assertIn('prior and unverified', user)
            self.assertIn('at most three', user)
            self.assertEqual((run / f'packet-{name}.md').read_text(), shared + '\n\n' + user)
        self.assertEqual((run / 'packet-review.md').read_text().split('# role')[0], shared + '\n\n')
        self.assertNotIn(
            'needs_owner', store.columns(store.connect(run / 'step.sqlite'), 'draft_change')
        )

    @unittest.skipUnless((ROOT / 'port').is_dir(), 'private legacy importer not included')
    def test_base_scripts_real_cli_with_synthetic_qmd(self):
        with store.connect(self.path, True) as db:
            result = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {'upsert': 'person', 'set': {'display_name': 'Synthetic Sender'}},
                                {'upsert': 'project', 'set': {'name': 'Synthetic Project'}},
                            ]
                        }
                    ]
                },
            )[0]
            person, project = result['records']
            item = self.change(db, {'title': 'Synthetic Item', 'project_id': project})['records'][0]
            db.execute('BEGIN IMMEDIATE')
            store.change_set(db, 'legacy', 'port', 'port')
            data = write.base('record_links', 'port')
            data.update(
                from_record_id=item, to_record_id=project, relation_kind='belongs_to_project'
            )
            write.insert(db, 'record_links', data, 'legacy', 'port', 'port')
            data = write.base('source_refs', 'port')
            data.update(
                source_ref_id=1, record_id=item, source_system='gmail', source_key='account/msg'
            )
            write.insert(db, 'source_refs', data, 'legacy', 'port', 'port')
            db.commit()
        qmd = self.root / 'qmd'
        qmd.write_text('''#!/usr/bin/env python3
import json, os, sys
assert os.environ['QMD_UNTIL'] == '2026-08-13T10:10:37Z'
a=sys.argv[1:]
if a[0]=='browse':
    source=a[a.index('--source')+1]
    rows=[{'id':'gmail:account:msg','from':'Synthetic Sender <sender@example.test>'}] if source=='gmail' else []
    print(json.dumps({'results':rows,'cursor':None}))
else:
    ids=[]
    for x in a[1:]:
        if x.startswith('--'): break
        ids.append(x)
    rows=[{'id':i,'text':'Kind: message\\nFrom: Synthetic Sender <sender@example.test>\\n---\\nBody','docs':[]} for i in ids]
    print(json.dumps(rows[0] if len(rows)==1 else rows) if '--json' in a else 'Verified')
''')
        qmd.chmod(0o700)
        env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'])
        for script in ('rekey', 'aliases', 'dedupe_links'):
            with store.connect(self.path) as db:
                before = store.maxima(db)
            for dry in (True, False):
                command = [
                    sys.executable,
                    '-I',
                    str(ROOT / 'port' / (script + '.py')),
                    str(self.path),
                ]
                result = subprocess.run(
                    command + (['--dry'] if dry else []), env=env, text=True, capture_output=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertNotIn('sender@example.test', result.stdout)
                self.assertEqual(report['mode'], 'dry' if dry else 'apply')
                if dry:
                    with store.connect(self.path) as db:
                        self.assertEqual(store.maxima(db), before)
        with store.connect(self.path) as db:
            self.assertEqual(store.rows(db, 'record_links'), [])
            self.assertEqual(
                json.loads(store.current(db, 'people', (person,))['aliases']),
                ['sender@example.test'],
            )
            self.assertEqual(
                store.current(db, 'source_refs', (1,))['evidence_qmd_event_id'], 'gmail:account:msg'
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM change_sets WHERE origin='port' AND note LIKE 'port/%'"
                ).fetchone()[0],
                3,
            )

    def test_restate_execute_uses_dynamic_sequential_reviewers(self):
        with store.connect(self.path, True) as db:
            write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {'upsert': 'project', 'set': {'name': 'One', 'status': 'active'}},
                                {'upsert': 'project', 'set': {'name': 'Two', 'status': 'active'}},
                            ]
                        }
                    ]
                },
            )
        run = self.root / 'execute'
        a = SimpleNamespace(
            since='2026-07-16T00:00:00Z',
            until='2026-08-13T00:00:00Z',
            dry=False,
            explicit_db=True,
            db=self.path,
            a='test',
            review='test',
            run=run,
            restate=True,
            extra=[],
        )
        with store.connect(self.path) as db:
            projects = {p['name']: p['id'] for p in store.rows(db, 'projects')}
        calls = []

        def launch(folder, name, model, until, **kw):
            calls.append(name)
            if name.startswith('a-'):
                with store.connect(folder / 'step.sqlite', True) as db:
                    project = projects['One' if name.startswith('a-one-') else 'Two']
                    count = 32 if name.startswith('a-one-') else 29
                    changes = [{
                        'id': name + f'-{i}', 'why': 'verified',
                        'ops': [{'upsert': 'item',
                                 'set': {'title': f'Synthetic {i}', 'project_id': project},
                                 'cite': [{'ref': 'gmail:synthetic'}]}],
                    } for i in range(count)]
                    if name.startswith('a-two-'):
                        changes += [{
                            'id': name + '-' + kind,
                            'ops': [{'upsert': kind, 'set': fields,
                                     'cite': [{'ref': 'gmail:synthetic'}]}],
                        } for kind, fields in [('person', {'display_name': 'Pat'}),
                                                ('meeting', {'title': 'Meeting'})]]
                    result = write.batch(db, {'changes': changes}, name)
                    self.assertTrue(all(c['ok'] for c in result))
                step.save(folder / f'open-{name}.md', 'Prior and unverified: none')
            else:
                with store.connect(folder / 'step.sqlite', True) as db:
                    ids = [
                        r[0]
                        for r in db.execute(
                            'SELECT change_id FROM draft_change WHERE grp=?', (name,)
                        )
                    ]
                    draft.decide(db, [{'id': cid, 'decision': 'accept'} for cid in ids], name)
                step.save(folder / f'notes-{name}.md', 'Checked')
            return []

        with patch('gsd.step.launch', side_effect=launch):
            result = step.run(a)
        self.assertEqual(result['state'], 'committed')
        self.assertEqual(calls[-2:], ['r1', 'r2'])
        self.assertEqual(len(calls), 4)
        self.assertEqual([len(v) for v in result['groups']['groups'].values()], [32, 31])
        self.assertEqual([s['kind'] for s in result['groups']['scopes']['r2']], ['projects', 'meetings', 'people'])
        self.assertIn('Before: r1 (One)', (run / 'packet-r2.md').read_text())
        self.assertIn('accept by r1', (run / 'packet-r2.md').read_text())
        self.assertTrue((run / 'open.md').is_file())
