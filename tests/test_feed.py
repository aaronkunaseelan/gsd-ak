"""D27: synthetic arrivals, real scratch drafts and atomic commits."""

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db as store, draft, feed, selection, step, write

CUT = '2026-10-19T00:00:00Z'
UNTIL = '2026-10-20T00:00:00Z'


def item(token='one', **changes):
    return dict(token=token, id='gmail:synthetic', item='gmail:synthetic', source='gmail',
                kind='email', time=1, arrived=1792368001, title=None,
                **{'from': None}, thread=None, chat=None, version='v1',
                tombstone=False, reparse=False) | changes


def page(items, complete=True, source='gmail', next_=None):
    return dict(schema='qmd.feed.v1', source=source, items=items,
                next=items[-1]['token'] if items else next_,
                head=(items[-1]['token'] if items else next_) if complete else 'pending',
                complete=complete)


class Qmd:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return next(self.pages)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'target.sqlite'
        with sqlite3.connect(self.target) as db:
            store.migrate(db)
            store.change_set(db, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until=CUT)

    def state(self):
        with store.connect(self.target) as db:
            self.assertFalse(store.has_table(db, 'feed_seen'))
            return feed.positions(db), feed.bootstraps(db)

    def attach(self, state, name='draft'):
        path = self.root / (name + '.sqlite')
        draft.snapshot(self.target, path, name, CUT, UNTIL)
        with store.connect(path, True) as db:
            feed.attach(db, state)
        return path

    def test_late_old_time_selected_then_committed_and_restart_dedupes(self):
        from test_selection import FakeQmd, doc, render
        old = '2026-01-01T00:00:00Z'
        qmd = FakeQmd({'gmail': [{'id': 'gmail:synthetic', 'time': old}]},
                      {'gmail:synthetic': doc('gmail:synthetic', 'pat@example.org', [('body', 'Pat: report')])},
                      {'gmail:synthetic': render()})
        qmd.feed_since = CUT
        selected = selection.select(qmd, CUT, UNTIL)
        self.assertIn('gmail:synthetic', selected['docs'])
        path = self.attach(selected['feed'])
        with store.connect(path, True) as db:
            write.batch(db, {'changes': [{'id': 'a1', 'ops': [{'upsert': 'item', 'set': {'title': 'Synthetic late input'}, 'cite': [{'ref': 'gmail:synthetic'}]}]}]}, 'a')
            draft.decide(db, [{'id': 'a1', 'decision': 'accept'}], 'review')
        result = draft.commit(path, self.target)
        self.assertEqual(result['landed_changes'], ['a1'])
        positions, bootstraps = self.state()
        self.assertEqual(positions['gmail'], '1')
        self.assertEqual(bootstraps['gmail'], CUT)
        q = Qmd([page([], next_='1')])
        q.feed_positions, q.feed_bootstraps = positions, bootstraps
        f = feed.Feed(q, CUT, UNTIL)
        self.assertEqual(f.rows('gmail'), [])
        self.assertEqual(q.calls[0][-2:], ('--after', '1'))
        self.assertEqual(f.state['positions']['gmail'], '1')

    def test_reparse_of_committed_mail_is_not_input_but_late_capture_is(self):
        from test_selection import FakeQmd, doc, render
        original = feed.Feed(Qmd([page([item('1')])]), CUT, UNTIL)
        original.rows('gmail')
        draft.commit(self.attach(original.state), self.target)
        old = '2026-01-01T00:00:00Z'
        qmd = FakeQmd({'gmail': [
            {'id': 'gmail:synthetic', 'time': old, 'version': 'v1'},
            {'id': 'gmail:synthetic', 'time': old, 'version': 'v2', 'reparse': True},
            {'id': 'gmail:late', 'time': old, 'version': 'v1'},
        ]}, {'gmail:late': doc('gmail:late', 'pat@example.org', [('body', 'Pat: late report')])},
            {'gmail:late': render()})
        with store.connect(self.target) as db:
            qmd.feed_positions = feed.positions(db)
            qmd.feed_bootstraps = feed.bootstraps(db)
            qmd.feed_since = store.pipeline_cut(db)['until']
        selected = selection.select(qmd, UNTIL, '2026-10-21T00:00:00Z')
        self.assertEqual(set(selected['docs']), {'gmail:late'})
        self.assertEqual(selected['manifest']['mail']['skipped_reparse'], 1)
        self.assertEqual(selected['manifest']['mail']['feed_rows'], 2)
        self.assertEqual(selected['feed']['positions']['gmail'], '3')
        gets = [call for call in qmd.calls if call[0] == 'get']
        self.assertEqual(len(gets), 1)
        self.assertNotIn('gmail:synthetic', gets[0])
        self.assertNotIn('seen', selected['feed'])

    def test_reparse_only_empty_commit_counts_and_advances_without_fetch(self):
        from test_selection import FakeQmd
        qmd = FakeQmd({'gmail': [
            {'id': 'gmail:synthetic', 'time': CUT, 'reparse': True},
            {'id': 'gmail:synthetic', 'time': CUT, 'reparse': True},
        ]}, {}, {})
        args = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False)
        run = self.root / 'reparse'
        run.mkdir()
        with patch.object(step, 'Qmd', return_value=qmd), patch('gsd.inbox.window'):
            out = step.execute(args, run, 'reparse', CUT, UNTIL, selection.select, None)
        self.assertEqual(out['state'], 'empty')
        import json
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertEqual(manifest['mail']['skipped_reparse'], 2)
        self.assertEqual(manifest['mail']['source_items'], 0)
        self.assertFalse(any(call[0] == 'get' for call in qmd.calls))
        self.assertEqual(self.state()[0]['gmail'], '2')

    def test_pages_duplicate_versions_and_bootstrap(self):
        first = item('opaque-a')
        q = Qmd([page([first], False), page([item('opaque-b'), item('opaque-c', version='v2')])])
        q.feed_since = CUT
        f = feed.Feed(q, '1999-01-01', UNTIL)
        self.assertEqual(len(f.rows('gmail')), 2)
        self.assertEqual(q.calls[0][-2:], ('--since-arrival', CUT))
        self.assertEqual(q.calls[1][-2:], ('--after', 'opaque-a'))
        self.assertEqual(f.state['positions']['gmail'], 'opaque-c')
        self.assertEqual(len(f.seen), 2)
        self.assertEqual(f.counts['gmail']['skipped_duplicate_versions'], 1)
        self.assertNotIn('seen', f.state)

    def test_future_prefix_is_not_skipped_including_seen_or_tombstone(self):
        for changes in ({'time': 9999999999}, {'arrived': 9999999999}, {'time': 9999999999, 'tombstone': True}):
            with self.subTest(changes=changes):
                q = Qmd([page([item('a'), item('hidden', **changes), item('c', version='v3')])])
                f = feed.Feed(q, CUT, UNTIL)
                self.assertEqual(len(f.rows('gmail')), 1)
                self.assertEqual(f.state['positions']['gmail'], 'a')
                self.assertEqual(len(f.seen), 1)
                q2 = Qmd([page([item('hidden', time=1792454401, version='v2'), item('c', version='v3')])])
                q2.feed_positions = {'gmail': 'a'}
                resumed = feed.Feed(q2, CUT, '2026-10-21T00:00:00Z')
                self.assertEqual(len(resumed.rows('gmail')), 2)
                self.assertEqual(q2.calls[0][-2:], ('--after', 'a'))
        q = Qmd([page([item('hidden', time=9999999999)])])
        f = feed.Feed(q, CUT, UNTIL)
        self.assertEqual(f.rows('gmail'), [])
        self.assertIsNone(f.state['positions']['gmail'])

    def test_tombstone_withdraws_earlier_arrival_in_the_same_window(self):
        # 2026-10-21 14:40: the owner deleted mail after it arrived; gsd kept the deleted id and
        # `qmd get` of the whole batch exited 3, failing every steady run.
        f = feed.Feed(Qmd([page([item('1', id='gmail:a', item='gmail:a'), item('2', id='gmail:b', item='gmail:b'),
                                 item('3', id='gmail:a', item='gmail:a', tombstone=True)])]), CUT, UNTIL)
        self.assertEqual([r['id'] for r in f.rows('gmail')], ['gmail:b'])
        self.assertEqual(f.counts['gmail']['skipped_tombstone'], 1)
        self.assertEqual(f.counts['gmail']['withdrawn_by_tombstone'], 1)

    def test_hidden_first_entry_keeps_original_bootstrap_after_empty_cut(self):
        f = feed.Feed(Qmd([page([item('future', time=1792454401)])]), CUT, UNTIL)
        self.assertEqual(f.rows('gmail'), [])
        draft.commit(self.attach(f.state), self.target, actor=store.PIPELINE_ACTOR)  # the step runner commits feed windows
        with store.connect(self.target) as db:
            self.assertEqual(store.pipeline_cut(db)['until'], store.iso(UNTIL))
            qmd = Qmd([page([item('future', time=1792454401)])])
            qmd.feed_positions = feed.positions(db)
            qmd.feed_bootstraps = feed.bootstraps(db)
        resumed = feed.Feed(qmd, UNTIL, '2026-10-21T00:00:00Z')
        self.assertEqual(len(resumed.rows('gmail')), 1)
        self.assertEqual(qmd.calls[0][-2:], ('--since-arrival', CUT))

    def test_missing_primary_document_fails_without_checkpoint(self):
        from test_selection import FakeQmd
        class Missing(FakeQmd):
            def __call__(self, *args):
                if args[0] == 'get':
                    return {'id': args[1], 'docs': []}
                return super().__call__(*args)
        for source in ('gmail', 'whatsapp', 'fathom'):
            with self.subTest(source=source):
                qmd = Missing({source: [{'id': source + ':missing', 'time': CUT}]}, {}, {})
                with self.assertRaisesRegex(ValueError, 'missing primary document'):
                    selection.select(qmd, CUT, UNTIL)
                self.assertEqual(self.state(), ({}, {}))
                qmd = Missing({source: [{'id': source + ':gone', 'time': CUT, 'tombstone': True}]}, {}, {})
                self.assertEqual(selection.select(qmd, CUT, UNTIL)['docs'], {})

    def test_contract_mismatch_is_failure_and_no_checkpoint(self):
        bad = page([item()])
        del bad['items'][0]['version']
        for value in (None, {}, bad, page([item(source='whatsapp')]), page([item()], source='whatsapp')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    feed.Feed(Qmd([value]), CUT, UNTIL).rows('gmail')
        self.assertEqual(self.state(), ({}, {}))

    def test_empty_commit_rollback_retry_and_stale_draft(self):
        f = feed.Feed(Qmd([page([item()])]), CUT, UNTIL)
        f.rows('gmail')
        path = self.attach(f.state)
        stale = self.attach(f.state, 'stale')
        self.assertEqual(self.state(), ({}, {}))  # dry/uncommitted draft
        with patch.object(feed, 'commit', side_effect=ValueError('synthetic failure')):
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                draft.commit(path, self.target)
        self.assertEqual(self.state(), ({}, {}))
        with store.connect(self.target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM change_sets').fetchone()[0], 1)
        draft.commit(path, self.target)
        self.assertEqual(self.state()[0], {'gmail': 'one'})
        with self.assertRaisesRegex(ValueError, 'position moved'):
            draft.commit(stale, self.target)
        self.assertEqual(len(self.state()[1]), 1)
        restate = self.attach(None, 'restate')
        draft.commit(restate, self.target)
        self.assertEqual(self.state()[0], {'gmail': 'one'})

    def test_empty_filtered_execute_persists_and_bootstraps_at_committed_cut(self):
        def select(qmd, since, until, extras):
            self.assertEqual(qmd.feed_since, CUT)
            f = feed.Feed(Qmd([page([item()])]), CUT, UNTIL)
            f.rows('gmail')
            return {'manifest': {}, 'docs': {}, 'feed': f.state}
        args = SimpleNamespace(db=self.target, dry=False, extra=[], tags=False)
        run = self.root / 'run'
        run.mkdir()
        with patch('gsd.inbox.window'):
            out = step.execute(args, run, 'empty', '2000-01-01T00:00:00Z', UNTIL, select, None)
        self.assertEqual(out['state'], 'empty')
        self.assertEqual(self.state()[0], {'gmail': 'one'})
        with store.connect(self.target) as db:
            self.assertEqual(store.pipeline_cut(db)['until'], store.iso(UNTIL))

    def test_filtered_only_real_selection_commits_but_dry_does_not(self):
        from test_selection import FakeQmd, doc, render
        def fake():
            return FakeQmd({'gmail': [{'id': 'gmail:alias', 'time': CUT}]},
                           {'gmail:alias': doc('gmail:alias', 'recruiter@organisation.test',
                                              [('body', 'Application')], to='recruitment@organisation.test')},
                           {'gmail:alias': render()})
        args = SimpleNamespace(db=self.target, dry=True, extra=[], tags=False)
        run = self.root / 'dry'
        run.mkdir()
        with patch.object(step, 'Qmd', return_value=fake()), patch('gsd.inbox.window'), patch.object(step, 'capture'):
            result = step.execute(args, run, 'dry', CUT, UNTIL, selection.select, None)
        self.assertEqual(result['state'], 'dry')
        self.assertEqual(result['manifest']['mail']['dropped_by_reason'], {'drop:not-addressed': 1})
        self.assertEqual(self.state(), ({}, {}))
        args.dry = False
        run = self.root / 'filtered'
        run.mkdir()
        with patch.object(step, 'Qmd', return_value=fake()), patch('gsd.inbox.window'):
            result = step.execute(args, run, 'filtered', CUT, UNTIL, selection.select, None)
        self.assertEqual(result['state'], 'empty')
        self.assertEqual(self.state()[0]['gmail'], '1')

    def test_no_committed_cut_is_not_replaced_by_now_or_since(self):
        with store.connect(self.target, True) as db:
            db.execute('DELETE FROM change_sets')
        args = SimpleNamespace(db=self.target, dry=True, extra=[], tags=False)
        run = self.root / 'no-cut'
        run.mkdir()
        with self.assertRaisesRegex(ValueError, 'last committed pipeline cut'):
            step.prepare(args, run, 'none', CUT, UNTIL, selection.select)
        self.assertEqual(self.state(), ({}, {}))

    def test_migration_backup_is_additive_and_idempotent(self):
        with store.connect(self.target, True) as db:
            db.executescript('DROP TABLE feed_positions; PRAGMA user_version=3;')
            store.migrate(db)
            store.migrate(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
        backups = list((self.root / 'backups').glob('pre-feed-*.sqlite'))
        self.assertEqual(len(backups), 1)
        with store.connect(backups[0]) as db:
            self.assertFalse(store.has_table(db, 'feed_positions'))
            self.assertEqual(store.pipeline_cut(db)['until'], CUT)

    def test_enumeration_environment_only_is_unceiled(self):
        run = self.root / 'qmd-run'
        (run / 'captures').mkdir(parents=True)
        with patch.dict(os.environ, {'QMD_UNTIL': 'inherited'}):
            qmd = step.Qmd(run, UNTIL)
        with patch.object(step.subprocess, 'run', return_value=SimpleNamespace(stdout='{}', stderr='', returncode=0)) as invoke:
            qmd('feed', '--json')
            self.assertNotIn('QMD_UNTIL', invoke.call_args.kwargs['env'])
            qmd('get', 'gmail:synthetic', '--json')
            self.assertEqual(invoke.call_args.kwargs['env']['QMD_UNTIL'], UNTIL)


if __name__ == '__main__':
    unittest.main()
