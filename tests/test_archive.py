"""Local frozen evidence and a fake immutable remote; no live stores or network."""
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
from gsd import db as store, draft
from gsd.backup import backup


class Archives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'graph.sqlite'
        with sqlite3.connect(self.target) as db:
            store.migrate(db)
        (self.root / 'bin').mkdir()
        fake = self.root / 'bin/rclone'
        fake.write_text((ROOT / 'tests/fake_rclone.py').read_text())
        fake.chmod(0o700)
        self.env = {
            'PATH': str(fake.parent) + os.pathsep + os.environ['PATH'],
            'FAKE_REMOTE': str(self.root / 'remote'),
            'FAKE_RCLONE_FAIL': '',
            'FAKE_RCLONE_LOG': str(self.root / 'rclone.jsonl'),
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()
        patch.object(store, 'LIVE', self.target).start()

    def snapshot(self, step_id):
        run = self.root / 'steps' / step_id
        source = run / 'step.sqlite'
        draft.snapshot(self.target, source, step_id, '2026-10-17T00:00:00Z', '2026-10-18T00:00:00Z')
        return source

    def cli(self, *args):
        return subprocess.run([str(ROOT / 'bin/gsd'), *args, '--db', str(self.target), '--json'],
                              capture_output=True, text=True, check=True)

    def test_two_live_commits_with_same_basename_and_immutable_retry(self):
        for step_id in ('first', 'second'):
            source = self.snapshot(step_id)
            draft.commit(source, self.target)
        remote = self.root / 'remote/steps'
        self.assertEqual({p.name for p in remote.iterdir()}, {'first.sqlite', 'second.sqlite'})
        self.assertNotEqual((remote / 'first.sqlite').read_bytes(), (remote / 'second.sqlite').read_bytes())
        before = {p.name: p.read_bytes() for p in remote.iterdir()}
        draft.upload_archives(self.target)
        self.assertEqual(before, {p.name: p.read_bytes() for p in remote.iterdir()})
        # The fake must actually enforce immutability, not just record arguments.
        (self.root / 'archive/first.sqlite').write_bytes(b'different bytes')
        with self.assertRaisesRegex(ValueError, r'archive failed \(exit 6\)'):
            draft.upload_archives(self.target)
        self.assertEqual((remote / 'first.sqlite').read_bytes(), before['first.sqlite'])

    def test_keeps_existing_frozen_bytes_and_historical_remote_step(self):
        source = self.snapshot('keep')
        frozen = Path(draft.archive(source, self.target))
        before = frozen.read_bytes()
        with store.connect(source, True) as db:
            store.put_meta(db, 'extra-review-note', 'later local edit')
        self.assertEqual(draft.archive(source, self.target), str(frozen))
        self.assertEqual(frozen.read_bytes(), before)
        legacy = self.root / 'remote/steps/step.sqlite'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'historical remote object')
        draft.upload_archives(self.target)
        self.assertEqual(legacy.read_bytes(), b'historical remote object')
        self.assertEqual((legacy.parent / 'keep.sqlite').read_bytes(), before)

    def test_freeze_precedes_commit_and_upload_follows_commit(self):
        source = self.snapshot('ordered')
        freeze = draft.archive
        upload = draft.upload_archives

        def freeze_before(source, target):
            with store.connect(target) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM change_sets WHERE step_id='ordered'").fetchone())
            return freeze(source, target)

        def upload_after(target):
            with store.connect(target) as db:
                self.assertIsNotNone(db.execute("SELECT 1 FROM change_sets WHERE step_id='ordered'").fetchone())
            self.assertTrue((self.root / 'archive/ordered.sqlite').exists())
            upload(target)

        with patch.object(draft, 'archive', side_effect=freeze_before), patch.object(draft, 'upload_archives', side_effect=upload_after):
            draft.commit(source, self.target)

    def test_nonlive_commit_never_archives(self):
        source = self.snapshot('scratch')
        with patch.object(store, 'LIVE', self.root / 'unused.sqlite'):
            result = draft.commit(source, self.target)
        self.assertIn('skipped', result['archive'])
        self.assertFalse((self.root / 'archive').exists())
        self.assertFalse((self.root / 'remote').exists())

    def test_readonly_check_and_local_recovery_do_not_reapply_or_upload(self):
        source = self.snapshot('recover')
        with patch.object(store, 'LIVE', self.root / 'unused.sqlite'):
            draft.commit(source, self.target, note='gsd-step-run:' + str(source.parent))
        with store.connect(self.target) as db:
            before = (store.maxima(db), [tuple(r) for r in db.execute('SELECT * FROM change_sets')])
        self.assertEqual(json.loads(self.cli('archive-check').stdout),
                         [{'step_id': 'recover', 'run': str(source.parent)}])
        self.assertFalse((self.root / 'archive').exists())
        self.assertFalse((self.root / 'gsd.lock').exists())
        result = json.loads(self.cli('archive-recover', 'recover').stdout)
        frozen = Path(result['archive']).read_bytes()
        self.cli('archive-recover', 'recover')
        self.assertEqual(Path(result['archive']).read_bytes(), frozen)
        self.assertEqual(json.loads(self.cli('archive-check').stdout), [])
        self.assertFalse((self.root / 'remote').exists())
        with store.connect(self.target) as db:
            self.assertEqual(before, (store.maxima(db), [tuple(r) for r in db.execute('SELECT * FROM change_sets')]))

    def test_recovery_refuses_mismatched_step_identity(self):
        source = self.snapshot('wrong')
        with store.connect(self.target, True) as db:
            store.change_set(db, 'pipeline:right', store.PIPELINE_ACTOR, 'pipeline', step_id='right',
                             note='gsd-step-run:' + str(source.parent))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            draft.recover_archive(self.target, 'right')
        self.assertFalse((self.root / 'archive').exists())

    def test_unverified_backup_never_enters_upload_set(self):
        root = self.root / 'backups'
        with patch.object(store, 'maxima', side_effect=[{'x': 1}, {'x': 2}]):
            with self.assertRaisesRegex(ValueError, 'source advanced'):
                backup(self.target, root)
        self.assertEqual(list(root.glob('gsd-*.sqlite')), [])
        backup(self.target, root)
        uploaded = self.root / 'remote/backups'
        self.assertEqual(len(list(uploaded.iterdir())), 1)

    def test_backup_directory_retries_only_snapshot_names(self):
        root = self.root / 'backups'
        root.mkdir()
        for name in ('gsd.pre-catchup-example.sqlite', 'gsd.pre-catchup-example.sqlite-wal',
                     'gsd.pre-catchup-example.sqlite-shm', 'gsd-older.sqlite-wal'):
            (root / name).write_bytes(b'do not upload')
        with patch.dict(os.environ, FAKE_RCLONE_FAIL='9'):
            with self.assertRaisesRegex(ValueError, r'backup failed \(exit 9\)'):
                backup(self.target, root)
        pending = list(root.glob('gsd-*.sqlite'))
        self.assertEqual(len(pending), 1)
        backup(self.target, root)
        expected = {p.name for p in root.glob('gsd-*.sqlite')}
        self.assertEqual(len(expected), 2)
        uploaded = self.root / 'remote/backups'
        self.assertEqual({p.name for p in uploaded.iterdir()}, expected)
        for path in uploaded.iterdir():
            self.assertEqual(path.read_bytes(), (root / path.name).read_bytes())
