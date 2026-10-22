"""D29 migrates current states, preserving append-only history and next actors."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gsd import db as store, packets, write

ROOT = Path(__file__).resolve().parents[1]
OLD = ('awaiting_user', 'awaiting_external')


class D29(unittest.TestCase):
    def test_legacy_migration_backs_up_first_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'graph.sqlite'
            with sqlite3.connect(path) as db:
                store.migrate(db)
            with store.connect(path, True) as db:
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='items_state_enum'").fetchone()[0]
                db.execute('DROP TRIGGER items_state_enum')
                db.execute(sql.replace("'done'", "'awaiting_user', 'awaiting_external', 'done'"))
                ids = []
                for state, actor in zip(OLD, ('user', 'external')):
                    result = write.batch(db, {'changes': [{'ops': [{'upsert': 'item',
                        'set': dict(title=state, state=state, next_actor=actor)}]}]}, 'test')
                    self.assertTrue(result[0]['ok'], result)
                    ids.append(result[0]['records'][0])
            with store.connect(path, True) as db:
                counts = store.migrate(db)
                self.assertEqual(counts['awaiting_user'], 1)
                self.assertEqual(counts['awaiting_external'], 1)
                current = store.rows(db, 'items')
                self.assertEqual([r['id'] for r in current], ids)
                self.assertEqual([r['state'] for r in current], ['ready', 'ready'])
                self.assertEqual([r['next_actor'] for r in current], ['user', 'external'])
                self.assertTrue(all(r['change_set_id'] == 'migration:d29' for r in current))
                self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 4)
                self.assertFalse(any(store.migrate(db).values()))
                for state in OLD:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'invalid state'):
                        db.execute('INSERT INTO items SELECT ' + ','.join(
                            '?' if r[1] == 'state' else 'NULL' if r[1] == 'seq' else r[1]
                            for r in db.execute('PRAGMA table_info(items)')) +
                            ' FROM items LIMIT 1', (state,))
            backups = list((Path(tmp) / 'backups').glob('pre-d29-*.sqlite'))
            self.assertEqual(len(backups), 1)
            with store.connect(backups[0]) as db:
                self.assertEqual([r['state'] for r in store.rows(db, 'items')], list(OLD))
                self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 2)

    def test_fresh_enum_and_model_contract(self):
        contract = json.loads((ROOT / 'contracts/gsd.worklist.v1.json').read_text())
        self.assertEqual(contract['properties']['items']['items']['properties']['state']['enum'],
                         ['inbox', 'ready', 'running', 'blocked'])
        self.assertIn('next_actor says who moves next', packets.contract())
        for state in OLD:
            self.assertNotIn(state, packets.contract())
