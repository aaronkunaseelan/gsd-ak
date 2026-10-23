"""D28 regression tests."""
import sqlite3
import unittest
from gsd import write, packets, tags
from pathlib import Path
import tempfile
from unittest.mock import patch
import json


class Outcomes(unittest.TestCase):
    def test_provenance_decisions_additions_and_stage_usage(self):
        from gsd import runs
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'captures').mkdir()
            changes = [
                {'change_id': 'a1', 'decision': 'accept'},
                {'change_id': 'a2', 'decision': 'amend'},
                {'change_id': 'misnamed', 'decision': 'reject', 'deltas': [{'after': {'actor': 'a'}}]},
                {'change_id': 'b-r1-1', 'decision': 'accept'},
            ]
            (run / 'changes.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in changes))
            for agent in ('a', 'r1'):
                (run / 'captures' / (agent+'.result.json')).write_text(json.dumps({
                    'total_cost_usd': 1.5, 'usage': {'input_tokens': 10, 'output_tokens': 2}}))
            result = runs.measure(run)
            self.assertEqual([result[k] for k in ('a_changes', 'r1_accepted', 'r1_amended', 'r1_rejected', 'r1_added')], [3, 1, 1, 1, 1])
            self.assertEqual(result['a_cost_usd'], 1.5)
            self.assertEqual(result['r1_output_tokens'], 2)
            self.assertNotIn('context_mode', result)

    def test_runs_is_read_only_and_contract_valid(self):
        from gsd import runs, contract_validate
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'steady-runs').mkdir()
            run = root / 'steps' / 'steady-test'
            run.mkdir(parents=True)
            (run / 'manifest.json').write_text(json.dumps({'since': '2026-10-17', 'until': '2026-10-18', 'state': 'empty'}))
            (root / 'steady-runs' / 'record.json').write_text(json.dumps({'since': '2026-10-17', 'until': '2026-10-18', 'links': [{'step': 'steady-test'}]}))
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            value = runs.command(SimpleNamespace(db=root / 'does-not-exist.sqlite', since='2026-10-17'))
            self.assertEqual(len(value['runs']), 1)
            self.assertEqual(value['runs'][0]['counts']['a_changes'], 0)
            self.assertNotIn('context_mode', value['runs'][0])
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})
            schema = json.loads((Path(__file__).resolve().parents[1] / 'contracts/gsd.runs.v1.json').read_text())
            self.assertEqual(contract_validate.problems(schema, value), [])

    def test_no_context_option(self):
        import subprocess
        root = Path(__file__).resolve().parents[1]
        for verb in ('steady', 'step'):
            result = subprocess.run([str(root / 'bin/gsd'), verb, '--help'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertNotIn('--context-mode', result.stdout)

    def test_missing_is_not_zero(self):
        from gsd import runs
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(runs.measure(Path(tmp))['a_changes'])


class Tagging(unittest.TestCase):
    def test_sparse_and_unknown_ids(self):
        row = {'id': 'u', 'projects': {'p': .8, 'unknown': .2},
               'p_none': 0, 'p_people_event': 0, 'reason': 'hint'}
        result = tags.validate({'units': [row]}, [{'id': 'u'}], ['p', 'other'])
        self.assertEqual(result[0]['projects'], {'p': .8})
        self.assertEqual(result[0]['unknown_project_ids'], 1)

    def test_malformed_and_retry_visible_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'captures').mkdir()
            envelope = type('Response', (), {'stdout': json.dumps({'result': 'bad'}),
                                             'stderr': '', 'returncode': 0})()
            with patch.object(tags.subprocess, 'run', return_value=envelope):
                with self.assertRaises(ValueError):
                    tags.haiku(run, [{'id': 'u', 'kind': 'mail', 'text': 'x'}], [{'id': 'p'}])
            counts = json.loads((run / 'result.json').read_text())['tag_counts']
            self.assertEqual(counts, {'retries': 1, 'malformed_batches': 2, 'unknown_project_ids': 0, 'name_keys_mapped': 0})


    def test_steady_tallies_failed_tag_calls(self):
        from gsd import steady
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            calls = [{'batch': 1, 'attempt': 0, 'malformed': True, 'usage': {}},
                     {'batch': 1, 'attempt': 1, 'malformed': True, 'usage': {}, 'name_keys_mapped': 2}]
            (run / 'tags-calls.json').write_text(json.dumps(calls))
            record = {'counts': dict.fromkeys(('proposed', 'accepted', 'amended', 'rejected', 'held'), 0),
                      'links': [], 'weighted_tokens': 0, 'model_calls': 0}
            steady.tally(record, run)
            self.assertEqual(record['counts']['tags_malformed_batches'], 2)
            self.assertEqual(record['counts']['tags_retries'], 1)
            self.assertEqual(record['counts']['tags_name_keys_mapped'], 2)
            self.assertIsNone(record['counts']['a_changes'])


class OCR(unittest.TestCase):
    def test_ocr_ref(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        ref = 'whatsapp:account:message/att/1/ocr'
        self.assertEqual(write.qmd_ref(db, ref), ref)
        self.assertIn('OCR text of screenshots is evidence', packets.contract())
