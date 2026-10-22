"""Page 2 intent: synthetic stores, no private graph fields or cross-store reads."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store, intent, steady, write

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
CUT = store.iso('2026-10-17T08:00Z')


def status():
    return {
        'schema': 'qmd.status.v1', 'installed_git_sha': None, 'at': NOW.timestamp(), 'alive': True,
        'heartbeat_at': NOW.timestamp(),
        'verify': {'state': 'green', 'at': NOW.timestamp(), 'red_checks': []},
        'sources': {source: {
            'fresh_through': NOW.timestamp() - 60, 'lag_s': None, 'current': True, 'limit_s': 3600,
            'cadence_s': 900, 'error': None, 'rate_limited': False,
        } for source in steady.SOURCES},
    }


class Intent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'graph.sqlite'
        with sqlite3.connect(self.path) as db:
            store.migrate(db)
            db.execute('PRAGMA journal_mode=WAL')
        self.work = self.upsert('project', name='Work', status='active')
        self.life = self.upsert('project', name='Life', status='active',
                                metadata_json='{"scope":"Owner commitments"}')
        self.archived = self.upsert('project', name='Archived', status='archived')
        self.upsert('project', name='Empty', status='active')
        yesterday = '2026-10-16T12:00:00Z'
        today = '2026-10-17T01:00:00Z'
        for fields in (
            {'project_id': self.work, 'next_actor': 'user', 'due_at': '2026-10-17T10:00Z'},
            {'project_id': self.work, 'next_actor': 'agent', 'due_at': '2026-10-19', 'created_at': today},
            {'project_id': self.life, 'next_actor': 'external', 'due_at': '2026-10-24T12:00Z', 'created_at': today},
            {'next_actor': 'none', 'due_at': NOW.isoformat()},
            {'project_id': self.work, 'state': 'done', 'closed_at': '2026-10-17T10:00Z'},
            {'state': 'canceled'},
            {'project_id': self.archived, 'next_actor': 'user'},
        ):
            self.upsert('item', **{'title': 'PRIVATE ITEM TITLE', 'created_at': yesterday, **fields})
        self.upsert('person', display_name='PRIVATE PERSON NAME')
        with store.connect(self.path, True) as db:
            db.execute('INSERT INTO notion_map VALUES (?,?,NULL,NULL,NULL)', (self.work, '1234-5678'))
            store.change_set(db, 'pipeline', store.PIPELINE_ACTOR, 'pipeline', window_until=CUT)
            store.change_set(db, 'not-pipeline', 'test', 'owner', window_until=store.iso('2026-10-19T00:00:00Z'))

    def upsert(self, entity, **fields):
        created = store.iso(fields.pop('created_at', NOW.isoformat()))
        op = {'upsert': entity, 'set': fields}
        if fields.get('state') in ('done', 'canceled'):
            op['cite'] = [{'ref': 'gmail:PRIVATE-QMD-ID', 'role': 'decides', 'excerpt': 'PRIVATE SOURCE TEXT'}]
        with store.connect(self.path, True) as db, patch.object(store, 'now', return_value=created):
            result = write.batch(db, {'changes': [{'ops': [op]}]}, 'test')
        self.assertTrue(result[0]['ok'], result)
        return result[0]['records'][0]

    def document(self, payload=None, now=NOW):
        payload = status() if payload is None else payload
        self.assertEqual(steady.problems(steady.STATUS_CONTRACT, status()), [])
        reply = subprocess.CompletedProcess(['qmd'], 0, json.dumps(payload), '')
        with patch('gsd.steady.subprocess.run', return_value=reply) as run:
            doc = intent.document(self.path, now)
        self.assertEqual(run.call_args.args[0], ['qmd', 'status', '--json'])
        self.assertEqual(run.call_args.kwargs['timeout'], 1)
        return doc

    def test_fixture_expected_blocks_and_pipeline_cut(self):
        doc = self.document()
        self.assertEqual({k: v for k, v in doc.items() if k != 'blocks'}, {
            'schema': 'intent/1', 'system': 'gsd', 'at': int(NOW.timestamp()),
            'state': 'green', 'headline': '2 wait on you; 1 past due',
        })
        stock, flow, rows, chain = doc['blocks']
        self.assertEqual(stock, {
            'kind': 'stock', 'layer': 'intent', 'open': 5, 'you': 2, 'agents': 1,
            'others': 1, 'past_due': 1, 'due_7d': 2, 'fresh_through': CUT,
        })
        with store.connect(self.path) as db:
            self.assertEqual(stock['fresh_through'], store.pipeline_cut(db)['until'])
        self.assertEqual(flow, {
            'kind': 'flow', 'layer': 'process', 'since': '2026-10-17',
            'days': [{'day': '2026-10-17', 'open': 5, 'in': 2, 'out': 0}],
        })
        self.assertEqual(rows, {'kind': 'rows', 'layer': 'intent', 'items': [
            {'name': 'Work', 'you': 1, 'agents': 1, 'others': 0, 'past_due': 1,
             'due_7d': 1, 'life': False, 'link': 'https://www.notion.so/12345678'},
            {'name': 'Life', 'you': 0, 'agents': 0, 'others': 1, 'past_due': 0,
             'due_7d': 0, 'life': True, 'link': None},
        ]})
        self.assertEqual(chain, {'kind': 'chain', 'layer': 'process', 'hops': [
            {'name': name, 'lag_s': 60, 'note': 'complete to 14:59 EEST', 'warn': False}
            for name in ('qmd · WhatsApp', 'qmd · Gmail', 'qmd · Fathom')
        ] + [{'name': 'gsd cut', 'lag_s': 14400,
              'note': 'next cut at the next steady run', 'warn': False}]})
        self.assertNotIn('Steward', json.dumps(doc))
        self.assertFalse(any(b['kind'] == 'funnel' for b in doc['blocks']))

    def test_you_reads_the_producer_priorities(self):
        with patch.object(intent, 'priorities', return_value={'mine': [], 'waiting_on_others': []}) as view:
            doc = self.document()
        self.assertEqual(view.call_count, 1)
        self.assertEqual(doc['blocks'][0]['you'], 0)
        self.assertEqual(doc['blocks'][0]['others'], 1)
        self.assertTrue(all(r['you'] == 0 for r in doc['blocks'][2]['items']))

    def test_flow_athens_boundary_and_31_day_cap(self):
        self.assertEqual(self.document(now=datetime(2026, 10, 16, 20, 59, tzinfo=UTC))['blocks'][1]['days'], [])
        first = self.document(now=datetime(2026, 10, 16, 21, tzinfo=UTC))['blocks'][1]['days']
        self.assertEqual(len(first), 1)
        days = self.document(now=NOW + timedelta(days=90))['blocks'][1]['days']
        self.assertEqual(len(days), 31)
        self.assertEqual(days[-1]['day'], '2026-11-16')

    def test_invalid_or_dead_qmd_is_one_unknown_warn_hop_plus_cut(self):
        for payload in ({}, {**status(), 'schema': 'other'}, {**status(), 'alive': False},
                        {**status(), 'sources': []}):
            with self.subTest(payload=payload):
                doc = self.document(payload)
                self.assertNotEqual(doc['state'], 'green')
                hops = doc['blocks'][3]['hops']
                self.assertEqual(len(hops), 2)
                self.assertEqual(hops[0]['name'], 'qmd')
                self.assertTrue(hops[0]['warn'])
                self.assertIsNone(hops[0]['lag_s'])
                self.assertIn('unknown', hops[0]['note'])
                self.assertEqual(hops[1]['name'], 'gsd cut')

    def test_unreadable_or_malformed_qmd_is_unknown(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('qmd', 1)):
            with patch('gsd.steady.subprocess.run', side_effect=failure):
                hops = intent.chain(NOW, CUT)
            self.assertTrue(hops[0]['warn'])
            self.assertIn('unknown', hops[0]['note'])
        with patch('gsd.steady.subprocess.run', return_value=subprocess.CompletedProcess('qmd', 0, '{bad', '')):
            self.assertTrue(intent.chain(NOW, CUT)[0]['warn'])

    def test_null_or_stale_source_warns_without_provider_text(self):
        payload = status()
        payload['sources']['gmail'].update(fresh_through=None, current=None)
        payload['sources']['fathom'].update(fresh_through=NOW.timestamp() - 4000, current=False, error='PRIVATE PROVIDER TEXT')
        hops = self.document(payload)['blocks'][3]['hops']
        self.assertFalse(hops[0]['warn'])
        self.assertTrue(hops[1]['warn'])
        self.assertTrue(hops[2]['warn'])
        self.assertNotIn('PRIVATE', json.dumps(hops))

    def test_freshness_uses_qmd_lateness_limits_not_a_cadence_heuristic(self):
        payload = status()
        payload['sources']['whatsapp'].update(
            fresh_through=NOW.timestamp() - 600, cadence_s=60, limit_s=3600, current=True,
        )
        payload['sources']['gmail'].update(
            fresh_through=NOW.timestamp() - 8000, cadence_s=3600, limit_s=7200, current=False,
        )
        hops = self.document(payload)['blocks'][3]['hops']
        self.assertFalse(hops[0]['warn'])
        self.assertEqual(hops[0]['lag_s'], 600)
        self.assertTrue(hops[1]['warn'])
        self.assertEqual(hops[1]['note'], 'capture behind')
        for age, warn in ((3600, False), (3600.5, True)):
            with self.subTest(age=age):
                payload['sources']['whatsapp'].update(
                    fresh_through=NOW.timestamp() - age, current=not warn,
                )
                hop = self.document(payload)['blocks'][3]['hops'][0]
                self.assertEqual(hop['lag_s'], 3600)
                self.assertEqual(hop['warn'], warn)

    def test_missing_cut_has_no_cursor_fallback(self):
        with store.connect(self.path, True) as db:
            db.execute("DELETE FROM change_sets WHERE origin='pipeline'")
        doc = self.document()
        self.assertNotEqual(doc['state'], 'green')
        self.assertIsNone(doc['blocks'][0]['fresh_through'])
        self.assertTrue(doc['blocks'][3]['hops'][-1]['warn'])

    def test_own_last_run_summary_funnel_and_no_foreign_reads(self):
        directory = self.root / 'steady-runs'
        directory.mkdir()
        (directory / '000-old.json').write_text('{not json}')
        record = {
            'schema': 'steward.record.v1', 'kind': 'gsd-steady', 'since': '2026-10-17T00:00Z',
            'until': '2026-10-17T08:00Z', 'empty_reason': None, 'minutes': 12.1,
            'weighted_tokens': 851336, 'counts': {'proposed': 16, 'accepted': 14,
                'amended': 2, 'rejected': 0, 'held': 3, 'records_landed': 16},
        }
        (directory / '001-new.json').write_text(json.dumps(record))
        self.assertEqual(self.document()['state'], 'green')
        self.assertEqual(self.document()['blocks'][-1], {
            'kind': 'funnel', 'layer': 'process', 'title': 'Last gsd run',
            'sub': 'Sat 03:00 EEST → Sat 11:00 EEST',
            'steps': [['proposed', 16, ''], ['accepted', 14, ''], ['amended', 2, ''],
                      ['rejected', 0, ''], ['held', 3, ''], ['landed', 16, '']],
            'note': '12 min · 0.85M weighted tokens',
        })
        record.update(since=None, until=None, empty_reason='No selected sources ' * 30)
        (directory / '002-empty.json').write_text(json.dumps(record))
        last = self.document()['blocks'][-1]
        self.assertEqual(last['sub'], ' → ')
        self.assertLessEqual(len(last['note']), 160)
        for terminated, expected in ((False, 'failed'), (True, 'steady stopped by TERM')):
            with self.subTest(terminated=terminated):
                record.update(error='PRIVATE ERROR DETAILS', terminated=terminated)
                (directory / '003-failed.json').write_text(json.dumps(record))
                doc = self.document()
                self.assertEqual(doc['state'], 'red')
                note = doc['blocks'][-1]['note']
                self.assertTrue(note.startswith(expected + ' · '), note)
                self.assertNotIn('PRIVATE', note)
                self.assertLessEqual(len(note), 160)

    def test_privacy_and_bounded_strings(self):
        doc = self.document()
        serialized = json.dumps(doc)
        for private in ('PRIVATE', self.work, self.life, self.archived):
            self.assertNotIn(private, serialized)
        def check(value):
            if isinstance(value, str):
                self.assertLessEqual(len(value), 200)
            elif isinstance(value, dict):
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(doc)
        self.assertLessEqual(len(doc['headline']), 160)
        self.assertEqual(len(intent.clip('long ' * 100, 80)), 80)

    def test_cli_read_only_no_stdout_on_unreadable_database(self):
        env = {**os.environ, 'GSD_DB': str(self.path)}
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        qmd = fake_bin / 'qmd'
        qmd.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps(status())) + ')\n')
        qmd.chmod(0o700)
        env['PATH'] = str(fake_bin) + os.pathsep + env.get('PATH', '')
        with store.connect(self.path, True) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch('gsd.draft.live_lock', side_effect=AssertionError('intent must not lock')):
                self.document()
            done = subprocess.run([str(ROOT / 'bin/gsd'), 'intent', '--json'], env=env,
                                  text=True, capture_output=True, timeout=2)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['system'], 'gsd')
        for name, content in (('missing', None), ('corrupt', 'not sqlite')):
            path = self.root / name
            if content:
                path.write_text(content)
            failed = subprocess.run([str(ROOT / 'bin/gsd'), 'intent', '--json', '--db', str(path)],
                                    env=env, text=True, capture_output=True, timeout=2)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, '')
            if content is None:
                self.assertFalse(path.exists())
        self.assertFalse((self.root / 'gsd.lock').exists())
        self.assertFalse((self.root / 'steady-runs').exists())


if __name__ == '__main__':
    unittest.main()
