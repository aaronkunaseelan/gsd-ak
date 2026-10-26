"""D32: each fact has one owner; copies are generated from it or checked here."""

import contextlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import cli, db as store, owner, packets
from gsd.write import rules

PROSE = [ROOT / 'README.md', ROOT / 'AGENTS.md', *sorted((ROOT / 'docs').glob('*.md'))]
# decisions.md is append-only history (D32): old entries keep their wording.
PROSE = [p for p in PROSE if p.name != 'decisions.md']


def schema():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    store.migrate(db)
    return db


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class SingleSource(unittest.TestCase):
    def test_principles_render_into_every_prompt_and_rules(self):
        contract = packets.contract()
        role = ' '.join(packets.REVIEW_ROLE.split())
        first = owner.get('name').split()[0]
        for n, p in enumerate(packets.PRINCIPLES, 1):
            body = packets.render_principle(n, p, first)
            self.assertIn(body, contract)
            self.assertIn(p['name'], role)
        code, out = run('rules', '--json')
        self.assertEqual(code, 0)
        rules_out = json.loads(out)
        self.assertEqual([r['n'] for r in rules_out], list(range(1, len(packets.PRINCIPLES) + 1)))
        self.assertTrue(all('{' not in r['text'] for r in rules_out))
        code, text = run('rules')
        self.assertIn(packets.render_principle(1, packets.PRINCIPLES[0], first), text)

    def test_principles_have_unique_names_and_real_decisions(self):
        names = [p['name'] for p in packets.PRINCIPLES]
        self.assertEqual(len(names), len(set(names)))
        decisions = (ROOT / 'docs/decisions.md').read_text()
        for p in packets.PRINCIPLES:
            if p['decision']:
                self.assertIn(f"## {p['decision']}. ", decisions)

    def test_prompt_vocabularies_match_schema(self):
        enums, _ = rules(schema(), 'items')
        text = packets.CONTRACT_TEMPLATE
        states = re.search(r'Item states describe progress \(([^)]+)\)', text).group(1)
        actors = re.search(r'next_actor says who moves next \(([^)]+)\)', text).group(1)
        self.assertEqual(sorted(states.split(', ')), sorted(enums['state']))
        self.assertEqual(sorted(actors.split(', ')), sorted(enums['next_actor']))

    def test_worklist_contract_vocabularies_match_schema(self):
        enums, _ = rules(schema(), 'items')
        text = (ROOT / 'contracts/gsd.worklist.v1.json').read_text()
        found = [json.loads(m) for m in re.findall(r'"state": \{"enum": (\[[^]]*\])', text)]
        self.assertEqual(sorted(s for group in found for s in group), sorted(enums['state']))
        actors = json.loads(re.search(r'"next_actor": \{"enum": (\[[^]]*\])', text).group(1))
        self.assertEqual(sorted(a for a in actors if a is not None), sorted(enums['next_actor']))

    def test_prose_does_not_restate_vocabularies(self):
        db = schema()
        lists = [vs for t in store.TABLES for vs in rules(db, t)[0].values() if len(vs) >= 3]
        for path in PROSE:
            text = path.read_text()
            for vs in lists:
                for i in range(len(vs) - 2):
                    a, b, c = (re.escape(str(v)) for v in vs[i:i + 3])
                    pattern = rf'\b{a},\s+{b},\s+(?:and\s+|or\s+)?{c}\b'
                    self.assertIsNone(
                        re.search(pattern, text),
                        f'{path.name} restates a vocabulary; point to gsd fields',
                    )

    def test_prose_does_not_restate_owned_values(self):
        owned = {
            r'1\.25\s*[×x*]': 'tokens.weighted_tokens',
            r'[Nn]ine principles': 'packets.PRINCIPLES',
        }
        for path in PROSE:
            text = path.read_text()
            for pattern, where in owned.items():
                self.assertIsNone(re.search(pattern, text), f'{path.name}: name {where}')
        verbs = json.dumps(cli.contract())
        for pattern, where in (owned | {r'schema v\d': 'db.migrate'}).items():
            self.assertIsNone(re.search(pattern, verbs), f'cli.VERBS: name {where}')

    def test_migrate_reports_the_version_it_wrote(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'g.sqlite'
            code, out = run('migrate', '--db', str(path))
            self.assertEqual(code, 0)
            with sqlite3.connect(path) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual(json.loads(out)['schema_version'], version)


if __name__ == '__main__':
    unittest.main()
