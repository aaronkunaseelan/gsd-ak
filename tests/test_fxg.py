"""BR3 regression probes: synthetic data, scratch SQLite, no provider writes.
These tests state the required behaviour and are expected to fail at 8b35fc0.
"""
import json
import os
import subprocess
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gsd import capture, db as store, draft, feed, inbox, intent, selection, status_page, step, tags, worklist, write
from test_selection import FakeQmd, doc, render
from test_inbox import comment

CUT = '2026-10-19T00:00:00Z'
END = '2026-10-20T00:00:00Z'
NEXT = '2026-10-21T00:00:00Z'
PARENT = 'gmail:synthetic:mail'
CHILD = PARENT + '/att/1'
EXTRACT = 'UNIQUE_PDF_ACTION: Pat must approve the budget by Friday.'


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='br3-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'gsd.sqlite'
        with sqlite3.connect(self.target) as db:
            store.migrate(db)
        with store.connect(self.target, True) as db:
            store.change_set(db, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until=CUT)

    def upsert(self, kind='item', rid=None, at=END, **fields):
        op = {'upsert': kind, 'set': fields}
        if rid:
            op['id'] = rid
        if fields.get('state') in ('done', 'canceled'):
            op['cite'] = [{'ref': 'gmail:synthetic:decision', 'role': 'decides', 'excerpt': 'Done.'}]
        with store.connect(self.target, True) as db, patch.object(store, 'now', return_value=at):
            result = write.batch(db, {'changes': [{'ops': [op]}]}, 'owner')
        self.assertTrue(result[0]['ok'], result)
        return result[0]['records'][0]

    def selected_attachment(self, state):
        parent = doc(PARENT, 'person@example.org', [('body', 'Pat, see attached.')],
                     source='gmail', time=CUT, attachments=[{'id': CHILD, 'name': 'brief.pdf', 'type': 'application/pdf'}])
        child = doc(CHILD, parts=[('body', EXTRACT)], source='gmail', time=CUT, version='2')
        rendered = render(attachments=[(CHILD, 'brief.pdf', 'application/pdf')])

        class AttachmentQmd(FakeQmd):
            def __call__(q, *args, **kwargs):
                if args[0] == 'get' and '--no-quoted' not in args:
                    q.calls.append(args)
                    if '--materialise' in args:
                        return {'id': CHILD, 'docs': [child]} if state == 'done' else {'id': CHILD, 'docs': [], 'state': state}
                    return {'id': CHILD, 'docs': []}
                reply = super().__call__(*args)
                # A refreshed parent really supplies the extracted attachment head.
                if args[0] == 'get' and any('--materialise' in c for c in q.calls) and state == 'done':
                    fresh = dict(parent, parts=parent['parts'] + [{'kind': 'attachment', 'text': EXTRACT}])
                    reply = {'id': PARENT, 'text': rendered, 'docs': [fresh]}
                return reply
        q = AttachmentQmd({'gmail': [{'id': PARENT, 'time': CUT}]}, {PARENT: parent}, {PARENT: rendered})
        return q, selection.select(q, CUT, END), child


    def test_queued_attachment_never_blocks_preparation_or_checkpoint(self):
        q, _, _ = self.selected_attachment('queued')
        run = self.root / 'run'
        run.mkdir()
        args = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False, a='fake', review='fake')
        def launch(*args, **kwargs):
            (run / 'open.md').write_text('')
            return []
        from gsd.groups import assign
        with patch.object(step, 'Qmd', return_value=q), patch.object(inbox, 'window'), patch.object(step, 'launch', side_effect=launch):
            result = step.execute(args, run, 'queued', CUT, END, selection.select, assign)
        self.assertEqual(result['state'], 'committed')
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertEqual(manifest['capture']['results'][0]['state'], 'queued')
        with store.connect(self.target) as db:
            self.assertEqual(store.pipeline_cut(db)['until'], store.iso(END))
            self.assertEqual(feed.positions(db)['gmail'], '1')

    def test_page_cap_and_unknown_states_are_recorded_without_blocking(self):
        q, selected, _ = self.selected_attachment('done')
        capture.capture(q, selected, cap=0)
        self.assertEqual(selected['manifest']['capture']['results'][0]['state'], 'skipped:page-cap')
        for response in (None, {}):
            q, selected, _ = self.selected_attachment('done')
            def reply(*args, **kwargs):
                return response if '--materialise' in args else q(*args, **kwargs)
            ledger = capture.capture(reply, selected)
            self.assertEqual(ledger['results'][0]['state'], 'unknown')
        for state in ('failed', 'unavailable', 'queued'):
            q, selected, _ = self.selected_attachment(state)
            self.assertEqual(capture.capture(q, selected)['results'][0]['state'], state)

    def committed_parent(self, state):
        q, selected, child = self.selected_attachment(state)
        try:
            capture.capture(q, selected)
        except ValueError as exc:
            # Fixture of an existing nonblocking parent commit, even on the old builder.
            if 'attachments pending' not in str(exc):
                raise
        run = self.root / 'first'
        run.mkdir()
        manifest = selected['manifest']
        manifest['mail']['kept_list'] = [{'ref': PARENT, 'why': selected['mail_entries'][0]['why']}]
        (run / 'manifest.json').write_text(json.dumps(manifest))
        (run / 'selected.json').write_text(json.dumps(selected['docs']))
        (run / 'changes.jsonl').write_text('')
        (run / 'open.md').write_text('')
        draft.snapshot(self.target, run / 'step.sqlite', 'first', CUT, END)
        with store.connect(run / 'step.sqlite', True) as db:
            feed.attach(db, selected['feed'])
        result = draft.commit(run / 'step.sqlite', self.target, note='gsd-step-run:' + str(run))
        q2 = FakeQmd({'gmail': [{'id': PARENT, 'time': CUT},
                               {'id': CHILD, 'item': PARENT, 'time': CUT, 'kind': 'attachment', 'version': '2'}]},
                     {CHILD: child}, {CHILD: render(EXTRACT)})
        return q2, result['change_set_id']

    def test_queued_child_arriving_later_reaches_proposer_under_selected_parent(self):
        q, committed = self.committed_parent('queued')
        run = self.root / 'late'
        run.mkdir()
        args = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False)
        picked = []
        def select(*args):
            selected = selection.select(*args)
            picked.append(selected)
            return selected
        with patch.object(step, 'Qmd', return_value=q), patch.object(inbox, 'window'), \
                patch.object(selection, 'classify', side_effect=AssertionError('do not reclassify committed parents')):
            _, window, _, manifest = step.prepare(args, run, 'late', END, NEXT, select)
        self.assertIn(EXTRACT, window)
        self.assertIn(committed, window)
        self.assertIn(EXTRACT, (run / 'packet-a.md').read_text())
        self.assertEqual(manifest['mail']['excluded_materialised'], 0)
        provenance = manifest['mail']['late_children'][0]
        self.assertEqual(provenance['parent_commit'], committed)
        self.assertEqual(provenance['why'], 'keep:person')
        units, _ = tags.make_units(picked[0])
        self.assertEqual(len(units), 1)
        self.assertEqual(set(units[0]['source_ids']), {PARENT, CHILD})
        self.assertIn(EXTRACT, units[0]['sparse'])
        self.assertFalse(any(PARENT in c for c in q.calls if c[0] == 'get'))

    def test_done_child_rearrival_is_excluded_with_owning_commit(self):
        q, committed = self.committed_parent('done')
        run = self.root / 'again'
        run.mkdir()
        args = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False)
        with patch.object(step, 'Qmd', return_value=q), patch.object(inbox, 'window'):
            _, _, _, manifest = step.prepare(args, run, 'again', END, NEXT, selection.select)
        self.assertEqual(manifest['state'], 'empty')
        self.assertEqual(manifest['mail']['excluded_materialised'], 1)
        self.assertIn(committed, manifest['mail']['late_children'][0]['why'])
        self.assertEqual([c[1] for c in q.calls if c[0] == 'get'], [CHILD])

    def test_late_child_without_history_still_obeys_parent_mail_selection(self):
        parent = doc(PARENT, 'person@example.org', [('body', 'Application attached.')],
                     source='gmail', time=CUT, to='alias@example.org')
        q = FakeQmd({'gmail': [{'id': CHILD, 'item': PARENT, 'time': CUT, 'kind': 'attachment'}]},
                    {PARENT: parent}, {PARENT: render()})
        selected = selection.select(q, END, NEXT)
        self.assertEqual(selected['docs'], {})
        self.assertIn('drop:not-addressed', selected['manifest']['mail']['late_children'][0]['why'])

    def test_changed_child_version_is_not_mistaken_for_included_content(self):
        q, _ = self.committed_parent('done')
        q.sources['gmail'][1]['version'] = 'new-version'
        q.docs[CHILD] = {**q.docs[CHILD], 'version': 'new-version'}
        run = self.root / 'changed'
        run.mkdir()
        args = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False)
        with patch.object(step, 'Qmd', return_value=q), patch.object(inbox, 'window'):
            _, window, _, _ = step.prepare(args, run, 'changed', END, NEXT, selection.select)
        self.assertIn(EXTRACT, window)

    def test_late_children_of_whatsapp_and_fathom_reach_both_views(self):
        for source, suffix in (('whatsapp', '/att/1'), ('fathom', '/att/1'), ('whatsapp', '/att/1/ocr')):
            with self.subTest(source=source, suffix=suffix):
                parent, child = source + ':test:parent', source + ':test:parent' + suffix
                docs = {parent: doc(parent, 'Pat', [('body', 'Parent context')], source=source,
                                    time=CUT, thread='pat@s.whatsapp.net'),
                        child: doc(child, parts=[('body', EXTRACT)], source=source, time=CUT)}
                q = FakeQmd({source: [{'id': child, 'item': child.removesuffix('/ocr'), 'kind': 'attachment', 'time': CUT}]},
                            docs, {parent: render(), child: render(EXTRACT)})
                selected = selection.select(q, END, NEXT)
                capture.capture(q, selected)
                view = selected['views']['whatsapp.md' if source == 'whatsapp' else 'meetings.md']
                self.assertIn('Parent context', view)
                self.assertIn(EXTRACT, view)
                units, _ = tags.make_units(selected)
                self.assertIn(EXTRACT, '\n'.join(u['sparse'] for u in units))
                self.assertEqual(sum(child in u['source_ids'] for u in units), 1)

    def test_unrelated_fathom_recap_is_not_dropped_by_time_coincidence(self):
        ref, meeting = 'gmail:test:recap-B', 'fathom:test:meeting-A'
        q = FakeQmd({'gmail': [{'id': ref, 'time': '2026-10-20T10:30:00Z'}],
                     'fathom': [{'id': meeting, 'time': '2026-10-20T09:00:00Z'}]},
                    {ref: doc(ref, 'Fathom <recap@fathom.video>',
                              [('body', 'Pat: a decision only in meeting B. Recording https://fathom.video/share/meeting-B')],
                              source='gmail', title='Recap B'),
                     meeting: doc(meeting, parts=[('body', 'Meeting A')], source='fathom',
                                  where='Zoom · End: 2026-10-20T10:00:00Z')}, {ref: render(), meeting: render()})
        selected = selection.select(q, CUT, NEXT)
        self.assertIn(ref, selected['docs'])

    def test_gsd_preserves_explicit_unknown_current(self):
        from test_intent import status, NOW
        raw = status()
        raw['sources']['gmail']['current'] = None
        reply = subprocess.CompletedProcess('qmd', 0, json.dumps(raw), '')
        with patch('gsd.steady.subprocess.run', return_value=reply):
            hop = intent.chain(NOW, CUT)[1]
        self.assertTrue(hop['warn'])
        self.assertIn('unknown', hop['note'])

    def test_failed_steward_read_is_not_an_empty_schedule_on_notion(self):
        with patch.object(status_page.subprocess, 'run', side_effect=subprocess.TimeoutExpired('steward', 30)):
            reading = status_page.steward_view()
        self.assertIsNone(reading)
        text = status_page.render(self.status_doc(), [], reading, [], None, datetime(2026, 10, 20, tzinfo=UTC))['Last run']
        self.assertNotIn('Next automatic run: not planned yet', text)
        self.assertIn('unknown', text)

    def status_doc(self):
        return {'blocks': [
            {'kind': 'stock', 'open': 0, 'you': 0, 'past_due': 0, 'due_7d': 0, 'fresh_through': None},
            {'kind': 'rows', 'items': []}, {'kind': 'chain', 'hops': []}]}

    def test_gsd_rejects_a_schema_less_steward_view(self):
        for malformed in ({'blocks': []}, {'schema': 'intent/1', 'system': 'other', 'state': 'green', 'at': 1, 'blocks': []}):
            with self.subTest(malformed=malformed):
                reply = subprocess.CompletedProcess('steward', 0, json.dumps(malformed), '')
                with patch.object(status_page.subprocess, 'run', return_value=reply):
                    self.assertIsNone(status_page.steward_view())

    def test_failed_run_must_not_borrow_nearby_success_record(self):
        start = datetime(2026, 10, 20, 10, tzinfo=UTC)
        failed = {'id': 'run-failed', 'started_at': start.isoformat(),
                  'ended_at': (start + timedelta(seconds=1)).isoformat(), 'status': 'failed'}
        later = {'steward_run_id': 'other', 'ended': start + timedelta(minutes=3),
                 'error': None, 'counts': {'records_landed': 7}}
        self.assertIsNone(status_page.match(failed, [later]))
        sections = status_page.render(self.status_doc(), [later], {'runs': [failed]}, [], None, start + timedelta(hours=1))
        for key in ('Last run', 'Recent runs'):
            self.assertIn('failed', sections[key])
            self.assertIn('gsd detail unknown', sections[key])
            self.assertNotIn('7 records landed', sections[key])
        matching = {**later, 'steward_run_id': 'failed', 'ended': start + timedelta(days=2)}
        self.assertIs(status_page.match(failed, [matching]), matching)
        self.assertIsNone(status_page.match(failed, [{k: v for k, v in later.items() if k != 'steward_run_id'}]))

    def test_steady_record_carries_steward_run_identity(self):
        from gsd import steady
        record = {'counts': {'records_landed': 0}}
        with patch.dict(os.environ, {'STEWARD_RUN_ID': 'identity', 'STEWARD_RUN_DIR': str(self.root / 'steward-run')}):
            steady.emit(record, self.target)
        local = json.loads(next((self.root / 'steady-runs').glob('*.json')).read_text())
        external = json.loads((self.root / 'steward-run/records.jsonl').read_text())
        self.assertEqual(local['steward_run_id'], 'identity')
        self.assertEqual(local, external)

    def test_nonowner_comment_must_not_be_relabelled_owner_instruction(self):
        rid = self.upsert(title='Task')
        with store.connect(self.target, True) as db:
            db.execute('INSERT INTO notion_map(record_id,page_id) VALUES (?,?)', (rid, 'page'))
        c = comment('other-comment', page='page', text='I finished my part.')
        c['created_by']['id'] = 'OTHER-AUTHOR'
        db = inbox.connect(self.target)
        inbox.store_comment(db, c, 'page', '2026-10-19T12:00:00.000000Z')
        db.close()
        selected = {'refs': {}, 'views': {}, 'manifest': {}}
        inbox.window(selected, self.target, store.iso(CUT), store.iso(END))
        self.assertEqual(selected['comments'], 1)
        text = selected['views']['comments.md']
        self.assertIn('OTHER-AUTHOR', text, 'author_id was saved but stripped; all comments asserted to be the owner')

    def test_invalid_operational_dates_are_rejected_before_poisoning_views(self):
        for kind, fields in (
            ('item', ('due_at', 'next_review_at', 'closed_at', 'created_at', 'updated_at', 'source_updated_at')),
            ('meeting', ('starts_at', 'ends_at', 'start_date', 'end_date')),
        ):
            for field in fields:
                with self.subTest(kind=kind, field=field), store.connect(self.target, True) as db:
                    result = write.batch(db, {'changes': [{'ops': [
                        {'upsert': kind, 'set': {'title': 'Task', field: 'not-a-date'}}]}]}, 'owner')
                    self.assertFalse(result[0]['ok'], 'write accepted a date its readers cannot render')
                    self.assertIn('ops[0].' + field, [e['field'] for e in result[0]['errors']])
        for value in ('2026-10-25', '2026-10-25T03:30:00+03:00', '2026-10-25T03:30:00+02:00', None):
            rid = self.upsert(title='Valid dates', due_at=value)
            with store.connect(self.target) as db:
                self.assertEqual(store.current(db, 'items', (rid,))['due_at'], value)

    def test_closure_flow_uses_transition_even_without_manually_supplied_closed_at(self):
        rid = self.upsert(at='2026-10-24T12:00:00Z', title='Task', state='ready')
        # Second occurrence of 03:30 Athens during the 25 Oct clock rollback.
        self.upsert(rid=rid, at='2026-10-25T01:30:00Z', state='done')
        now = datetime(2026, 10, 25, 12, tzinfo=UTC)
        with patch.object(store, 'now', return_value=store.iso(now.isoformat())):
            events = worklist.command(SimpleNamespace(db=self.target))['done']
        self.assertEqual(len(events), 1)
        with patch.object(intent, 'chain', return_value=[]):
            flow = intent.document(self.target, now)['blocks'][1]['days']
        day = next(d for d in flow if d['day'] == '2026-10-25')
        self.assertEqual(day['out'], 1, 'worklist has a closure but intent claims zero because closed_at is null')

    def test_reopening_must_reenter_flow_stock(self):
        rid = self.upsert(at='2026-10-23T12:00:00Z', title='Task', state='ready')
        self.upsert(rid=rid, at='2026-10-24T12:00:00Z', state='done', closed_at='2026-10-24T12:00:00Z')
        self.upsert(rid=rid, at='2026-10-25T01:30:00Z', state='ready')
        with patch.object(intent, 'chain', return_value=[]):
            document = intent.document(self.target, datetime(2026, 10, 25, 12, tzinfo=UTC))
        self.assertEqual(document['blocks'][0]['open'], 1)
        self.assertEqual(document['blocks'][1]['days'][-1]['open'], 1, 'current open=1 but flow open=0 from stale closed_at')

    def test_flow_keeps_deleted_history_and_repeated_close_reopen_events(self):
        rid = self.upsert(at='2026-10-22T12:00:00Z', title='Task')
        self.upsert(rid=rid, at='2026-10-23T12:00:00Z', state='done')
        self.upsert(rid=rid, at='2026-10-24T12:00:00Z', state='ready')
        self.upsert(rid=rid, at='2026-10-24T13:00:00Z', state='canceled')
        self.upsert(rid=rid, at='2026-10-24T14:00:00Z', title='Closed edit')
        with store.connect(self.target, True) as db, patch.object(store, 'now', return_value='2026-10-25T01:30:00Z'):
            self.assertTrue(write.batch(db, {'changes': [{'ops': [{'tombstone': rid}]}]}, 'owner')[0]['ok'])
        with patch.object(intent, 'chain', return_value=[]):
            days = intent.document(self.target, datetime(2026, 10, 25, 12, tzinfo=UTC))['blocks'][1]['days']
        self.assertEqual([(d['open'], d['in'], d['out']) for d in days[-4:]],
                         [(1, 1, 0), (0, 0, 1), (0, 1, 1), (0, 0, 0)])

    def test_completed_attachment_head_reaches_untagged_view(self):
        q, selected, _ = self.selected_attachment('done')
        capture.capture(q, selected)
        self.assertIn(CHILD, selected['docs'])
        self.assertIn(EXTRACT, selected['views']['mail.md'], 'refresh discarded docs[].parts attachment text')

    def test_completed_attachment_head_reaches_default_tagged_view(self):
        q, selected, _ = self.selected_attachment('done')
        capture.capture(q, selected)
        units, _ = tags.make_units(selected)
        self.assertIn(EXTRACT, units[0]['text'])  # Haiku sees it.
        self.assertIn(EXTRACT, units[0]['sparse'], 'att# marker suppressed the fallback child head for A/R1')

    def test_shared_meeting_reference_selected_by_capture_is_citable(self):
        ref = PARENT + '/att/link-' + 'a' * 24
        selected = {'docs': {}, 'refs': {}, 'meetings': [], 'views': {'meetings.md': ''}}
        capture.record_captured_docs([doc(ref, parts=[('body', 'Meeting action')])], 'share', selected, {'shared_meetings': 0})
        self.assertIn(ref, selected['refs'])
        with store.connect(self.target, True) as db:
            result = write.batch(db, {'changes': [{'ops': [
                {'upsert': 'item', 'set': {'title': 'Meeting action'}, 'cite': [{'ref': ref}]}]}]}, 'owner')
        self.assertTrue(result[0]['ok'], result)

    def test_next_actor_none_is_not_another_person(self):
        self.upsert(title='No action owed', next_actor='none')
        with patch.object(intent, 'chain', return_value=[]):
            document = intent.document(self.target, datetime(2026, 10, 20, 12, tzinfo=UTC))
        with patch.object(store, 'now', return_value=store.iso(END)):
            priorities = worklist.command(SimpleNamespace(db=self.target))['priorities']
        self.assertEqual(priorities['waiting_on_others'], [])
        self.assertEqual(document['blocks'][0]['others'], 0, 'next_actor=none was counted as another person')

    def test_all_four_actors_have_one_stock_and_project_meaning(self):
        project = self.upsert(kind='project', name='Project', status='active')
        for actor in ('user', 'agent', 'external', 'none'):
            self.upsert(title=actor, next_actor=actor, project_id=project)
        with patch.object(intent, 'chain', return_value=[]):
            blocks = intent.document(self.target, datetime(2026, 10, 20, 12, tzinfo=UTC))['blocks']
        for row in (blocks[0], blocks[2]['items'][0]):
            self.assertEqual([row[k] for k in ('you', 'agents', 'others')], [1, 1, 1])

    def test_seven_day_totals_include_more_than_fourteen_manual_and_automatic_runs(self):
        now = datetime(2026, 10, 20, 12, tzinfo=UTC)
        records = [{'ended': now - timedelta(hours=15 - n), 'counts': {'records_landed': 1},
                    'minutes': 1, 'weighted_tokens': 1} for n in range(15)]
        doc = {'blocks': [
            {'kind': 'stock', 'open': 0, 'you': 0, 'past_due': 0, 'due_7d': 0, 'fresh_through': None},
            {'kind': 'rows', 'items': []}, {'kind': 'chain', 'hops': []}]}
        text = status_page.render(doc, records, None, [], None, now)['Last 7 days']
        self.assertIn('**15** runs', text, 'rolling seven days was truncated to fourteen runs')

    def test_seven_day_publish_median_uses_every_publish_in_window(self):
        now = datetime(2026, 10, 20, 12, tzinfo=UTC)
        publishes = [{'timestamp': (now - timedelta(hours=n)).isoformat(),
                      'requests': {'total': 100 if n < 15 else 1}} for n in range(29)]
        publishes.append({'timestamp': (now - timedelta(days=8)).isoformat(), 'requests': {'total': 0}})
        text = status_page.render(self.status_doc(), [], None, publishes, None, now)['Last 7 days']
        self.assertIn('Notion requests per publish · median 100', text)

    def test_source_window_conflict_must_not_claim_committed_and_consume_feed(self):
        rid = self.upsert(title='Original', state='ready')
        stable = self.upsert(title='Stable original', state='ready')
        q = FakeQmd({'gmail': [{'id': PARENT, 'time': CUT}]},
                    {PARENT: doc(PARENT, 'person@example.org', [('body', 'Pat: updated title')], source='gmail')},
                    {PARENT: render()})
        a = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False, a='fake', review='fake')
        run = self.root / 'run'
        run.mkdir()

        def launch(run, name, *args, **kwargs):
            with store.connect(run / 'step.sqlite', True) as db:
                if name == 'a':
                    result = write.batch(db, {'changes': [
                        {'id': 'a1', 'ops': [{'upsert': 'item', 'id': rid, 'set': {'title': 'Proposed'}, 'cite': [{'ref': PARENT}]}]},
                        {'id': 'a2', 'ops': [{'upsert': 'item', 'id': stable, 'set': {'title': 'Would otherwise land'}, 'cite': [{'ref': PARENT}]}]},
                    ]}, 'a')
                    self.assertTrue(result[0]['ok'], result)
                    (run / 'open.md').write_text('')
                else:
                    self.assertTrue(all(r['ok'] for r in draft.decide(db, [
                        {'id': cid, 'decision': 'accept'} for cid in ('a1', 'a2')], name)))
            if name != 'a':
                self.upsert(rid=rid, summary='Concurrent owner edit')
            return []

        from gsd.groups import assign
        with patch.object(step, 'Qmd', return_value=q), patch.object(step, 'launch', side_effect=launch), patch.object(inbox, 'window'):
            with self.assertRaises(write.Invalid) as error:
                step.execute(a, run, 'conflict', CUT, END, selection.select, assign)
        self.assertIn('source window rolled back', str(error.exception.errors))
        self.assertFalse((run / 'commit.json').exists())
        self.assertFalse((run / 'result.json').exists())
        with store.connect(self.target) as db:
            self.assertEqual(feed.positions(db), {})
            self.assertEqual(store.pipeline_cut(db)['until'], CUT)
            self.assertEqual(store.current(db, 'items', (rid,))['title'], 'Original')
            self.assertEqual(store.current(db, 'items', (stable,))['title'], 'Stable original')
            self.assertIsNone(db.execute("SELECT 1 FROM change_sets WHERE change_set_id='pipeline:conflict'").fetchone())

    def test_feed_window_review_hold_and_reject_still_commit_accepted_work(self):
        path = self.root / 'decisions.sqlite'
        draft.snapshot(self.target, path, 'decisions', CUT, END)
        with store.connect(path, True) as db:
            result = write.batch(db, {'changes': [{'id': cid, 'ops': [
                {'upsert': 'item', 'set': {'title': cid}}]} for cid in ('a1', 'a2', 'a3')]}, 'a')
            self.assertTrue(all(r['ok'] for r in result), result)
            decisions = [{'id': cid, 'decision': decision,
                          'quote': {'ref': PARENT, 'text': 'Source statement'}}
                         for cid, decision in (('a1', 'accept'), ('a2', 'hold'), ('a3', 'reject'))]
            self.assertTrue(all(r['ok'] for r in draft.decide(db, decisions, 'reviewer')))
            feed.attach(db, {'starts': {'gmail': None}, 'positions': {'gmail': '1'}, 'bootstraps': {'gmail': CUT}})
        result = draft.commit(path, self.target, actor=store.PIPELINE_ACTOR)  # the step runner commits feed windows
        self.assertEqual(result['refused'], [])
        with store.connect(self.target) as db:
            self.assertEqual([row['title'] for row in store.rows(db, 'items')], ['a1'])
            self.assertEqual(feed.positions(db)['gmail'], '1')
            self.assertEqual(store.pipeline_cut(db)['until'], store.iso(END))

# More packet regressions follow.
