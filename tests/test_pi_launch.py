"""The pi vendor behind gsd's one launch function: argv, JSONL, and a split relay."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store, draft, pi_launch, step

os.environ.setdefault('GSD_OWNER_CONFIG', str(ROOT / 'tests/owner.json'))

PI = 'pi:opencode-go-min/deepseek-v4.1-flash'
CLAUDE = 'claude:claude-opus-5-5'


def usage(inp=100, out=10, read=80, write=5, cost=0.0031):
    return {
        'input': inp, 'output': out, 'cacheRead': read, 'cacheWrite': write,
        'totalTokens': inp + out + read + write,
        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': cost},
    }


def turn(message_id, content, totals=None, model='deepseek-v4.1-flash'):
    """One pi `turn_end`, as its JSON mode frames it."""
    return {
        'type': 'turn_end',
        'message': {
            'role': 'assistant',
            'content': content,
            'model': model,
            'responseId': message_id,
            'usage': totals or usage(),
            'stopReason': 'stop',
        },
        'toolResults': [],
    }


def events(*turns, aborted=False):
    return [
        {'type': 'session', 'version': 3, 'id': 'synthetic', 'cwd': '/synthetic'},
        {'type': 'agent_start'},
        {'type': 'turn_start'},
        *turns,
        {'type': 'agent_end', 'messages': [], 'willRetry': False},
        {'type': 'agent_settled', 'aborted': aborted},
    ]


def stream(path, *records):
    path.write_text('\n'.join(json.dumps(record) for record in records) + '\n')
    return path


# One real pi failure (F-97), captured as the packet describes: `pi -p --no-session
# --mode json --model opencode-go-min/deepseek-v4.1-flash --no-tools 'say hi'` with
# a fake key, exit 0, and its one turn ending with `stopReason: "error"`.
ERROR_STREAM = Path(__file__).resolve().parent / 'pi-provider-error.jsonl'
ERROR_MESSAGE = '401: {"type":"authentication_error","message":"Invalid credential"}'


def error_records():
    return [json.loads(line) for line in ERROR_STREAM.read_text().splitlines() if line.strip()]


class Pi(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / 'base.sqlite'
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            store.migrate(db)
            store.change_set(
                db, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until='2025-12-30T00:00:00Z'
            )

    def fake(self, name, body):
        path = self.root / name
        path.write_text('#!/usr/bin/env python3\n' + body)
        path.chmod(0o700)
        return path

    def run_folder(self, name, agent='a'):
        run = self.root / name
        run.mkdir()
        (run / 'captures').mkdir()
        (run / 'shared.md').write_text('shared')
        (run / f'prompt-{agent}.md').write_text('first message')
        draft.snapshot(
            self.db, run / 'step.sqlite', name, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'
        )
        return run

    # The proposer's own work: one cited change, open.md, and its reflection file.
    # The reflection is written only when the prompt names the file, so a launch that
    # never tells a pi agent where to write it leaves the receipt empty.
    PROPOSER = '''
import json, os, pathlib, subprocess, sys
root = pathlib.Path.cwd()
role = os.environ['GSD_ACTOR']
prompt = sys.stdin.read()
record = pathlib.Path(os.environ['PI_RECORD']) / (role + '.json')
record.parent.mkdir(parents=True, exist_ok=True)
record.write_text(json.dumps({'argv': sys.argv[1:], 'stdin': prompt,
                              'env': sorted(os.environ)}))
def cli(cmd, payload):
    p = subprocess.run([os.environ['GSD_BIN'], cmd, '--db', str(root / 'step.sqlite'), '--json'],
                       input=json.dumps(payload), text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stdout)
if role == 'a':
    cli('write', {'changes': [{'id': 'a1', 'why': 'synthetic', 'confidence': .9,
                               'importance': .2,
                               'ops': [{'upsert': 'item', 'set': {'title': 'Synthetic task'},
                                        'cite': [{'ref': 'gmail:synthetic'}]}]}]})
    (root / 'open.md').write_text('Open after this step: none')
    sidecar = 'reflection-' + role + '.json'
    if sidecar in prompt:
        (root / sidecar).write_text(json.dumps({'reflection': 'Dates changed.'}))
else:
    cli('decide', {'decisions': [{'id': 'a1', 'decision': 'accept', 'by': role}]})
    (root / ('notes-' + role + '.md')).write_text('No disagreements')
for event in EVENTS:
    print(json.dumps(event))
'''

    PI_EVENTS = repr(events(turn('call-1', [{'type': 'text', 'text': 'done'}])))

    CLAUDE_EVENTS = repr([
        {'type': 'assistant', 'message': {'id': 'call', 'model': 'synthetic',
                                          'usage': {'input_tokens': 7}, 'content': []}},
        {'type': 'result', 'subtype': 'success', 'is_error': False},
    ])

    def test_pi_argv_for_proposer(self):
        run = self.run_folder('proposer')
        pi = self.fake(
            'pi', self.PROPOSER.replace('EVENTS', self.PI_EVENTS)
        )
        with (
            patch.object(pi_launch, 'BINARY', str(pi)),
            patch.dict(os.environ, {'PI_RECORD': str(self.root / 'records'),
                                    'GSD_BIN': str(ROOT / 'bin/gsd')}),
        ):
            calls = step.launch(
                run, 'a', PI, '2026-01-02T00:00:00Z', effort_a='medium'
            )
        recorded = json.loads((self.root / 'records/a.json').read_text())
        receipt = json.loads((run / 'agent-a.json').read_text())
        self.assertEqual(
            recorded['argv'],
            [
                '-p', '--mode', 'json', '--session-id', receipt['session_id'],
                '--model', 'opencode-go-min/deepseek-v4.1-flash',
                # gsd's medium has no pi level on this model; the same ask is high.
                '--thinking', 'high',
                '--append-system-prompt', str(run / 'shared.md'),
                '--tools', 'read,bash,write,edit',
                '--no-extensions', '--no-skills', '--no-context-files',
                '--no-prompt-templates', '--no-mcp',
                '--session-dir', str(run / 'sessions'),
            ],
        )
        self.assertNotIn('--json-schema', recorded['argv'])
        self.assertEqual(recorded['stdin'], (run / 'prompt-a.md').read_text())
        self.assertEqual(receipt['vendor'], 'pi')
        self.assertEqual(receipt['model'], 'opencode-go-min/deepseek-v4.1-flash')
        self.assertEqual(receipt['state'], 'proven')
        self.assertEqual(receipt['reflection'], 'Dates changed.')
        self.assertTrue((run / 'sessions').is_dir())
        self.assertEqual(calls[0]['usage']['input_tokens'], 100)

    def test_pi_usage_synthesises_result(self):
        stream(
            self.root / 'pi.jsonl',
            *events(
                turn('call-1', [
                    {'type': 'thinking', 'thinking': 'considering'},
                    {'type': 'toolCall', 'id': 'tool-1', 'name': 'bash', 'arguments': {}},
                ]),
                # A retried response repeats responseId: one row, summed usage.
                turn('call-1', [{'type': 'toolCall', 'id': 'tool-2', 'name': 'read',
                                 'arguments': {}}], totals=usage(20, 5, 16, 0, 0.0002)),
                turn('call-2', [{'type': 'text', 'text': 'done'}], totals=usage(0, 7, 0, 0, 4e-4)),
                aborted=False,
            ),
        )
        calls, result = step.usage(self.root / 'pi.jsonl', exit_code=0, duration_ms=1234)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['usage']['input_tokens'], 120)
        self.assertTrue(calls[0]['tool_use'])
        self.assertEqual([c['name'] for c in calls[0]['tool_calls']], ['bash', 'read'])
        self.assertEqual(calls[0]['thinking_blocks'], 1)
        self.assertEqual(calls[0]['thinking_chars'], len('considering'))
        self.assertEqual(result['subtype'], 'success')
        self.assertFalse(result['is_error'])
        self.assertEqual(result['num_turns'], 2)
        self.assertEqual(result['duration_ms'], 1234)
        self.assertEqual(
            result['usage'],
            {'input_tokens': 120, 'output_tokens': 22,
             'cache_read_input_tokens': 96, 'cache_creation_input_tokens': 5},
        )
        self.assertAlmostEqual(result['total_cost_usd'], 0.0037)
        # The same stream with pi's abort signal, or a failing exit, is not a success.
        stream(
            self.root / 'aborted.jsonl',
            *events(turn('call-1', [{'type': 'text', 'text': 'done'}]), aborted=True),
        )
        _, aborted = step.usage(self.root / 'aborted.jsonl', exit_code=0)
        self.assertEqual(aborted['subtype'], 'error_during_execution')
        self.assertTrue(aborted['is_error'])
        _, failed = step.usage(self.root / 'pi.jsonl', exit_code=3)
        self.assertTrue(failed['is_error'])

    def test_pi_last_turn_provider_error_is_a_failure(self):
        path = stream(self.root / 'provider-error.jsonl', *error_records())
        _, result = step.usage(path, exit_code=0, duration_ms=90)
        self.assertEqual(result['subtype'], 'error_during_execution')
        self.assertTrue(result['is_error'])
        # The turn's own message travels where step's cause naming reads it, and
        # only its class leaves that function.
        self.assertEqual(result['error'], ERROR_MESSAGE)
        self.assertEqual(step.failure_cause(0, result, path.read_text()), 'auth')
        # pi's exit code says nothing here: a clean stream still fails on it alone,
        # and carries no error text.
        good = stream(
            self.root / 'clean.jsonl',
            *events(turn('call-1', [{'type': 'text', 'text': 'done'}])),
        )
        _, clean = step.usage(good, exit_code=3)
        self.assertTrue(clean['is_error'])
        self.assertNotIn('error', clean)
        _, fine = step.usage(good, exit_code=0)
        self.assertEqual(fine['subtype'], 'success')
        # An abort keeps its own class, even with a provider error behind it.
        aborted_path = stream(
            self.root / 'aborted-provider.jsonl',
            *[
                dict(record, aborted=True)
                if record.get('type') == 'agent_settled'
                else record
                for record in error_records()
            ],
        )
        _, aborted = step.usage(aborted_path, exit_code=0)
        self.assertTrue(aborted['is_error'])
        self.assertEqual(step.failure_cause(0, aborted, aborted_path.read_text()), 'aborted')

    def test_pi_provider_error_then_success_is_a_success(self):
        refusal = [
            record for record in error_records() if record.get('type') == 'turn_end'
        ][0]
        good = turn('call-2', [{'type': 'text', 'text': 'hi'}])
        path = stream(self.root / 'retried.jsonl', *events(refusal, good))
        calls, result = step.usage(path, exit_code=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['subtype'], 'success')
        self.assertFalse(result['is_error'])
        self.assertNotIn('error', result)
        # The same two turns the other way round: the last one decides.
        reversed_path = stream(self.root / 'reversed.jsonl', *events(good, refusal))
        _, last = step.usage(reversed_path, exit_code=0)
        self.assertEqual(last['subtype'], 'error_during_execution')
        self.assertEqual(last['error'], ERROR_MESSAGE)

    def test_pi_abort_is_named(self):
        path = stream(
            self.root / 'aborted-reason.jsonl',
            *events(turn('call-1', [{'type': 'text', 'text': 'done'}]), aborted=True),
        )
        _, aborted = step.usage(path, exit_code=0)
        self.assertEqual(aborted['subtype'], 'error_during_execution')
        self.assertEqual(step.failure_cause(0, aborted, path.read_text()), 'aborted')
        # Through a launch the class reaches the envelope, and nothing else does.
        run = self.run_folder('aborted-reason')

        def headless(run, name, args, *rest):
            (run / 'captures' / f'{name}.stream.jsonl').write_text(path.read_text())
            return 0

        with patch.object(step, 'headless', side_effect=headless):
            with self.assertRaises(ValueError) as caught:
                step.launch(run, 'a', PI, '2026-01-02T00:00:00Z')
        self.assertEqual(
            str(caught.exception),
            'session a did not finish successfully: aborted; '
            'inspect private captures, do not retry blindly',
        )
        self.assertNotIn('deepseek-v4.1-flash', str(caught.exception))

    def split_run(self, name):
        """One real step.run over a qmd, a pi and a claude fake: (run, result).

        run is the resolved folder the step itself used, so its paths compare with
        what the agents were told.
        """
        self.fake(
            'qmd',
            '''import json, sys
args = sys.argv[1:]
doc = {'id': 'gmail:synthetic', 'source': 'gmail', 'kind': 'email',
       'time': '2026-01-01T12:00:00Z', 'from': 'test@organisation.test',
       'to': 'owner@example.test', 'title': 'Synthetic source',
       'parts': [{'kind': 'authored', 'text': 'Synthetic evidence'}]}
if args[0] == 'feed':
    source = args[args.index('--source') + 1]
    item = dict(doc, token='1', item=doc['id'], source=source, time=1767268800,
                arrived=1767268800, thread=None, chat=None, version='v1',
                tombstone=False, reparse=False)
    items = [item] if source == 'gmail' else []
    print(json.dumps({'schema': 'qmd.feed.v1', 'source': source, 'items': items,
                      'next': '1' if items else None, 'head': '1' if items else None,
                      'complete': True}))
else:
    print(json.dumps({'id': doc['id'], 'docs': [doc], 'text': ''}))
''',
        )
        pi = self.fake('pi', self.PROPOSER.replace('EVENTS', self.PI_EVENTS))
        claude = self.fake(
            'claude', self.PROPOSER.replace('EVENTS', self.CLAUDE_EVENTS)
        )
        raw = self.root / name
        args = SimpleNamespace(
            since='2026-01-01T00:00:00Z',
            until='2026-01-02T00:00:00Z',
            run=str(raw),
            db=str(self.db),
            dry=False,
            explicit_db=True,
            extra=[],
            a=PI,
            review=CLAUDE,
            tags=False,
        )
        with (
            patch.object(step, 'CLAUDE', str(claude)),
            patch.object(pi_launch, 'BINARY', str(pi)),
            patch.dict(os.environ, {'PI_RECORD': str(self.root / 'records'),
                                    'GSD_BIN': str(ROOT / 'bin/gsd'),
                                    'PATH': str(self.root) + os.pathsep + os.environ['PATH']}),
        ):
            result = step.run(args)
        return raw.resolve(), result

    def test_reviewer_model_can_differ_from_a(self):
        run, result = self.split_run('split')
        self.assertEqual(result['state'], 'committed')
        a = json.loads((self.root / 'records/a.json').read_text())['argv']
        r1 = json.loads((self.root / 'records/r1.json').read_text())['argv']
        self.assertEqual(a[a.index('--model') + 1], 'opencode-go-min/deepseek-v4.1-flash')
        self.assertIn('--mode', a)
        self.assertEqual(r1[r1.index('--model') + 1], 'claude-opus-5-5')
        self.assertIn('stream-json', r1)
        for name, vendor in (('a', 'pi'), ('r1', 'claude')):
            receipt = json.loads((run / f'agent-{name}.json').read_text())
            self.assertEqual((receipt['vendor'], receipt['state']), (vendor, 'proven'))
        self.assertEqual(result['groups']['groups'], {'r1': ['a1']})

    def test_pi_reflection_reaches_receipt_in_production_packet(self):
        # No packet text names the sidecar: the launch path must add the sentence.
        run, result = self.split_run('production-packet')
        self.assertEqual(result['state'], 'committed')
        self.assertIn(str(run / 'reflection-a.json'), (run / 'prompt-a.md').read_text())
        self.assertNotIn('reflection-', (run / 'packet-a.md').read_text())
        receipt = json.loads((run / 'agent-a.json').read_text())
        self.assertEqual((receipt['vendor'], receipt['reflection']),
                         ('pi', 'Dates changed.'))

    def test_claude_reviewer_never_gets_another_agents_reflection(self):
        run = self.run_folder('claude-reviewer')
        (run / 'prompt-r1.md').write_text('review this')
        (run / 'reflection-a.json').write_text(json.dumps({'reflection': 'A saw a date change.'}))
        # The pre-C1 shared file stays ignored too: it is never another agent's floor.
        (run / 'reflection.json').write_text(json.dumps({'reflection': 'A saw a date change.'}))
        claude = self.fake(
            'claude',
            '''import json, sys
sys.stdin.read()
print(json.dumps({'type': 'assistant', 'message': {'id': 'call', 'model': 'synthetic',
                  'usage': {'input_tokens': 7}, 'content': []}}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}))
''',
        )
        with patch.object(step, 'CLAUDE', str(claude)), patch.object(step, 'acceptance'):
            step.launch(run, 'r1', CLAUDE, '2026-01-02T00:00:00Z')
        receipt = json.loads((run / 'agent-r1.json').read_text())
        self.assertEqual((receipt['vendor'], receipt['reflection']), ('claude', ''))
        # claude's prompt is untouched: only pi is told where to write its words.
        self.assertEqual((run / 'prompt-r1.md').read_text(), 'review this')

    def test_pi_launch_refuses_missing_shared_prompt(self):
        # pi sends the path text when the file is missing; the argv must not.
        run = self.root / 'no-shared'
        run.mkdir()
        with self.assertRaises(ValueError):
            pi_launch.argv(run, 'synthetic', 'session', 'medium')
        (run / 'shared.md').write_text('shared')
        argv = pi_launch.argv(run, 'synthetic', 'session', 'medium')
        self.assertEqual(argv[argv.index('--append-system-prompt') + 1], str(run / 'shared.md'))


if __name__ == '__main__':
    unittest.main()
