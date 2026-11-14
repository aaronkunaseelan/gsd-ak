"""The index graph packing for A: one line per record, and today's packet kept byte for byte."""

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store, packets, read, write

# The suite's synthetic owner, as test_000_config sets it for discovery.
os.environ.setdefault('GSD_OWNER_CONFIG', str(ROOT / 'tests/owner.json'))

WINDOW = '## mail.md\n\nSynthetic window text about Project 00.\n'
SLICE = 'Window: synthetic\nWorking directory: /run\nDraft: step.sqlite\nShort refs: refs.tsv\n'
BODY = 'Synthetic sentence about the work and what moves it next. '


def fixture(path, projects=20, items=10):
    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        store.migrate(raw)
        store.change_set(
            raw, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until='2026-01-01T00:00:00Z'
        )
    with store.connect(path, True) as db:
        for n in range(projects):
            pid = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'project',
                                    'set': {
                                        'name': f'Project {n:02d} synthetic',
                                        'status': 'active',
                                        'current_state': BODY * 6,
                                        'next_action': BODY * 3,
                                        'risk_summary': BODY * 2,
                                    },
                                }
                            ]
                        }
                    ]
                },
                'test',
            )[0]['records'][0]
            write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'item',
                                    'set': {
                                        'title': f'Item {n:02d}-{k:02d} synthetic decision',
                                        'kind': 'task',
                                        'state': 'ready',
                                        'project_id': pid,
                                        'summary': BODY * 4,
                                        'next_actions': BODY * 2,
                                        'waiting_on': BODY,
                                    },
                                }
                            ]
                        }
                        for k in range(items)
                    ]
                },
                'test',
            )


class GraphIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.db = cls.root / 'synthetic.sqlite'
        fixture(cls.db)
        with store.connect(cls.db) as db:
            cls.graph = read.view(db, sparse=True)
            cls.index = read.index(db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_index_is_one_line_per_record_project_grouped(self):
        lines = self.index.splitlines()
        items = [r for r in lines if r.startswith('  - ')]
        self.assertEqual(len(items), 20 * 10)
        self.assertEqual(len([r for r in lines if r.startswith('## ')]), 20)
        self.assertTrue(lines[0].startswith('## Project 00 synthetic ['))
        self.assertRegex(
            lines[1],
            r'^  - [0-9a-f]{8} · item · ready · next=user · \d{4}-\d\d-\d\d'
            r'  Item 00-\d\d synthetic decision$',
        )
        self.assertNotIn('## People', self.index)  # this fixture has none
        self.assertNotIn('Synthetic sentence', self.index)  # no narrative fields
        self.assertLess(len(self.index), len(self.graph) / 3)

    def test_loose_items_get_their_own_group(self):
        with store.connect(self.db, True) as db:
            write.batch(
                db,
                {'changes': [{'ops': [{'upsert': 'item', 'set': {'title': 'Loose item'}}]}]},
                'test',
            )
        with store.connect(self.db) as db:
            text = read.index(db)
            self.assertIn('## Items with no project', text)
            self.assertIn('Loose item', text)
            self.assertNotIn('Loose item', read.index(db, ids=[]))

    def test_index_packet_smaller_than_full(self):
        full, full_sizes = packets.packet(self.root, self.graph, WINDOW, SLICE)
        indexed, sizes = packets.packet(
            self.root, self.graph, WINDOW, SLICE, graph_mode='index', graph_index=self.index
        )
        self.assertLess(len(indexed), 0.4 * len(full))
        self.assertEqual(full_sizes['graph view']['chars'], len(self.graph))
        self.assertEqual(
            sizes['graph view']['chars'], len(packets.INDEX_HINT) + 2 + len(self.index)
        )
        self.assertIn(packets.INDEX_HINT, indexed)
        self.assertIn(packets.INDEX_ROLE, indexed)
        self.assertNotIn(packets.INDEX_HINT, full)
        self.assertNotIn(packets.INDEX_ROLE, full)
        # Reviewer packets stay full even when the run computed the index.
        review, review_sizes = packets.packet(
            self.root, self.graph, WINDOW, SLICE, 'r1', graph_index=self.index
        )
        self.assertEqual(review_sizes['graph view']['chars'], len(self.graph))
        self.assertNotIn(packets.INDEX_HINT, review)
        with self.assertRaises(ValueError):
            packets.packet(self.root, self.graph, WINDOW, SLICE, graph_mode='index')

    def test_graph_mode_full_unchanged(self):
        default, sizes = packets.packet(self.root, self.graph, WINDOW, SLICE)
        explicit, _ = packets.packet(
            self.root, self.graph, WINDOW, SLICE, graph_mode='full', graph_index=self.index
        )
        self.assertEqual(default, explicit)
        # The base commit's assembly, restated here: default output must equal it byte for byte.
        blocks = [
            ('contract', packets.contract()),
            ('graph view', self.graph),
            ('sparse window', WINDOW),
            ('role', packets.A_ROLE),
            ('slice', SLICE),
            ('task', f'Propose this window into the draft; finish {self.root / "open.md"}.'),
        ]
        shared = '\n\n'.join(f'# {name}\n\n{value}' for name, value in blocks[:1])
        user = '\n\n'.join(f'# {name}\n\n{value}' for name, value in blocks[1:])
        user += '\n\nThe runner independently checks the draft and required notes after you finish.\n'
        self.assertEqual(default, shared + '\n\n' + user)
        self.assertEqual(sizes['shared']['chars'], len(shared))
        self.assertEqual(sizes['graph view']['chars'], len(self.graph))


if __name__ == '__main__':
    unittest.main()
