"""Tagging on either vendor: the pi selector, and the claude rollback byte for byte."""

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
from gsd import db as store, tags, write

# The suite's synthetic owner, as test_000_config sets it for discovery.
os.environ.setdefault('GSD_OWNER_CONFIG', str(ROOT / 'tests/owner.json'))

PI = 'pi:opencode-go-min/deepseek-v4.1-flash'
CLAUDE = 'claude:claude-haiku-5-5'
CARDS = [{'id': 'p0', 'name': 'Synthetic project'}]

# One real pi refusal (F-97), captured as the packet describes: `pi -p --no-session
# --mode json --no-tools` with a fake key. Exit 0; its one turn ends with
# `stopReason: "error"` and this message.
ERROR_STREAM = ROOT / 'tests/pi-provider-error.jsonl'
ERROR_MESSAGE = '401: {"type":"authentication_error","message":"Invalid credential"}'


def unit():
    return {'id': 'unit-1', 'kind': 'mail', 'text': 'Synthetic'}


def events(reply):
    """Pi's JSONL as the docs frame it: two turns, the second one carrying the reply."""
    return [
        {'type': 'session', 'version': 3, 'id': 'synthetic', 'cwd': '/tmp'},
        {
            'type': 'turn_end',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'thinking', 'thinking': 'synthetic'}],
                'usage': {
                    'input': 100,
                    'output': 10,
                    'cacheRead': 80,
                    'cacheWrite': 5,
                    'totalTokens': 195,
                    'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0,
                             'total': 0.0031},
                },
                'stopReason': 'toolUse',
            },
            'toolResults': [],
        },
        {
            'type': 'turn_end',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': reply}],
                'usage': {
                    'input': 20,
                    'output': 5,
                    'cacheRead': 16,
                    'cacheWrite': 0,
                    'totalTokens': 41,
                    'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0,
                             'total': 0.0002},
                },
                'stopReason': 'stop',
            },
            'toolResults': [],
        },
        {'type': 'agent_end', 'messages': [], 'willRetry': False},
        {'type': 'agent_settled', 'aborted': False},
    ]


class PiTags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'captures').mkdir()

    def fake(self, name, body):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o700)
        return path

    def test_tags_pi_argv_and_usage(self):
        self.fake(
            'pi',
            '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['PI_RECORD']).parent
(root / 'pi-argv.json').write_text(json.dumps(sys.argv[1:]))
(root / 'pi-env.json').write_text(json.dumps(sorted(os.environ)))
units = json.loads(sys.stdin.read())['units']
reply = {'units': [{'id': u['id'], 'projects': {'p0': 0.9}, 'p_none': 0.0,
                    'p_people_event': 0.0, 'reason': 'synthetic'} for u in units]}
events = '''
            + repr(events('placeholder'))
            + '''
events[2]['message']['content'] = [{'type': 'text', 'text': json.dumps(reply)}]
for event in events:
    print(json.dumps(event))
''',
        )
        prefix = self.root / 'tags-system.md'
        with patch.dict(
            os.environ,
            {
                'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                'PI_RECORD': str(self.root / 'pi-record'),
            },
        ):
            rows, calls = tags.haiku(self.root, [unit()], CARDS, PI)
        # The reply is the LAST turn's assistant text, not the thinking of the first.
        self.assertEqual(rows[0]['projects'], {'p0': 0.9})
        self.assertEqual(
            json.loads((self.root / 'pi-argv.json').read_text()),
            [
                '-p', '--mode', 'json', '--model', 'opencode-go-min/deepseek-v4.1-flash',
                '--thinking', 'high', '--no-tools', '--no-mcp', '--no-skills',
                '--no-context-files', '--no-prompt-templates', '--no-extensions',
                '--no-session', '--system-prompt', str(prefix),
            ],
        )
        self.assertNotIn(
            'CLAUDE_CODE_MAX_OUTPUT_TOKENS',
            json.loads((self.root / 'pi-env.json').read_text()),
        )
        self.assertEqual(prefix.read_text(), tags.CONTRACT + json.dumps(CARDS, ensure_ascii=False))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['exit'], 0)
        self.assertEqual(
            calls[0]['usage'],
            {'input_tokens': 120, 'output_tokens': 15,
             'cache_read_input_tokens': 96, 'cache_creation_input_tokens': 5},
        )
        self.assertAlmostEqual(calls[0]['cost_usd'], 0.0033)
        self.assertEqual(
            json.loads((self.root / 'tags-calls.json').read_text())[0]['usage'],
            calls[0]['usage'],
        )

    def fake_stream(self, records, name='stream.jsonl'):
        """A `pi` that prints one JSONL stream and exits 0, whatever it holds."""
        path = self.root / name
        path.write_text('\n'.join(json.dumps(record) for record in records) + '\n')
        return self.fake(
            'pi',
            '\nimport pathlib\nprint(pathlib.Path('
            + repr(str(path))
            + ').read_text(), end="")\n',
        )

    def reply_units(self):
        return {'units': [{'id': 'unit-1', 'projects': {'p0': 0.9}, 'p_none': 0.0,
                           'p_people_event': 0.0, 'reason': 'synthetic'}]}

    def test_pi_provider_error_is_a_provider_failure_not_malformed(self):
        captured = [
            json.loads(line)
            for line in ERROR_STREAM.read_text().splitlines()
            if line.strip()
        ]
        self.fake_stream(captured)
        with patch.dict(
            os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}
        ):
            with self.assertRaisesRegex(ValueError, r'tags call failed \(pi:') as caught:
                tags.haiku(self.root, [unit()], CARDS, PI)
            # The provider's own refusal, carried as such, and never retried.
            self.assertIn(ERROR_MESSAGE, str(caught.exception))
            self.assertNotIn('malformed', str(caught.exception))
            calls = json.loads((self.root / 'tags-calls.json').read_text())
            self.assertEqual([(call['attempt'], call['exit']) for call in calls], [(0, 0)])
            # The same refusal followed by a good turn is the reply, not a failure.
            refusal = [
                record for record in captured if record.get('type') == 'turn_end'
            ][0]
            good = events(json.dumps(self.reply_units()))
            self.fake_stream([good[0], refusal, good[2], good[3], good[4]], 'retried.jsonl')
            rows, calls = tags.haiku(self.root, [unit()], CARDS, PI)
        self.assertEqual(rows[0]['projects'], {'p0': 0.9})
        self.assertEqual(len(calls), 1)

    def test_pi_aborted_run_names_the_model(self):
        self.fake(
            'pi',
            '''
import json
for event in '''
            + repr([dict(e, aborted=True) if e['type'] == 'agent_settled' else e
                    for e in events('placeholder')])
            + ''':
    print(json.dumps(event))
''',
        )
        with patch.dict(
            os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}
        ):
            with self.assertRaisesRegex(ValueError, r'tags call failed \(pi:'):
                tags.haiku(self.root, [unit()], CARDS, PI)

    def test_tags_model_flag_claude_rollback(self):
        dbpath = self.root / 'base.sqlite'
        with sqlite3.connect(dbpath) as raw:
            raw.row_factory = sqlite3.Row
            store.migrate(raw)
            store.change_set(
                raw, 'seed', store.PIPELINE_ACTOR, 'pipeline',
                window_until='2026-01-01T00:00:00Z',
            )
        with store.connect(dbpath, True) as db:
            project = write.batch(
                db,
                {
                    'changes': [
                        {
                            'ops': [
                                {
                                    'upsert': 'project',
                                    'set': {'name': 'Synthetic project', 'status': 'active'},
                                }
                            ]
                        }
                    ]
                },
                'test',
            )[0]['records'][0]
        self.fake(
            'qmd',
            '''
import json, sys
args = sys.argv[1:]
doc = {'id': 'gmail:account:one', 'source': 'gmail', 'kind': 'email',
       'time': '2026-01-02T00:00:00Z', 'title': 'Synthetic', 'thread': 'thread',
       'from': 'Pat <pat@example.org>', 'to': 'owner@example.test',
       'parts': [{'kind': 'authored', 'text': 'Send the report due 2026-01-04'}]}
if args[0] == 'feed':
    src = args[args.index('--source') + 1]
    item = {'token': '1', 'id': doc['id'], 'item': doc['id'], 'source': src,
            'kind': 'email', 'time': 1767312000, 'arrived': 1767312000, 'title': None,
            'from': None, 'thread': None, 'chat': None, 'version': 'v1', 'tombstone': False,
            'reparse': False}
    items = [item] if src == 'gmail' else []
    print(json.dumps({'schema': 'qmd.feed.v1', 'source': src, 'items': items,
                      'next': '1' if items else None, 'head': '1' if items else None,
                      'complete': True}))
else:
    print(json.dumps({'id': doc['id'], 'docs': [doc], 'text': ''}))
''',
        )
        self.fake(
            'claude',
            '''
import json, os, pathlib, sys
a = sys.argv[1:]
root = pathlib.Path(os.environ['CLAUDE_RECORD']).parent
(root / 'claude-argv.json').write_text(json.dumps(a))
assert a[a.index('--output-format') + 1] == 'json'
units = json.loads(sys.stdin.read())['units']
reply = {'units': [{'id': u['id'], 'projects': {__PROJECT__: 0.9}, 'p_none': 0.0,
                    'p_people_event': 0.0, 'reason': 'synthetic'} for u in units]}
print(json.dumps({'result': json.dumps(reply), 'usage': {'input_tokens': 3},
                  'total_cost_usd': 0.001}))
'''.replace('__PROJECT__', repr(project)),
        )
        self.fake('pi', 'echo "pi must not run" >&2\nexit 9\n')
        run = self.root / 'run'
        env = dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ['PATH'],
            CLAUDE_RECORD=str(self.root / 'claude-record'),
        )
        p = subprocess.run(
            [
                str(ROOT / 'bin/gsd'), 'step', '--db', str(dbpath),
                '--since', '2026-01-01T00:00:00Z', '--until', '2026-01-03T00:00:00Z',
                '--run', str(run), '--a', 'synthetic', '--review', 'synthetic',
                '--tags-model', CLAUDE, '--dry', '--json',
            ],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)['state'], 'dry')
        # Today's tagging argv (before the selector) byte for byte, program name aside.
        self.assertEqual(
            json.loads((self.root / 'claude-argv.json').read_text()),
            [
                '-p', '--model', 'claude-haiku-5-5', '--effort', 'high',
                '--output-format', 'json', '--system-prompt-file',
                str(run / 'tags-system.md'), '--tools', '', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
                '--no-session-persistence', '--setting-sources', '', '--safe-mode',
            ],
        )
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertEqual(manifest['tags']['model'], CLAUDE)
        self.assertEqual(manifest['graph_mode'], 'full')


if __name__ == '__main__':
    unittest.main()
