import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store  # noqa: E402


class CutActor(unittest.TestCase):
    """F-53: only the pipeline's own commits move the cut; a hand-landed draft does not."""

    def test_hand_landing_with_a_later_window_does_not_move_the_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gsd.sqlite'
            with sqlite3.connect(path) as db:
                store.migrate(db)
            with store.connect(path, True) as db:
                store.change_set(db, 'pipeline:20261021T154527-x', store.PIPELINE_ACTOR, 'pipeline',
                                 window_until='2026-10-21T15:45:27.858066Z', note='gsd-step-run:/x')
                store.change_set(db, 'pipeline:example-docs-located', 'claude-code', 'pipeline',
                                 window_until='2026-10-21T20:00:00.000000Z', note='hand landing')
                cut = store.pipeline_cut(db)
            self.assertEqual(cut['until'], '2026-10-21T15:45:27.858066Z')
            self.assertEqual(cut['change_set_id'], 'pipeline:20261021T154527-x')


if __name__ == '__main__':
    unittest.main()
