"""Synthetic tagging, source preservation and a real-process tagged review relay."""

from collections import Counter
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
from gsd import db as store, packets, step, tags, write


def state(n=4):
    graph = {t: [] for t in store.TABLES}
    graph['projects'] = [
        {'id': f'p{i}', 'name': f'Project {i}', 'status': 'active'} for i in range(n)
    ]
    return graph


def doc(ref, source='gmail', at='2026-01-02T00:00:00Z', thread='thread', text='Synthetic evidence'):
    return {
        'id': ref,
        'source': source,
        'time': at,
        'thread': thread,
        'from': 'Pat <pat@example.org>',
        'title': 'Synthetic subject',
        'parts': [{'kind': 'authored', 'text': text}],
    }


def selected(docs):
    return {
        'docs': {d['id']: d for d in docs},
        'refs': {d['id']: d['id'] for d in docs},
        'meetings': [d for d in docs if d['source'] in ('fathom', 'file')],
        'views': {'whatsapp.md': ''},
        'mail_entries': [],
    }


def unit(uid, *projects):
    return {
        'id': uid,
        'kind': 'mail',
        'tags': [{'project_id': p, 'p': round(0.8 - i * 0.1, 2)} for i, p in enumerate(projects)],
        'source_ids': [uid + '-source'],
        'member_refs': [uid + '-ref'],
        'p_people_event': 0,
        'p_none': 0,
        'sparse': 'source-' + uid,
        'at': uid,
        'text': 'Synthetic',
    }


def data(units, n=4):
    return {'cards': state(n)['projects'], 'units': units, 'aliases': {}}


def change(cid, rid=None, ref=None):
    return {
        'change_id': cid,
        'records': [{'id': rid}] if rid else [],
        'deltas': [],
        'citations': [{'source_key': ref}] if ref else [],
        'decision': 'accept',
    }


class Tags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'captures').mkdir()

    def test_every_selected_source_once_and_attachments_with_parent(self):
        mail = doc('gmail:account:one')
        mail['attachments'] = [{'id': mail['id'] + '/att/1'}]
        att = doc(mail['id'] + '/att/1')
        second = doc('gmail:account:two')
        wa = doc('wa:one', 'whatsapp')
        meeting = doc(
            'fathom:account:one',
            'fathom',
            text='# Decisions\nDecided.\n# Actions\n- [ ] Send report\n- [ ] Review report',
        )
        pack = selected([mail, att, second, wa, meeting])
        pack['refs']['att#0001'] = att['id']
        units, aliases = tags.make_units(pack)
        counts = Counter(ref for u in units for ref in u['source_ids'])
        self.assertEqual(counts, Counter({ref: 1 for ref in pack['docs']}))
        owner = next(u for u in units if mail['id'] in u['source_ids'])
        self.assertIn(att['id'], owner['source_ids'])
        self.assertIn('att#0001', owner['member_refs'])
        self.assertIn(second['id'], owner['source_ids'])
        self.assertGreaterEqual(len(aliases[meeting['id']]), 3)
        for section in (u for u in units if u['kind'] == 'meeting'):
            self.assertIn(meeting['id'], section['sparse'])
        for body in ('Decided.', 'Send report', 'Review report'):
            self.assertEqual(sum(u['sparse'].count(body) for u in units), 1)
        self.assertEqual(
            len(
                tags.citation_units(
                    change('x', ref=meeting['id']), {'units': units, 'aliases': aliases}
                )
            ),
            len(aliases[meeting['id']]),
        )

    def test_selected_shared_recording_stays_visible_inside_parent(self):
        parent = doc('gmail:a:one')
        shared = doc(
            'gmail:a:one/att/link-shared',
            'fathom',
            text='Shared meeting decision\nTranscript:\nHIDDEN',
        )
        parent['attachments'] = [{'id': shared['id']}]
        units, _ = tags.make_units(selected([parent, shared]))
        self.assertEqual(len(units), 1)
        self.assertEqual(set(units[0]['source_ids']), {parent['id'], shared['id']})
        self.assertIn(shared['id'], units[0]['sparse'])
        self.assertIn('Shared meeting decision', units[0]['sparse'])
        self.assertNotIn('HIDDEN', units[0]['sparse'])

    def test_burst_gap_boundary_and_previous_three_lines_not_members(self):
        docs = [
            doc(f'wa:{i}', 'whatsapp', at=at, text=f'line {i}')
            for i, at in enumerate(
                [
                    '2026-01-02T00:00:00Z',
                    '2026-01-02T01:00:00Z',
                    '2026-01-02T02:00:00Z',
                    '2026-01-02T05:00:00Z',
                    '2026-01-02T08:00:01Z',
                ]
            )
        ]
        units, _ = tags.make_units(selected(docs))
        self.assertEqual([len(u['source_ids']) for u in units], [4, 1])
        context = units[-1]['text'].split('THIS BURST:')[0]
        self.assertNotIn('line 0', context)
        for i in (1, 2, 3):
            self.assertIn(f'line {i}', context)
            self.assertNotIn(f'wa:{i}', units[-1]['member_refs'])

    def test_account_local_mail_thread_keys(self):
        a, b = doc('gmail:a:one'), doc('gmail:b:two')
        units, _ = tags.make_units(selected([a, b]))
        self.assertEqual(len(units), 2)
        self.assertNotEqual(tags.route_key(a), tags.route_key(b))

    def test_cards_historical_refs_and_unique_sticky_routes(self):
        graph = state(2)
        graph['items'] = [
            {
                'id': 'item',
                'project_id': 'p0',
                'title': 'Open task',
                'state': 'open',
                'assigned_to': 'person',
            }
        ]
        graph['people'] = [
            {'id': 'person', 'display_name': 'Pat Synthetic', 'aliases': '["pat@example.org"]'}
        ]
        a = doc('gmail:a:old', at='2026-01-01T00:00:00Z')
        b = doc('gmail:a:future', at='2026-01-03T00:00:00Z', thread='future')
        graph['source_refs'] = [
            {'record_id': 'item', 'evidence_qmd_event_id': a['id'], 'at': '2099-01-01'},
            {'record_id': 'p1', 'evidence_qmd_event_id': b['id']},
        ]

        def qmd(*args, **kw):
            return {'docs': [a, b]}

        cards, routes = tags.cards_and_routes(graph, qmd, '2026-01-02T00:00:00Z')
        self.assertEqual(routes, {tags.route_key(a): 'p0'})
        self.assertEqual(cards[0]['open_items'], ['Open task'])
        self.assertEqual(cards[0]['people'][0]['aliases'], ['pat@example.org'])
        self.assertEqual(cards[0]['cited_chats_and_subjects'], ['Synthetic subject'])
        graph['source_refs'].append({'record_id': 'p1', 'source_key': a['id']})
        self.assertEqual(tags.cards_and_routes(graph, qmd, '2026-01-02T00:00:00Z')[1], {})

    def test_group_chats_are_never_sticky(self):
        graph = state(2)
        group = doc(
            'wa:acct:team@g.us:m1', 'whatsapp', at='2026-01-01T00:00:00Z', thread='team@g.us'
        )
        one = doc(
            'wa:acct:pat@s.whatsapp.net:m2',
            'whatsapp',
            at='2026-01-01T00:00:00Z',
            thread='pat@s.whatsapp.net',
        )
        graph['source_refs'] = [
            {'record_id': 'p0', 'evidence_qmd_event_id': group['id']},
            {'record_id': 'p0', 'evidence_qmd_event_id': one['id']},
        ]

        def qmd(*args, **kw):
            return {'docs': [group, one]}

        routes = tags.cards_and_routes(graph, qmd, '2026-01-02T00:00:00Z')[1]
        self.assertEqual(routes, {tags.route_key(one): 'p0'})

    def test_sticky_skips_model_and_p_is_one(self):
        pack = selected([doc('gmail:a:one')])
        with (
            patch.object(
                tags,
                'cards_and_routes',
                return_value=(
                    state(1)['projects'],
                    {tags.route_key(pack['docs']['gmail:a:one']): 'p0'},
                ),
            ),
            patch.object(tags, 'haiku') as call,
        ):
            result = tags.tag(self.root, pack, state(1), None, '2026-01-02')
        call.assert_not_called()
        self.assertEqual(result['sticky'], 1)
        self.assertEqual(result['units'][0]['tags'], [{'project_id': 'p0', 'p': 1.0}])
        self.assertEqual(result['units'][0]['source'], 'sticky')
        self.assertEqual((self.root / 'tags.json').stat().st_mode & 0o777, 0o600)

    def test_threshold_independent_probabilities_top_two_and_people(self):
        pack = selected([doc('gmail:a:one')])
        rows = [
            {
                'id': 'mail-001',
                'projects': {'p0': 0.06, 'p1': 0.9, 'p2': 0.8, 'p3': 0.05},
                'p_none': 0.1,
                'p_people_event': 0.7,
                'reason': 'synthetic',
            }
        ]
        with (
            patch.object(tags, 'cards_and_routes', return_value=(state()['projects'], {})),
            patch.object(tags, 'haiku', return_value=(rows, [{'usage': {'output_tokens': 100, 'input_tokens': 10, 'cache_creation_input_tokens': 8, 'cache_read_input_tokens': 20}, 'cost_usd': 0}])), 
        ):
            result = tags.tag(self.root, pack, state(), None, '2026-01-02')
        self.assertEqual([t['project_id'] for t in result['units'][0]['tags']], ['p1', 'p2'])
        self.assertIn('; people', tags.grouped(result)[0])
        self.assertEqual(result['weighted_tokens'], 122)
        from gsd import steady
        (self.root / 'tags-calls.json').write_text(json.dumps(result['calls']))
        self.assertEqual(steady.usage(self.root), (122, 1))

    def test_group_each_unit_once_project_order_chronological_and_untagged(self):
        units = [unit('z', 'p1'), unit('a', 'p0', 'p1'), unit('x'), unit('b', 'p0')]
        text, sizes = tags.grouped(data(units))
        self.assertLess(text.index('Project 0'), text.index('Project 1'))
        self.assertLess(text.index('unit a '), text.index('unit b '))
        self.assertIn('No project tagged (read in full)', text)
        for u in units:
            self.assertEqual(text.count('source-' + u['id']), 1)
        self.assertEqual(sizes['untagged'], 1)
        self.assertEqual(sum(sizes.values()), 4)

    def test_touched_projects_keeps_old_and_new_homes(self):
        graph = state(2)
        c = change('a')
        c['deltas'] = [{
            'table': 'items',
            'before': {'id': 'i', 'project_id': 'p1'},
            'after': {'id': 'i', 'project_id': 'p0'},
        }]
        self.assertEqual(tags.touched_projects(c, graph), {'p0', 'p1'})

    def test_meeting_trigger_window_and_changed_record_only(self):
        graph = state()
        self.assertEqual(tags.meeting_refs([doc('gmail:a:x')], [], graph), [])
        for source in ('fathom', 'file'):
            self.assertEqual(tags.meeting_refs([doc('ref', source)], [], graph), ['ref'])
        graph['meetings'] = [{'id': 'meeting'}]
        self.assertIn('record:meeting', tags.meeting_refs([], [change('a', 'meeting')], graph))
        c = change('a')
        c['deltas'] = [{'table': 'meetings', 'after': {'id': 'new'}}]
        self.assertTrue(tags.meeting_refs([], [c], state()))

    def test_misroutes_only_landed_project_changes_and_meeting_aliases(self):
        d = data([unit('a', 'p0', 'p1'), unit('b', 'p0'), unit('c')], 2)
        changes = [change('a1', 'p0', 'a-ref'), change('a2', 'p0', 'b-ref')]
        changes[1]['decision'] = 'hold'
        suspects = tags.misroutes(d, changes, state(2))
        self.assertEqual(
            suspects,
            [
                {'id': 'a', 'tags': [{'project_id': 'p1', 'p': 0.7}]},
                {'id': 'b', 'tags': [{'project_id': 'p0', 'p': 0.8}]},
            ],
        )
        d['aliases']['meeting'] = ['a', 'b']
        changes = [change('a3', 'p0', 'meeting'), change('a4', 'p1', 'a-ref')]
        self.assertEqual(tags.misroutes(d, changes, state(2)), [])

    def test_refused_commit_changes_remain_misroute_suspects(self):
        d = data([unit('a', 'p0')], 1)
        c = change('a1', 'p0', 'a-ref')
        landed = {'landed_records': [], 'landed_changes': []}
        self.assertEqual(len(tags.misroutes(d, [c], state(1), landed)), 1)
        landed = {'landed_records': ['p0'], 'landed_changes': ['a1']}
        self.assertEqual(tags.misroutes(d, [c], state(1), landed), [])

    def test_empty_active_cards_preserve_untagged_units_without_model(self):
        with (
            patch.object(tags, 'cards_and_routes', return_value=([], {})),
            patch.object(tags, 'haiku') as provider,
        ):
            result = tags.tag(
                self.root, selected([doc('gmail:a:one')]), state(0), None, '2026-01-02'
            )
        provider.assert_not_called()
        self.assertEqual(len(result['units']), 1)
        self.assertEqual(result['units'][0]['tags'], [])
        self.assertIn('No project tagged (read in full)', tags.grouped(result)[0])

    def test_meeting_sections_keep_context_but_not_transcript(self):
        meeting = doc(
            'fathom:a:one',
            'fathom',
            text='# Meeting Purpose\nSynthetic purpose\n# Decisions\nDecided.\n# Transcript\nHIDDEN TRANSCRIPT',
        )
        units, _ = tags.make_units(selected([meeting]))
        for u in units:
            self.assertIn('Synthetic purpose', u['text'])
            self.assertNotIn('HIDDEN TRANSCRIPT', u['sparse'])
            self.assertNotIn('HIDDEN TRANSCRIPT', u['text'])

    def test_validation_rejects_duplicates_and_bad_probabilities(self):
        row = {
            'id': 'a',
            'projects': {'p0': 0.9},
            'p_none': 0.1,
            'p_people_event': 0,
            'reason': 'ok',
        }
        self.assertEqual(tags.validate({'units': [row]}, [{'id': 'a'}], ['p0']), [row])
        for k, v in [
            ('p_none', True),
            ('p_none', float('nan')),
            ('p_none', 1.1),
            ('projects', []),
            ('reason', 'two\nlines'),
        ]:
            bad = {**row, k: v}
            with self.subTest(k=k, v=v), self.assertRaises(ValueError):
                tags.validate({'units': [bad]}, [{'id': 'a'}], ['p0'])
        with self.assertRaises(ValueError):
            tags.validate({'units': [row, row]}, [{'id': 'a'}, {'id': 'b'}], ['p0'])

    def test_captured_style_project_name_keys(self):
        first = '11111111-1111-4111-8111-111111111111'
        second = '22222222-2222-4222-8222-222222222222'
        cards = [{'id': first, 'name': 'Project One'},
                 {'id': second, 'name': 'Project Two'}]
        cases = [
            ('names', cards, {'Project One': 0.9, 'Project Two': 0.7},
             {first: 0.9, second: 0.7}, 2, 0),
            ('ambiguous', [cards[0], {**cards[1], 'name': 'Project One'}],
             {'Project One': 0.9, 'project one': 0.8, 'Project One ': 0.7}, {}, 0, 3),
            ('ids', cards, {first: 0.9, second: 0.7}, {first: 0.9, second: 0.7}, 0, 0),
        ]
        for label, supplied, projects, expected, mapped, unknown in cases:
            with self.subTest(label=label):
                reply = {'units': [{'id': 'a', 'projects': projects, 'p_none': 0,
                                    'p_people_event': 0, 'reason': 'hint'}]}
                response = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
                    {'result': json.dumps(reply), 'usage': {}}))
                with patch.object(tags.subprocess, 'run', return_value=response):
                    rows, calls = tags.haiku(self.root, [unit('a')], supplied)
                self.assertEqual(rows[0]['projects'], expected)
                self.assertEqual(rows[0]['name_keys_mapped'], mapped)
                self.assertEqual(rows[0]['unknown_project_ids'], unknown)
                self.assertEqual(calls[0]['name_keys_mapped'], mapped)
                counts = json.loads((self.root / 'result.json').read_text())['tag_counts']
                self.assertEqual(counts['name_keys_mapped'], mapped)
                self.assertEqual(counts['unknown_project_ids'], unknown)

    def test_malformed_batch_retry_once_usage_and_fixed_prefix(self):
        valid = {
            'units': [
                {
                    'id': 'a',
                    'projects': {'p0': 0.9},
                    'p_none': 0.1,
                    'p_people_event': 0,
                    'reason': 'ok',
                }
            ]
        }
        seen = []

        def provider(args, **kw):
            seen.append(args)
            return SimpleNamespace(
                returncode=0,
                stderr='',
                stdout=json.dumps(
                    {
                        'result': json.dumps(valid) if len(seen) == 2 else '{}',
                        'usage': {'input_tokens': 11, 'cache_read_input_tokens': 20},
                    }
                ),
            )

        with patch.object(tags.subprocess, 'run', side_effect=provider):
            rows, calls = tags.haiku(self.root, [unit('a')], state(1)['projects'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['usage']['cache_read_input_tokens'], 20)
        self.assertEqual(rows[0]['id'], 'a')
        self.assertEqual(seen[0], seen[1])
        self.assertIn('--no-session-persistence', seen[0])
        self.assertEqual(seen[0][seen[0].index('--tools') + 1], '')
        with patch.object(
            tags.subprocess,
            'run',
            return_value=SimpleNamespace(returncode=0, stderr='', stdout='{}'),
        ) as provider:
            with self.assertRaisesRegex(ValueError, 'twice'):
                tags.haiku(self.root, [unit('a')], state(1)['projects'])
            self.assertEqual(provider.call_count, 2)

    def test_tagged_packet_prefix_is_identical_and_no_tags_unchanged(self):
        a, sizes = packets.packet(self.root, 'graph', 'window', 'A', tagged=True)
        b, bs = packets.packet(self.root, 'graph', 'window', 'B', 'g1', tagged=True)
        self.assertEqual(a[: sizes['shared']['chars']], b[: bs['shared']['chars']])
        self.assertIn("cheap model's guesses", a)
        plain, _ = packets.packet(self.root, 'graph', 'window', 'A')
        self.assertNotIn("cheap model's guesses", plain)
        meeting, _ = packets.packet(self.root, 'graph', 'window', 'M', 'meetings', tagged=True)
        self.assertIn(packets.REVIEW_ROLE.format(group='meetings'), meeting)
        self.assertIn('whole project groups, then meetings, then people', meeting)

    def test_turn_metrics_merge_blocks_without_double_count(self):
        path = self.root / 'turns.jsonl'
        blocks = [
            {'type': 'thinking', 'thinking': 'abc'},
            {'type': 'tool_use', 'id': 'tool', 'name': 'Bash'},
        ]
        events = [
            {
                'type': 'assistant',
                'message': {'id': 'one', 'usage': {'input_tokens': 9}, 'content': c},
            }
            for c in [blocks[:1], blocks, blocks[1:]]
        ]
        path.write_text('\n'.join(json.dumps(e) for e in events))
        calls, _ = step.usage(path)
        self.assertEqual(calls[0]['thinking_blocks'], 1)
        self.assertEqual(calls[0]['thinking_chars'], 3)
        totals = step.turn_totals(calls)
        self.assertEqual(totals['tool_calls'], 1)
        self.assertEqual(totals['usage']['input_tokens'], 9)

    def test_effort_a_and_review_real_subprocess(self):
        ctl = self.root / 'claude'
        ctl.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv
role=os.environ['GSD_ACTOR']
assert args[args.index('--effort')+1] == ('low' if role=='a' else 'high')
print(json.dumps({'type':'assistant','message':{'id':'one','content':[], 'usage':{'input_tokens':1}}}))
print(json.dumps({'type':'result','subtype':'success'}))
''')
        ctl.chmod(0o700)
        (self.root / 'shared.md').write_text('shared')
        for name in ('a', 'g1'):
            (self.root / f'prompt-{name}.md').write_text('read and stop')
            with patch.object(step, 'CLAUDE', str(ctl)):
                step.launch(
                    self.root,
                    name,
                    'synthetic',
                    '2026-01-03T00:00:00Z',
                    effort_a='low',
                    effort_review='high',
                    read_only=True,
                )

    def test_real_cli_tagged_scopes_packed_in_order_and_misroutes(self):
        dbpath = self.root / 'base.sqlite'
        with sqlite3.connect(dbpath) as db:
            db.row_factory = sqlite3.Row
            store.migrate(db)
            store.change_set(db, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until='2026-01-01T00:00:00Z')
        with store.connect(dbpath, True) as db:
            project = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'project',
                                    'set': {'name': 'Synthetic project', 'status': 'active'},
                                }
                            ]
                        }
                    ]
                },
                'test',
            )[0]['records'][0]
        qmd = self.root / 'qmd'
        qmd.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
assert ('QMD_UNTIL' not in os.environ) if args[0]=='feed' else os.environ['QMD_UNTIL'].startswith('2026-01-03')
def doc(ref, src):
 return {'id':ref,'source':src,'time':'2026-01-02T00:00:00Z','title':'Synthetic','thread':'thread','from':'Pat <pat@example.org>','to':'owner@example.test','parts':[{'kind':'authored','text':'Send the report due 2026-01-04'}]}
if args[0]=='feed':
 src=args[args.index('--source')+1]
 ref={'gmail':'gmail:account:one','fathom':'fathom:account:one'}.get(src)
 item={'token':'1','id':ref,'item':ref,'source':src,'kind':'email' if src=='gmail' else 'meeting','time':1767312000,'arrived':1767312000,'title':None,'from':None,'thread':None,'chat':None,'version':'v1','tombstone':False,'reparse':False}
 print(json.dumps({'schema':'qmd.feed.v1','source':src,'items':[item] if ref else [],'next':'1' if ref else None,'head':'1' if ref else None,'complete':True}))
else:
 print(json.dumps([{'id':ref,'docs':[doc(ref,'fathom' if ref.startswith('fathom:') else 'gmail')]} for ref in args[1:] if not ref.startswith('--')]))
''')
        qmd.chmod(0o700)
        ctl = self.root / 'claude'
        ctl.write_text(
            '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
GSD=__GSD__; PROJECT=__PROJECT__
a=sys.argv[1:]; prompt=sys.stdin.read(); root=pathlib.Path.cwd()
if a[a.index('--output-format')+1]=='json':
 assert a[a.index('--model')+1]=='claude-haiku-5-5'
 units=json.loads(prompt)['units']
 print(json.dumps({'result':json.dumps({'units':[{'id':u['id'],'projects':{PROJECT:.9},'p_none':0,'p_people_event':0,'reason':'synthetic'} for u in units]}),'usage':{'input_tokens':3,'cache_read_input_tokens':5,'output_tokens':7}}))
 raise SystemExit()
role=os.environ['GSD_ACTOR']
assert a[a.index('--effort')+1]==('high' if role=='a' else 'medium')
def cli(cmd, payload):
 p=subprocess.run([GSD,cmd,'--json'],input=json.dumps(payload),text=True,capture_output=True)
 if p.returncode: raise RuntimeError(p.stdout)
if role=='a':
 cli('write',{'changes':[{'id':'a1','ops':[{'upsert':'item','set':{'title':'Synthetic report','project_id':PROJECT},'cite':[{'ref':'fathom:account:one'}]}]},{'id':'a2','ops':[{'upsert':'person','set':{'display_name':'Pat Synthetic','status':'active'},'cite':[{'ref':'gmail:account:one'}]}]}]})
 (root/'open.md').write_text('No open work')
elif role=='r1':
 assert 'Project' in prompt
 assert "each of the window's meetings" in prompt
 assert 'including what earlier reviewers wrote' in prompt
 assert 'Before: none' in prompt and 'After: none' in prompt
 assert 'fathom:account:one' in prompt and 'a1' in prompt
 cli('decide',{'decisions':[{'id':cid,'decision':'accept'} for cid in ('a1','a2')]})
 cli('decide',{'decisions':[{'id':'a1','decision':'amend','quote':{'ref':'fathom:account:one','text':'due 2026-01-04'},'set':{'due_at':'2026-01-04'}}]})
 cli('write',{'changes':[{'id':'b-r1-1','grp':'r1','ops':[{'upsert':'meeting','set':{'title':'Synthetic meeting'},'cite':[{'ref':'fathom:account:one'}]}]}]})
 cli('decide',{'decisions':[{'id':'b-r1-1','decision':'accept'}]})
else: raise RuntimeError(role)
if role!='a': (root/('notes-'+role+'.md')).write_text('Checked')
print(json.dumps({'type':'assistant','message':{'id':'one','content':[{'type':'thinking','thinking':'synthetic thought'}],'usage':{'input_tokens':3}}}))
print(json.dumps({'type':'result','subtype':'success'}))
'''.replace('__GSD__', repr(str(ROOT / 'bin/gsd'))).replace('__PROJECT__', repr(project))
        )
        ctl.chmod(0o700)
        run = self.root / 'run'
        env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'])
        p = subprocess.run(
            [
                str(ROOT / 'bin/gsd'),
                'step',
                '--db',
                str(dbpath),
                '--since',
                '2026-01-01T00:00:00Z',
                '--until',
                '2026-01-03T00:00:00Z',
                '--run',
                str(run),
                '--a',
                'synthetic',
                '--review',
                'synthetic',
                '--effort-a',
                'high',
                '--json',
            ],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result['state'], 'committed')
        self.assertTrue((run / 'packet-r1.md').is_file())
        self.assertFalse((run / 'packet-people.md').exists())
        self.assertEqual(list(result['groups']['groups']), ['r1'])
        self.assertEqual([s['kind'] for s in result['groups']['scopes']['r1']], ['projects', 'meetings', 'people'])
        self.assertTrue((run / 'misroutes.md').is_file())
        self.assertEqual(result['misroutes']['units'], 1)
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertTrue(manifest['tagged'])
        self.assertEqual(manifest['a_totals']['thinking_chars'], len('synthetic thought'))
        self.assertEqual(len(manifest['tags']['calls']), 1)
        with store.connect(dbpath) as db:
            self.assertEqual(store.rows(db, 'items')[0]['due_at'], '2026-01-04')
            self.assertEqual(len(store.rows(db, 'meetings')), 1)


if __name__ == '__main__':
    unittest.main()
