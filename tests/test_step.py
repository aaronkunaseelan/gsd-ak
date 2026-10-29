"""Private synthetic windows: CLI exports/dry packets and runner failure boundaries."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store, draft, export, write, step, packets, read
from gsd.capture import capture


class Step(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'base.sqlite'
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            store.migrate(db)
            store.change_set(db, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until='2025-12-30T00:00:00Z')
        self.env = dict(os.environ)
        self.env.pop('GSD_DB', None)

    def cli(self, *args):
        p = subprocess.run(
            [str(ROOT / 'bin/gsd'), *args, '--db', str(self.db), '--json'],
            text=True,
            capture_output=True,
            env=self.env,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def test_export_current_rows_and_sparse_navigation(self):
        with store.connect(self.db, True) as db:
            result = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'item',
                                    'set': {
                                        'title': 'Synthetic',
                                        'description': 'PRIVATE-LONG-DESCRIPTION',
                                        'body_markdown': 'LONG-BODY',
                                        'summary': 'navigation',
                                    },
                                }
                            ]
                        }
                    ]
                },
                'test',
            )
            rid = result[0]['records'][0]
        state = self.cli('export')
        self.assertEqual(set(state), set(export.TABLES))
        self.assertEqual(state['items'][0]['title'], 'Synthetic')
        self.assertNotIn('seq', state['items'][0])
        self.assertNotIn('op', state['items'][0])
        sparse = self.cli('view', '--sparse')
        self.assertIn('navigation', sparse)
        self.assertNotIn('PRIVATE-LONG-DESCRIPTION', sparse)
        self.assertNotIn('LONG-NOTES', sparse)
        self.assertNotIn('LONG-BODY', sparse)
        self.assertIn('LONG-BODY', self.cli('view'))
        self.cli('tombstone', rid)
        self.assertEqual(self.cli('export')['items'], [])

    def test_digest_and_delta_export(self):
        d = self.root / 'step.sqlite'
        draft.snapshot(self.db, d, 'synthetic', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        with store.connect(d, True) as db:
            write.batch(
                db,
                {
                    'changes': [
                        {
                            'id': 'a1',
                            'why': 'uncertain',
                            'confidence': 0.7,
                            'ops': [
                                {
                                    'upsert': 'item',
                                    'set': {'title': 'One'},
                                    'cite': [{'ref': 'gmail:synthetic'}],
                                }
                            ],
                        },
                        {
                            'id': 'a2',
                            'why': 'held',
                            'confidence': 0.9,
                            'ops': [{'upsert': 'item', 'set': {'title': 'Two'}}],
                        },
                        {
                            'id': 'a3',
                            'why': 'certain',
                            'confidence': 0.95,
                            'ops': [{'upsert': 'item', 'set': {'title': 'Three'}}],
                        },
                    ]
                },
                'A',
            )
            draft.decide(
                db,
                [
                    {'id': 'a1', 'decision': 'accept'},
                    {'id': 'a2', 'decision': 'hold'},
                    {'id': 'a3', 'decision': 'accept'},
                ],
                'B',
            )
            rows = export.changes(db)
        self.assertEqual(rows[0]['citations'][0]['source_key'], 'gmail:synthetic')
        self.assertTrue(rows[0]['deltas'])
        self.assertIsNone(rows[0]['deltas'][0]['before'])
        step.save(self.root / 'changes.jsonl', ''.join(json.dumps(r) + '\n' for r in rows))
        step.save(self.root / 'open.md', '## Open after this step\nUnresolved synthetic question')
        step.save(self.root / 'notes-g1.md', '## Disagreements\nSynthetic disagreement')
        digest = export.carry(self.root)
        self.assertNotIn('uncertain', digest)  # scores no longer select carry-over
        for text in [
            'held',
            'Unresolved synthetic question',
            'Synthetic disagreement',
            'Three',
        ]:
            self.assertIn(text, digest)

    def test_dry_real_cli_fake_qmd_ceiling_and_packets(self):
        executable = self.root / 'qmd'
        executable.write_text('''#!/usr/bin/env python3
import json, os, sys
assert 'QMD_UNTIL' not in os.environ
source = sys.argv[sys.argv.index('--source')+1]
print(json.dumps({'schema':'qmd.feed.v1','source':source,'items':[], 'next':None,'head':None,'complete':True}))
''')
        executable.chmod(0o700)
        self.env['PATH'] = str(self.root) + ':' + self.env['PATH']
        run = self.root / 'run'
        before = self.db.read_bytes()
        result = self.cli(
            'step',
            '--since',
            '2026-01-01T00:00:00Z',
            '--until',
            '2026-01-02T00:00:00Z',
            '--run',
            str(run),
            '--dry',
            '--no-tags',
        )
        self.assertEqual(result['state'], 'dry')
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        self.assertTrue((run / 'step.sqlite').is_file())
        a, b = (run / 'packet-a.md').read_text(), (run / 'packet-review.md').read_text()
        self.assertEqual(a.split('# role')[0], b.split('# role')[0])
        self.assertIn('# graph view', a.split('# role')[0])
        self.assertIn('# sparse window', a.split('# role')[0])
        for text in (a, b):
            positions = [
                text.index('# ' + name + '\n')
                for name in ['contract', 'graph view', 'sparse window', 'role', 'slice', 'task']
            ]
            self.assertEqual(positions, sorted(positions))
            self.assertIn('runner independently checks', text)
            self.assertNotIn('src.py', text)
            self.assertIn('everything the owner does', text)
        self.assertEqual(list(run.glob('agent-*.json')), [])
        shared = (run / 'shared.md').read_text()
        for name in ('a', 'review'):
            self.assertEqual(
                (run / f'packet-{name}.md').read_text(),
                shared + '\n\n' + (run / f'prompt-{name}.md').read_text(),
            )
        self.assertFalse((run / 'bootstrap.md').exists())
        self.assertEqual(json.loads((run / 'groups.json').read_text())['scopes'], {})
        self.assertIn('No scopes yet', b)
        self.assertNotIn('First cluster', b)
        minutes = self.root / 'minutes.md'
        minutes.write_text('Synthetic meeting: send the report.')
        meeting_run = self.root / 'dry-meeting'
        self.cli('step', '--since', '2026-01-01T00:00:00Z', '--until', '2026-01-02T00:00:00Z',
                 '--run', str(meeting_run), '--dry', '--no-tags', '--extra', str(minutes))
        plan = json.loads((meeting_run / 'groups.json').read_text())
        self.assertEqual(plan['groups'], {'r1': []})
        self.assertEqual(plan['scopes']['r1'][0]['kind'], 'meetings')
        self.assertIn("each of the window's meetings", (meeting_run / 'packet-review.md').read_text())
        self.assertEqual(list(meeting_run.glob('agent-*.json')), [])

    def test_people_scope_sees_earlier_reviewers_additions_as_context(self):
        d = self.root / 'context.sqlite'
        draft.snapshot(self.db, d, 'context', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        with store.connect(d, True) as db:
            changes = [{
                'id': cid, 'grp': group,
                'ops': [{'upsert': 'person', 'set': {'display_name': cid},
                         'cite': [{'ref': 'gmail:synthetic'}]}],
            } for cid, group in [('a1', 'r1'), ('b-r1-1', 'r1'), ('a2', 'r2')]]
            self.assertTrue(all(r['ok'] for r in write.batch(db, {'changes': changes}, 'r1')))
            draft.decide(db, [{'id': cid, 'decision': 'accept'} for cid in ('a1', 'b-r1-1')], 'r1')
            text = step.bundle(db, 'r2', people=True)
        own, context = text.split('## Already reviewed person changes')
        self.assertIn('### a2', own)
        self.assertNotIn('### a1', own)
        self.assertIn('### a1', context)
        self.assertIn('### b-r1-1', context)
        self.assertIn('a1 accept by r1', text)

    def test_bundle_changed_fields_cuts_sparse_records_no_source_reads(self):
        with store.connect(self.db, True) as db:
            rid = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'item',
                                    'set': {
                                        'title': 'Synthetic',
                                        'description': 'UNCHANGED-PRIVATE-BODY',
                                        'summary': 'old summary',
                                    },
                                }
                            ]
                        }
                    ]
                },
                'test',
            )[0]['records'][0]
        d = self.root / 'step.sqlite'
        draft.snapshot(self.db, d, 'synthetic', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        with store.connect(d, True) as db:
            write.batch(
                db,
                {
                    'changes': [
                        {
                            'id': 'a1',
                            'grp': 'g1',
                            'why': 'supported',
                            'confidence': 0.7,
                            'importance': 0.2,
                            'ops': [
                                {
                                    'upsert': 'item',
                                    'id': rid,
                                    'set': {
                                        'summary': 'new summary',
                                        'body_markdown': 'long changed value ' * 200,
                                    },
                                    'cite': [
                                        {
                                            'ref': 'gmail:synthetic',
                                            'role': 'states',
                                            'excerpt': 'evidence',
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
                'a',
            )
            text = step.bundle(db, 'g1')
            self.assertIn('old summary', text)
            self.assertIn('new summary', text)
            self.assertNotIn('needs_owner', text)
            self.assertNotIn('question:', text)
            self.assertIn('gmail:synthetic', text)
            self.assertIn('states', text)
            self.assertIn('evidence', text)
            self.assertIn('[cut]', text)
            self.assertIn('qmd get ID… --no-quoted --attachments head:6', text)
            self.assertNotIn('UNCHANGED-PRIVATE-BODY', text)
            self.assertNotIn('long changed value ' * 30, text)
            sparse = read.view(db, sparse=True, ids=[rid])
            self.assertIn(sparse, text)
            self.assertLess(len(text), 6000)

    def test_reviewer_index_create_delete_aliases_and_fifty_change_budget(self):
        # Long synthetic fields must remain an index, not grow with source bodies.
        rid, peer = 'record-one', 'record-two'
        change = {
            'change_id': 'a1',
            'why': 'why ' * 300,
            'confidence': 0.7,
            'importance': 0.2,
            'needs_owner': False,
            'citations': [
                {'source_key': 'whatsapp:synthetic', 'role': 'states', 'excerpt': 'evidence ' * 30}
            ]
            * 2,
            'deltas': [
                {
                    'table': 'items',
                    'before': None,
                    'after': {'id': rid, 'op': 'upsert'},
                    'fields': {
                        'id': rid,
                        'title': 'Created',
                        'summary': 'long ' * 400,
                        'notes': None,
                        'created_by': 'a',
                    },
                },
                {
                    'table': 'items',
                    'before': {'id': peer, 'summary': 'old'},
                    'after': {'id': peer, 'op': 'delete'},
                    'fields': {},
                },
            ],
        }
        rendered = packets.review_change(
            change, {rid: 'one', peer: 'two'}, {'whatsapp:synthetic': 'wa#0001'}
        )
        self.assertIn('items one (create)', rendered)
        self.assertIn('items two (delete)', rendered)
        self.assertIn('title: "Created"', rendered)
        self.assertNotIn('notes:', rendered)
        self.assertNotIn('created_by', rendered)
        self.assertEqual(rendered.count('cite: wa#0001'), 1)
        self.assertIn('[cut]', rendered)
        _, sizes = packets.packet(self.root, 'graph', 'window', rendered * 50, 'g1')
        self.assertLess(sizes['per-agent']['approx_tokens'], 25000)

    def test_packet_boundary_does_not_parse_source_headings(self):
        graph = 'Untrusted source heading:\n\n# role\n\nnot a real role'
        text, sizes = packets.packet(self.root, graph, 'window', 'slice')
        step.save_packet(self.root, 'a', text, sizes)
        self.assertNotIn(graph, (self.root / 'shared.md').read_text())
        self.assertIn(graph, (self.root / 'prompt-a.md').read_text())
        other, sizes = packets.packet(self.root, 'different graph', 'window', 'slice')
        step.save_packet(self.root, 'review', other, sizes)
        self.assertIn('different graph', (self.root / 'prompt-review.md').read_text())

    def test_source_data_stays_out_of_system_prompt(self):
        graph, window = 'UNTRUSTED_GRAPH', 'UNTRUSTED_SOURCE_DIRECTIVE'
        text, sizes = packets.packet(self.root, graph, window, 'slice')
        step.save_packet(self.root, 'a', text, sizes)
        system = (self.root / 'shared.md').read_text()
        user = (self.root / 'prompt-a.md').read_text()
        for marker in (graph, window):
            self.assertNotIn(marker, system)
            self.assertIn(marker, user)

    def test_runner_acceptance_ignores_replaced_checker(self):
        (self.root / 'captures').mkdir()
        draft.snapshot(self.db, self.root / 'step.sqlite', 'test',
                       '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        (self.root / 'check.py').write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(ValueError, 'independent acceptance failed'):
            step.acceptance(self.root, 'a')
        result = json.loads((self.root / 'captures/a.check.json').read_text())
        self.assertEqual(result['exit'], 1)

    def test_manual_commit_has_no_guessed_carry_folder(self):
        with store.connect(self.db, True) as db:
            db.execute(
                "INSERT INTO change_sets VALUES ('x','x','x','pipeline','manual','2026-01-01T00:00:00Z','Manual note')"
            )
            db.commit()
            self.assertIsNone(step.previous_folder(db))
            db.execute(
                "INSERT INTO change_sets VALUES ('runner','x','x','pipeline','older','2025-12-31T00:00:00Z','gsd-step-run:/private/runner')"
            )
            db.commit()
            self.assertEqual(step.previous_folder(db), Path('/private/runner'))
            restate = self.root / 'restate-run'
            restate.mkdir()
            (restate / 'manifest.json').write_text('{"mode": "restate"}')
            db.execute(
                "INSERT INTO change_sets VALUES ('restate','x','x','pipeline','newer','2026-01-02T00:00:00Z',?)",
                ('gsd-step-run:' + str(restate),),
            )
            db.commit()
            self.assertIsNone(step.previous_folder(db))

    def test_proposer_actor_preserves_quote_rule_with_wrong_id_prefix(self):
        d = self.root / 'step.sqlite'
        draft.snapshot(self.db, d, 'synthetic', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        with store.connect(d, True) as db:
            write.batch(
                db,
                {
                    'changes': [
                        {
                            'id': 'b1',
                            'ops': [
                                {
                                    'upsert': 'item',
                                    'set': {'title': 'Synthetic'},
                                    'cite': [{'ref': 'gmail:synthetic'}],
                                }
                            ],
                        }
                    ]
                },
                'a',
            )
            refused = draft.decide(db, [{'id': 'b1', 'decision': 'reject'}], 'reviewer')
            self.assertFalse(refused[0]['ok'])
            self.assertIn('quote', str(refused[0]['errors']))
            allowed = draft.decide(
                db,
                [
                    {
                        'id': 'b1',
                        'decision': 'reject',
                        'quote': {'ref': 'gmail:synthetic', 'text': 'Direct statement'},
                    }
                ],
                'reviewer',
            )
            self.assertTrue(allowed[0]['ok'])

    def test_capture_routes_pdf_docx_image_and_shared_summary(self):
        for name, expected_pages, count in [
            ('one.pdf', ['--pages', '4'], 4),
            ('one.docx', [], 301),
            ('one.png', None, 0),
        ]:
            with self.subTest(name=name):
                calls = []

                def qmd(*args, **kw):
                    calls.append(args)
                    if '--attachments' in args:
                        return {'id': 'gmail:test', 'text': ''}
                    if '--materialise' in args:
                        self.assertEqual(list(args[3:-1]), expected_pages)
                        return {'docs': [{'id': args[1], 'parts': [], 'pages': {'covered': count}}]}
                    return None

                selected = {
                    'kept_mail': [
                        {
                            'id': 'gmail:test',
                            'attachments': [{'id': 'gmail:test/att/1', 'name': name}]
                            + (
                                [{'id': 'gmail:test/att/2', 'name': 'next.pdf'}]
                                if count > 300
                                else []
                            ),
                            'parts': [],
                        }
                    ],
                    'docs': {},
                    'refs': {},
                    'meetings': [],
                    'views': {'mail.md': '', 'meetings.md': ''},
                    'manifest': {'meetings': {'kept': 0}},
                }
                ledger = capture(qmd, selected)
                self.assertEqual(ledger['pages_spent'], count)
                self.assertEqual(ledger['stopped_at_cap'], count > 300)
                self.assertEqual(
                    sum('--materialise' in c for c in calls), int(expected_pages is not None)
                )

        def shared(*args, **kw):
            if '--attachments' in args:
                return {'id': 'gmail:test', 'text': ''}
            if '--materialise' in args:
                return {
                    'docs': [
                        {
                            'id': 'link:synthetic',
                            'title': 'Shared meeting',
                            'parts': [
                                {
                                    'kind': 'attachment_text',
                                    'text': 'Summary: synthetic\nTranscript:\nDO NOT INLINE THIS TRANSCRIPT',
                                }
                            ],
                        }
                    ]
                }
            return None

        selected = {
            'kept_mail': [
                {
                    'id': 'gmail:test',
                    'parts': [
                        {'kind': 'authored', 'text': 'https://fathom.video/share/test-token'}
                    ],
                }
            ],
            'docs': {},
            'refs': {},
            'meetings': [],
            'views': {'mail.md': '', 'meetings.md': ''},
            'manifest': {'meetings': {'kept': 0}},
        }
        ledger = capture(shared, selected)
        self.assertEqual(ledger['shared_meetings'], 1)
        self.assertIn('Summary: synthetic', selected['views']['meetings.md'])
        self.assertNotIn('DO NOT INLINE', selected['views']['meetings.md'])
        self.assertEqual(selected['refs']['link:synthetic'], 'link:synthetic')

    def test_full_relay_addition_does_not_retrigger_and_commits(self):
        from types import SimpleNamespace

        qmd = self.root / 'qmd'
        qmd.write_text('''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
doc = {'id':'gmail:synthetic', 'source':'gmail', 'kind':'email',
       'time':'2026-01-01T12:00:00Z', 'from':'test@organisation.test', 'to':'owner@example.test',
       'title':'Synthetic source', 'parts':[{'kind':'authored', 'text':'Synthetic evidence'}]}
if args[0] == 'feed':
    source = args[args.index('--source')+1]
    item = dict(doc, token='1', item=doc['id'], source=source, time=1767268800,
                arrived=1767268800, thread=None, chat=None, version='v1', tombstone=False, reparse=False)
    items = [item] if source == 'gmail' else []
    print(json.dumps({'schema':'qmd.feed.v1', 'source':source, 'items':items,
                      'next':'1' if items else None, 'head':'1' if items else None, 'complete':True}))
else:
    print(json.dumps({'id':doc['id'], 'docs':[doc], 'text':''}))
''')
        qmd.chmod(0o700)
        ctl = self.root / 'claude'
        ctl.write_text(
            '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
GSD = __GSD__
a=sys.argv[1:]
root=pathlib.Path.cwd(); role=os.environ['GSD_ACTOR']
assert sys.stdin.read() == (root/('prompt-'+role+'.md')).read_text()
assert a[a.index('--effort')+1]=='medium'
assert os.environ['GSD_DB']==str(root/'step.sqlite')
if True:
    def cli(cmd, payload):
        p=subprocess.run([GSD,cmd,'--db',str(root/'step.sqlite'),'--json'],input=json.dumps(payload),text=True,capture_output=True)
        if p.returncode: raise RuntimeError(p.stdout)
    if role=='a':
        cli('write',{'changes':[{'id':'a1','why':'synthetic','confidence':.9,'importance':.2,'ops':[{'upsert':'item','set':{'title':'Synthetic task'},'cite':[{'ref':'gmail:synthetic'}]}]}]})
        (root/'open.md').write_text('Open after this step: none')
    elif role=='r1':
        cli('decide',{'decisions':[{'id':'a1','decision':'accept','by':'r1'}]})
        cli('write',{'changes':[{'id':'b-r1-1','grp':'r1','why':'plain arrival','confidence':.9,'importance':.2,'ops':[{'upsert':'person','set':{'display_name':'Synthetic person','status':'active'},'cite':[{'ref':'gmail:synthetic'}]}]}]})
        cli('decide',{'decisions':[{'id':'b-r1-1','decision':'accept','by':'r1'}]})
    if role!='a': (root/('notes-'+role+'.md')).write_text('No disagreements')
print(json.dumps({'type':'assistant','message':{'id':'call','model':'synthetic','usage':{'input_tokens':7},'content':[]}}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,
                  'structured_output': {'reflection': 'Changed work.' if role == 'a' else 'Missed a person.'}}))
'''.replace('__GSD__', repr(str(ROOT / 'bin/gsd')))
        )
        ctl.chmod(0o700)
        run = self.root / 'full'
        args = SimpleNamespace(
            since='2026-01-01T00:00:00Z',
            until='2026-01-02T00:00:00Z',
            run=str(run),
            db=str(self.db),
            dry=False,
            explicit_db=True,
            extra=[],
            a='synthetic',
            review='synthetic',
            tags=False,
        )
        env = {'PATH': str(self.root) + ':' + os.environ['PATH']}
        with patch.object(step, 'CLAUDE', str(ctl)), patch.dict(os.environ, env):
            result = step.run(args)
            args.since, args.until = '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z'
            args.run, args.dry = str(self.root / 'next'), True
            following = step.run(args)
        self.assertEqual(following['state'], 'dry')
        self.assertIn('Synthetic task', (self.root / 'next/views/prev-step.md').read_text())
        self.assertIn('## prev-step.md', (self.root / 'next/packet-a.md').read_text())
        self.assertEqual(result['state'], 'committed')
        self.assertEqual(result['changes'], 2)
        from gsd import steady
        from gsd.contract_validate import problems
        record = dict(links=[], steps=0, counts=dict.fromkeys(
            ('proposed', 'accepted', 'amended', 'rejected', 'held', 'records_landed'), 0),
            weighted_tokens=0, model_calls=0)
        steady.tally(record, run)
        self.assertEqual(record['reflections'], [
            dict(agent='a', text='Changed work.', step=run.name),
            dict(agent='r1', text='Missed a person.', step=run.name),
        ])
        contract = json.loads((ROOT / 'contracts/vendor/steward.record.v1.json').read_text())
        self.assertEqual(problems(contract, record), [])
        self.assertEqual(result['groups']['groups'], {'r1': ['a1', 'b-r1-1']})
        self.assertFalse((run / 'packet-people.md').exists())
        self.assertFalse(result['commit']['refused'])
        state = json.loads((run / 'state.json').read_text())
        self.assertEqual(len(state['items']), 1)
        self.assertEqual(len(state['people']), 1)
        with store.connect(self.db) as db:
            self.assertEqual(step.previous_folder(db), run)
        self.assertIn('Synthetic task', export.carry(run))

    def test_launch_timeout_kills_process_and_independent_check_is_required(self):
        for name, program in (
            ('timeout', 'import time; time.sleep(60)'),
            ('acceptance', 'import json; print(json.dumps({"type":"result","subtype":"success"}))'),
        ):
            run = self.root / name
            run.mkdir()
            (run / 'captures').mkdir()
            (run / 'shared.md').write_text('shared')
            (run / 'prompt-a.md').write_text('first message')
            draft.snapshot(self.db, run / 'step.sqlite', 'test',
                           '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
            ctl = run / 'claude'
            ctl.write_text('#!/usr/bin/env python3\n' + program + '\n')
            ctl.chmod(0o700)
            with patch.object(step, 'CLAUDE', str(ctl)):
                with self.assertRaisesRegex(ValueError, 'stopped'):
                    step.launch(run, 'a', 'synthetic', '2026-01-02T00:00:00Z', timeout=0.2)
            receipt = json.loads((run / 'agent-a.json').read_text())
            self.assertEqual(receipt['state'], 'failed')
            with self.assertRaises(ProcessLookupError):
                os.kill(receipt['pid'], 0)
            if name == 'acceptance':
                self.assertEqual(json.loads((run / 'captures/a.check.json').read_text())['exit'], 1)

    def test_read_only_launch_uses_real_prompt_but_never_step_acceptance(self):
        run = self.root / 'proof'
        run.mkdir()
        (run / 'captures').mkdir()
        (run / 'shared.md').write_text('shared')
        (run / 'prompt-a.md').write_text('REAL ROLE SLICE TASK')
        (run / 'check.py').write_text('raise SystemExit(1)')
        ctl = run / 'claude'
        ctl.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
user=sys.stdin.read()
assert user.endswith('REAL ROLE SLICE TASK') and user.startswith('CACHE PROOF ONLY')
a=sys.argv
assert a[a.index('--tools')+1]=='Bash,Read,Write,Edit'
assert pathlib.Path(a[a.index('--append-system-prompt-file')+1]).read_text()=='shared'
print(json.dumps({'type':'assistant','message':{'id':'proof','usage':{'input_tokens':2},'content':[]}}))
print(json.dumps({'type':'result','subtype':'success','is_error':False}))
''')
        ctl.chmod(0o700)
        with patch.object(step, 'CLAUDE', str(ctl)):
            calls = step.launch(run, 'a', 'synthetic', '2026-01-02T00:00:00Z', read_only=True)
        self.assertEqual(calls[0]['usage']['input_tokens'], 2)
        self.assertEqual(json.loads((run / 'agent-a.json').read_text())['state'], 'read-only-proof')
        self.assertFalse((run / 'captures/a.check.json').exists())

    def test_optional_reflection_is_bounded_in_existing_receipt(self):
        from gsd import reflections
        for name, value in [('a', 'Saw a change.'), ('r1', 'Missed a date.'),
                            ('a-long', 'x' * (reflections.MAX_CHARS + 100)), ('r2', None)]:
            run = self.root / name
            run.mkdir()
            (run / 'captures').mkdir()
            result = dict(type='result', subtype='success', is_error=False)
            if value is not None:
                result['structured_output'] = {reflections.FIELD: value}

            def headless(run, agent, args, *rest):
                schema = json.loads(args[args.index('--json-schema') + 1])
                self.assertEqual(schema, reflections.OUTPUT_SCHEMA)
                (run / 'captures' / f'{agent}.stream.jsonl').write_text(json.dumps(result))
                return 0

            with patch.object(step, 'headless', side_effect=headless), patch.object(step, 'acceptance'):
                step.launch(run, name, 'synthetic', '2026-01-02T00:00:00Z')
            receipt = json.loads((run / f'agent-{name}.json').read_text())
            self.assertEqual(receipt[reflections.FIELD], (value or '')[:reflections.MAX_CHARS])
            self.assertEqual(receipt['state'], 'proven')

    def test_usage_one_row_per_call(self):
        stream = self.root / 'stream.jsonl'
        events = []
        for mid, tool in [('first', False), ('first', True), ('second', False)]:
            events.append(
                {
                    'type': 'assistant',
                    'message': {
                        'id': mid,
                        'model': 'synthetic',
                        'usage': {'input_tokens': 17},
                        'content': [{'type': 'tool_use'}] if tool else [],
                    },
                }
            )
        events.append(
            {
                'type': 'result',
                'subtype': 'success',
                'usage': {
                    'iterations': [
                        {'type': 'message', 'input_tokens': 17, 'output_tokens': 99},
                        {'type': 'message', 'input_tokens': 17, 'output_tokens': 12},
                    ]
                },
            }
        )
        stream.write_text('\n'.join(json.dumps(e) for e in events))
        calls, result = step.usage(stream)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]['tool_use'])
        self.assertEqual(calls[0]['usage']['output_tokens'], 99)
        self.assertFalse(calls[1]['tool_use'])
        self.assertEqual(result['subtype'], 'success')

    def test_failed_launch_captured_and_never_retried(self):
        run = self.root / 'run'
        run.mkdir()
        (run / 'captures').mkdir()
        (run / 'shared.md').write_text('shared')
        (run / 'prompt-a.md').write_text('first message' * 20000)
        ctl = self.root / 'claude'
        ctl.write_text('''#!/usr/bin/env python3
import json, os, sys
assert len(sys.stdin.read()) > 131072
assert os.environ['QMD_UNTIL']=='2026-01-02T00:00:00Z'
assert os.environ['GSD_ACTOR']=='a'
assert os.environ['CLAUDE_CODE_DISABLE_AUTO_MEMORY']=='1'
assert os.environ['GSD_DB'].endswith('/step.sqlite')
print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True}))
print('private provider diagnostic', file=sys.stderr)
sys.exit(3)
''')
        ctl.chmod(0o700)
        with patch.object(step, 'CLAUDE', str(ctl)):
            with self.assertRaisesRegex(ValueError, 'do not retry blindly'):
                step.launch(run, 'a', 'synthetic-model', '2026-01-02T00:00:00Z')
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                step.launch(run, 'a', 'synthetic-model', '2026-01-02T00:00:00Z')
        self.assertEqual(json.loads((run / 'agent-a.json').read_text())['state'], 'failed')
        self.assertIn('private provider diagnostic', (run / 'captures/a.stderr.txt').read_text())
        self.assertEqual((run / 'captures/a.stream.jsonl').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
