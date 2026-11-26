"""Retention: old step folders gain a verified remote cold copy, old backups go.

A fake rclone stands in for the remote: it copies into a temporary directory and
answers `hashsum sha1` from it, so nothing here touches a network or the store.
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import cli, export, retention, runs

PREFIX = 'test_remote:example-backups/gsd/'
OLD_DAYS = 30

FAKE_RCLONE = '''#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
if os.environ.get('FAKE_RETENTION_LOG'):
    with open(os.environ['FAKE_RETENTION_LOG'], 'a') as out:
        out.write(json.dumps(args) + chr(10))
prefix = os.environ['FAKE_RETENTION_PREFIX']
remote = Path(os.environ['FAKE_RETENTION_REMOTE'])

def local(spec):
    assert spec.startswith(prefix), spec
    return remote / spec[len(prefix):]

if args[0] == 'copyto':
    assert args[1] == '--immutable', args
    source, target = Path(args[2]), local(args[3])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != source.read_bytes():
        sys.exit(6)
    shutil.copyfile(source, target)
    sys.exit(0)
if args[0] == 'hashsum':
    assert args[1] == 'sha1', args
    target = local(args[2])
    if not target.is_file():
        sys.exit(3)
    digest = hashlib.sha1(target.read_bytes()).hexdigest()
    if os.environ.get('FAKE_RETENTION_WRONG'):
        digest = '0' * 40
    print(digest + '  ' + target.name)
    sys.exit(0)
sys.exit(2)
'''


class Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'store'
        self.root.mkdir()
        self.db = self.root / 'gsd.sqlite'
        self.remote = self.base / 'remote'
        self.log = self.base / 'rclone.jsonl'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        rclone = self.bin / 'rclone'
        rclone.write_text('#!' + sys.executable + '\n' + FAKE_RCLONE)
        rclone.chmod(0o700)
        self.env = {
            'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
            'GSD_OWNER_CONFIG': str(ROOT / 'tests/owner.json'),
            'FAKE_RETENTION_PREFIX': PREFIX,
            'FAKE_RETENTION_REMOTE': str(self.remote),
            'FAKE_RETENTION_LOG': str(self.log),
        }

    def sweep(self, dry_run=False, **env):
        with patch.dict(os.environ, {**self.env, **env}, clear=True):
            return retention.sweep(self.root, dry_run=dry_run)

    def make_step(self, name='steady-old', age_days=OLD_DAYS):
        path = self.root / 'steps' / name
        (path / 'captures').mkdir(parents=True)
        (path / 'manifest.json').write_text(json.dumps({'state': 'committed'}))
        (path / 'changes.jsonl').write_text(json.dumps({'change_id': 'a1'}) + '\n')
        (path / 'result.json').write_text(json.dumps({'state': 'committed'}))
        (path / 'status.json').write_text('{}')
        (path / 'captures' / 'a.result.json').write_text(
            json.dumps({'usage': {'input_tokens': 5}, 'total_cost_usd': 0.01})
        )
        (path / 'step.sqlite').write_bytes(b's' * 4096)
        (path / 'open.md').write_text('Synthetic open work')
        self.age(path, age_days)
        return path

    def age(self, path, age_days):
        stamp = time.time() - age_days * 86400
        for entry in [*path.rglob('*'), path]:
            os.utime(entry, (stamp, stamp), follow_symlinks=False)

    def make_backup(self, name='gsd-20261001T000000000000Z.sqlite', age_days=OLD_DAYS, upload=True):
        directory = self.root / 'backups'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(b'b' * 512)
        os.utime(path, (time.time() - age_days * 86400,) * 2)
        if upload:
            self.remote.mkdir(parents=True, exist_ok=True)
            (self.remote / name).write_bytes(path.read_bytes())
        return path

    def snapshot(self, path):
        return {
            entry.relative_to(path).as_posix(): (entry.read_bytes(), entry.stat().st_mtime_ns)
            for entry in sorted(path.rglob('*'))
            if entry.is_file()
        }

    def test_old_step_is_archived_and_keeps_what_runs_and_carry_need(self):
        path = self.make_step()
        before = runs.measure(path)
        self.assertEqual(before['missing'], [])
        self.assertEqual(before['a_input_tokens'], 5)
        result = self.sweep()
        self.assertEqual(result['dry_run'], False)
        self.assertEqual(result['steps_archived'], 1)
        self.assertGreater(result['steps_bytes_freed'], 0)
        self.assertEqual(result['backups_removed'], 0)
        self.assertEqual(result['kept_unverified'], 0)
        self.assertEqual(
            sorted(entry.name for entry in path.iterdir()),
            [
                'archived.json',
                'changes.jsonl',
                'manifest.json',
                'open.md',
                'result.json',
                'status.json',
            ],
        )
        archived = json.loads((path / 'archived.json').read_text())
        tar = self.remote / 'steps' / (path.name + '.tar.zst')
        self.assertTrue(tar.is_file())
        self.assertEqual(archived['remote'], PREFIX + 'steps/' + path.name + '.tar.zst')
        self.assertEqual(archived['sha1'], hashlib.sha1(tar.read_bytes()).hexdigest())
        self.assertEqual(archived['bytes'], tar.stat().st_size)
        self.assertRegex(archived['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$')
        self.assertFalse(list((self.root / 'scratch').glob('*')))
        self.assertEqual((self.root / 'scratch').stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.sweep()['steps_archived'], 0)
        self.assertEqual(json.loads((path / 'archived.json').read_text()), archived)

    def test_archived_step_still_carries_its_open_log(self):
        path = self.make_step()
        self.sweep()
        carried = export.carry(path)
        self.assertIn('Synthetic open work', carried)
        self.assertIn('# Previous step: belief to re-check, not evidence', carried)

    def test_measure_names_archived_and_reports_no_token_or_cost(self):
        path = self.make_step()
        self.sweep()
        measured = runs.measure(path)
        self.assertIn('archived', measured['missing'])
        self.assertEqual(measured['a_changes'], 1)
        for key in (
            'a_input_tokens',
            'a_output_tokens',
            'a_weighted_tokens',
            'a_cost_usd',
            'r1_input_tokens',
            'r1_cost_usd',
        ):
            self.assertNotIn(key, measured)

    def test_runs_command_keeps_the_record_counts_for_an_archived_step(self):
        path = self.make_step()
        runs_dir = self.root / 'steady-runs'
        runs_dir.mkdir()
        record = {
            'since': '2026-10-17',
            'until': '2026-10-18',
            'links': [{'step': path.name}],
            'counts': {
                'weighted_tokens': 30114,
                'model_calls': 3,
                'records_landed': 2,
                'a_changes': 1,
            },
        }
        (runs_dir / 'record.json').write_text(json.dumps(record))
        self.sweep()
        value = runs.command(SimpleNamespace(db=self.db, since='2026-10-17'))
        self.assertEqual(len(value['runs']), 1)
        row = value['runs'][0]
        self.assertEqual(row['steps'], [path.name])
        self.assertEqual(row['counts']['weighted_tokens'], 30114)
        self.assertEqual(row['counts']['model_calls'], 3)
        self.assertEqual(row['counts']['records_landed'], 2)
        self.assertNotIn('a_input_tokens', row['counts'])
        self.assertEqual(row['missing'], [path.name + ': archived'])

    def test_recent_folder_or_fresh_manifest_is_untouched(self):
        fresh = self.make_step('steady-fresh', age_days=1)
        running = self.make_step('steady-running', age_days=OLD_DAYS)
        os.utime(running / 'manifest.json', None)
        before_fresh = self.snapshot(fresh)
        before_running = self.snapshot(running)
        result = self.sweep()
        self.assertEqual(result['steps_archived'], 0)
        self.assertEqual(self.snapshot(fresh), before_fresh)
        self.assertEqual(self.snapshot(running), before_running)
        self.assertFalse((self.remote / 'steps').exists())
        self.assertFalse((self.root / 'scratch').exists())
        self.assertFalse((fresh / 'archived.json').exists())
        self.assertFalse((running / 'archived.json').exists())

    def test_wrong_remote_hash_raises_and_leaves_the_folder_byte_identical(self):
        path = self.make_step()
        before = self.snapshot(path)
        with self.assertRaises(ValueError) as caught:
            self.sweep(FAKE_RETENTION_WRONG='1')
        self.assertIn(path.name, str(caught.exception))
        self.assertEqual(self.snapshot(path), before)
        self.assertFalse((path / 'archived.json').exists())
        self.assertFalse(list((self.root / 'scratch').glob('*.tar.zst')))

    def test_old_backup_removed_only_when_the_remote_matches(self):
        verified = self.make_backup()
        missing = self.make_backup('gsd-20261002T000000000000Z.sqlite', upload=False)
        fresh = self.make_backup('gsd-20261120T000000000000Z.sqlite', age_days=1, upload=False)
        result = self.sweep()
        self.assertEqual(result['backups_removed'], 1)
        self.assertEqual(result['kept_unverified'], 1)
        self.assertFalse(verified.exists())
        self.assertTrue(missing.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(result['steps_archived'], 0)

    def test_dry_run_changes_nothing_and_calls_no_rclone(self):
        path = self.make_step()
        backup = self.make_backup(upload=False)
        before = self.snapshot(path)
        result = self.sweep(dry_run=True)
        self.assertEqual(
            result,
            {
                'steps_archived': 0,
                'steps_bytes_freed': 0,
                'backups_removed': 0,
                'kept_unverified': 0,
                'dry_run': True,
                'would_archive': [path.name],
                'would_remove': [backup.name],
            },
        )
        self.assertEqual(self.snapshot(path), before)
        self.assertTrue(backup.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse(self.remote.exists())
        self.assertFalse((self.root / 'scratch').exists())

    def test_at_most_twenty_steps_oldest_first(self):
        names = [f'steady-{n:02d}' for n in range(21)]
        for n, name in enumerate(names):
            self.make_step(name, age_days=60 - n)
        result = self.sweep()
        self.assertEqual(result['steps_archived'], retention.MAX_STEPS)
        archived = sorted(
            entry.parent.name for entry in (self.root / 'steps').glob('*/archived.json')
        )
        self.assertEqual(archived, names[:retention.MAX_STEPS])
        self.assertEqual(self.sweep()['steps_archived'], 1)

    def test_cli_refuses_the_implicit_live_default_and_touches_nothing(self):
        path = self.make_step()
        out = io.StringIO()
        env = {k: v for k, v in self.env.items() if k != 'GSD_DB'}
        self.assertNotIn('GSD_DB', env)
        with (
            patch.dict(os.environ, env, clear=True),
            contextlib.redirect_stdout(out),
        ):
            code = cli.main(['retention', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('writes require --db FILE or GSD_DB', json.loads(out.getvalue())['error'])
        self.assertFalse((path / 'archived.json').exists())
        self.assertTrue((path / 'captures').is_dir())
        self.assertFalse((self.root / 'scratch').exists())
        self.assertFalse(self.log.exists())
        self.assertFalse(self.remote.exists())

    def test_cli_verb_reads_the_store_parent_and_is_advertised(self):
        path = self.make_step()
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.env, clear=True),
            contextlib.redirect_stdout(out),
        ):
            code = cli.main(['retention', '--db', str(self.db), '--dry-run', '--json'])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['dry_run'], True)
        self.assertEqual(report['would_archive'], [path.name])
        self.assertIn('retention', {spec['name'] for spec in cli.contract()})


if __name__ == '__main__':
    unittest.main()
