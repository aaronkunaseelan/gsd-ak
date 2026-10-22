"""Vocabulary coherence: synthetic evidence only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import runs, intent, inbox, db as store, status_page
from unittest.mock import patch
import fcntl
import time
import tempfile
import json


class Evidence(unittest.TestCase):
    def test_early_inbox_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'graph.sqlite'
            signal = Path(tmp) / 'telemetry.json'
            with patch.object(inbox, 'TELEMETRY', signal):
                with Path(str(path) + '.notion.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    inbox.pull(path)
                doc = json.loads(signal.read_text())
                self.assertEqual(doc['state'], 'green')
                self.assertTrue(doc['checks'][0]['reason'].startswith('busy:'))
                with inbox.connect(path) as db:
                    inbox.put(db, 'backoff_until', str(time.time() + 600))
                inbox.pull(path)
                doc = json.loads(signal.read_text())
                self.assertEqual(doc['state'], 'red')
                self.assertIn('until', doc['checks'][0]['reason'])

    def test_unreadable_latest_is_not_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(intent.last_gsd_run(directory), (None, False))
            for value in ('{bad', '[]', '{"kind":"other"}', '{"kind":"gsd-steady","counts":[]}',
                          '{"kind":"gsd-steady","counts":{"proposed":null}}',
                          '{"kind":"gsd-steady","since":"2026-10-21T12:00:00"}',
                          '{"kind":"gsd-steady","minutes":null}'):
                (directory / 'latest.json').write_text(value)
                block, failed = intent.last_gsd_run(directory)
                self.assertTrue(failed)
                self.assertIn('unknown:', block['note'])


class OwnerTime(unittest.TestCase):
    def test_naive_instants_rejected(self):
        from gsd import selection
        for parser in (store.iso, intent.when, status_page.at, selection.when, inbox.when):
            with self.subTest(parser=parser):
                with self.assertRaises(ValueError):
                    parser('2026-10-21T12:00:00')
        with self.assertRaises(ValueError):
            store.iso('2026-10-21')
        self.assertEqual(store.notion_date_utc('2026-10-21'), '2026-10-21T00:00:00.000000Z')

    def test_zoned_owner_time(self):
        for stamp, expected in [('2026-10-21T12:00Z', '15:00 EEST'),
                                ('2026-12-21T12:00Z', '14:00 EET'),
                                ('2026-10-21T22:00Z', '01:00 EEST')]:
            self.assertEqual(store.owner_time(stamp, day=False), expected)
            self.assertEqual(status_page.hm(status_page.at(stamp), day=False), expected)


class WorkMeanings(unittest.TestCase):
    def test_due_boundary_and_waiting_people(self):
        from gsd.worklist import past_due, waiting_on_others
        at = '2026-10-21T12:00:00Z'
        item = {'state': 'ready', 'next_actor': 'external', 'due_at': at}
        self.assertFalse(past_due(item, at))
        self.assertTrue(waiting_on_others(item, at))
        self.assertTrue(past_due({**item, 'due_at': '2026-10-21T11:59Z'}, at))
        self.assertFalse(waiting_on_others({**item, 'due_at': '2026-10-21T11:59Z'}, at))
        self.assertFalse(waiting_on_others({**item, 'next_actor': 'agent'}, at))
        self.assertFalse(past_due({**item, 'due_at': None}, at))


class SafeCause(unittest.TestCase):
    def test_qmd_cause_and_private_suppression(self):
        from gsd import step
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'captures').mkdir()
            for stderr, expected in [('private earlier line\n2 ids not found\n',
                                      'QMD read failed (exit 3): 2 ids not found'),
                                     ('PRIVATE TOKEN AND BODY', 'QMD read failed (exit 3): diagnostics suppressed')]:
                with patch('gsd.step.subprocess.run', return_value=CompletedProcess('qmd', 3, '', stderr)):
                    with self.assertRaises(ValueError) as error:
                        step.Qmd(root, '2026-10-21T00:00Z')('get', 'synthetic')
                self.assertEqual(str(error.exception), expected)
                self.assertEqual(runs.outcome({'error': expected})['reason'], expected)


class Outcomes(unittest.TestCase):
    def test_outcomes(self):
        for record, status, result in [
            ({'terminated': True}, 'failed', 'stopped'),
            ({'error': 'PRIVATE'}, 'failed', 'failed'),
            ({'empty_reason': 'nothing', 'counts': {'records_landed': 0}}, 'succeeded', 'quiet'),
            ({'counts': {'records_landed': 3}}, 'succeeded', '3 records landed'),
        ]:
            with self.subTest(record=record):
                value = runs.outcome(record)
                self.assertEqual(value['status'], status)
                self.assertEqual(value['result'], result)
                self.assertNotIn('PRIVATE', value['reason'])
