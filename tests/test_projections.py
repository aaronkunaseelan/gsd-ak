"""Synthetic real SQLite and HTTP edges for projection commands."""

import importlib
import json
import os
from unittest.mock import patch
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db, notion
from gsd.backup import backup
import sqlite3
import subprocess


def load_tests(loader, tests, pattern):
    if (db.ROOT / 'port').is_dir():
        tests.addTests(loader.loadTestsFromModule(importlib.import_module('port.test_port')))
    return tests


class Projections(unittest.TestCase):
    def test_commands_and_backup(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'graph.sqlite'
            with sqlite3.connect(path) as conn:
                db.migrate(conn)
            subprocess.run(
                [
                    str(db.ROOT / 'bin/gsd'),
                    'upsert',
                    'project',
                    '--db',
                    str(path),
                    '--set',
                    'name=Example',
                ],
                input='{}',
                text=True,
                capture_output=True,
                check=True,
            )
            with db.connect(path) as conn:
                first = notion.render(conn)
                self.assertEqual(first, notion.render(conn))
                self.assertEqual(len(first), 1)
                self.assertIn('## Summary', first[0]['body'])
            with patch.dict(os.environ, HOME=root, NOTION_API_TOKEN=''):
                self.assertEqual(notion.publish(path, check=True)['total'], 1)
            from gsd.qmd_publish import publish

            spool = Path(root) / 'qmd/spool/gsd-events'
            self.assertEqual(publish(path, spool)['published'], 1)
            self.assertEqual(publish(path, spool)['published'], 0)
            entries = [
                json.loads(line) for line in (spool / 'index.jsonl').read_text().splitlines()
            ]
            self.assertEqual(len(entries), 1)
            payload = json.loads((spool / entries[0]['path']).read_text())
            self.assertIn('Example', payload['text'])
            self.assertIn('upsert', payload['text'])
            with self.assertRaises(ValueError):
                notion.publish(path, check=True, do_seed=True)
            result = backup(path, Path(root) / 'backups', upload=False)
            self.assertEqual(result['counts']['records'], 1)
            with db.connect(Path(root) / 'backups' / result['object']) as conn:
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
