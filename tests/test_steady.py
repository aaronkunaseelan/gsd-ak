"""Steward contract: real subprocesses and SQLite, synthetic clocks and providers only."""

import contextlib
import fcntl
import io
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsd import db as store
from gsd import steady
from gsd.contract_validate import problems

RECORD_CONTRACT = json.loads((ROOT / 'contracts/vendor/steward.record.v1.json').read_text())

NOW = datetime(2026, 10, 17, 18, 30, tzinfo=UTC)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class Steady(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'graph.sqlite'
        with sqlite3.connect(self.db) as db:
            store.migrate(db)
            store.change_set(
                db, 'seed', store.PIPELINE_ACTOR, 'pipeline', window_until=store.iso('2026-10-17T06:15Z')
            )
        self.seed = steady.cursor(self.db)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'commands.jsonl'
        self.status = self.root / 'status.json'
        self.set_status()
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'PATH': str(self.bin) + ':' + os.environ['PATH'],
            'PYTHONPATH': str(ROOT),
            'FAKE_STATUS': str(self.status),
            'FAKE_LOG': str(self.log),
            'FAKE_REMOTE': str(self.root / 'remote'),
            'FAKE_RCLONE_LOG': str(self.root / 'rclone.jsonl'),
            'GSD_DB': str(self.db),
        }
        for key in (
            'GSD_LOCK_FD',
            'GSD_STEP_ARGV',
            'STEWARD_RUN_DIR',
            'FAKE_MODE',
            'FAKE_FAIL_VERB',
            'FAKE_RCLONE_FAIL',
        ):
            self.env.pop(key, None)
        self.executable(
            'qmd',
            '''import json, os, pathlib, sys
if sys.argv[1:] == ['status', '--json']:
    print(pathlib.Path(os.environ['FAKE_STATUS']).read_text())
else:
    source = sys.argv[sys.argv.index('--source')+1]
    print(json.dumps({'schema':'qmd.feed.v1','source':source,'items':[], 'next':None,'head':None,'complete':True}))
''',
        )
        self.executable('rclone', (ROOT / 'tests/fake_rclone.py').read_text())
        self.executable(
            'gsd',
            '''import json, os, pathlib, sys, time, uuid
from gsd import db as store, draft, write, export
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as out:
    out.write(json.dumps(args)+'\\n')
if args[0] != 'step':
    if os.environ.get('FAKE_FAIL_VERB') == ' '.join(args[:2]):
        print('PRIVATE PROVIDER DIAGNOSTIC', file=sys.stderr)
        sys.exit(9)
    if args[0] == 'backup':
        from gsd.backup import backup
        backup(os.environ['GSD_DB'])
    if args[:2] == ['publish', 'notion']:
        mode = os.environ.get('FAKE_MODE')
        report = {'pages': [], 'published': 0}
        if '--check' in args:
            from gsd.notion import publish_log
            time.sleep(float(os.environ.get('FAKE_CHECK_DELAY', '0')))
            publish_log(os.environ['GSD_DB'], {'requests': {}, 'plan': {},
                'projection_check': {'known': True, 'classes': {}}})
        if mode == 'notion-refused':
            report['refused'] = 'PRIVATE SCHEMA DETAILS'
        elif mode == 'notion-incomplete':
            report['pages'] = [{'errors': [{'unmapped_relations': ['PRIVATE RECORD ID']}]}]
        print(json.dumps(report))
    sys.exit(0)
def arg(name): return args[len(args)-args[::-1].index(name)]
path = pathlib.Path(arg('--db'))
run = pathlib.Path(arg('--run'))
run.mkdir(parents=True, mode=0o700)
(run/'captures').mkdir()
mode = os.environ.get('FAKE_MODE')
def commit_draft(source, note):
    try:
        return draft.commit(source, path, arg('--until'), note+str(run), 'pipeline')
    except ValueError as exc:
        print(json.dumps({'ok':False, 'error':str(exc)}))
        sys.exit(1)
if mode == 'empty':
    draft.snapshot(path, run/'step.sqlite', run.name, arg('--since'), arg('--until'))
    store.LIVE = path
    commit = commit_draft(run/'step.sqlite', 'gsd-empty-run:')
    (run/'commit.json').write_text(json.dumps(commit))
    (run/'result.json').write_text(json.dumps({'state':'empty', 'commit':commit}))
    sys.exit(0)
if mode == 'noadvance': sys.exit(0)
for name in ('a', 'r1'):
    (run/('agent-'+name+'.json')).write_text(json.dumps({
        'session_id': str(uuid.uuid4()), 'pid': os.getpid(), 'state': 'proven',
    }))
d = run/'step.sqlite'
draft.snapshot(path, d, run.name, arg('--since'), arg('--until'))
with store.connect(d, True) as db:
    result = write.batch(db, {'changes':[
        {'id':'a'+str(n), 'why':'Synthetic', 'ops':[{'upsert':'item', 'set':{'title':'Synthetic '+str(n)}}]}
        for n in range(4)]}, 'a')
    assert all(r['ok'] for r in result)
    assert all(r['ok'] for r in draft.decide(db, [
        {'id':'a0', 'decision':'accept'},
        {'id':'a1', 'decision':'amend', 'quote':{'ref':'gmail:synthetic', 'text':'A synthetic quote'}, 'set':{'title':'Amended'}},
        {'id':'a2', 'decision':'reject', 'quote':{'ref':'gmail:synthetic', 'text':'Not needed'}},
        {'id':'a3', 'decision':'hold'},
    ], 'reviewer'))
    (run/'status.json').write_text(json.dumps(draft.status(db)))
    (run/'changes.jsonl').write_text(''.join(json.dumps(c)+'\\n' for c in export.changes(db)))
(run/'open.md').write_text('Synthetic open work')
usage = {'input_tokens':10, 'cache_creation_input_tokens':20, 'cache_read_input_tokens':30, 'output_tokens':10000}
for name in ('a', 'g1'):
    (run/'captures'/(name+'.usage.json')).write_text(json.dumps([{'usage':usage}]))
(run/'tags-calls.json').write_text(json.dumps([{'usage':usage}]))
# Deliberately duplicate aggregates: they must not count a second time.
(run/'manifest.json').write_text(json.dumps({'a_totals':{'usage':usage}, 'tags':{'tokens':usage}}))
if mode == 'failed':
    print('PRIVATE PROVIDER DIAGNOSTIC', file=sys.stderr)
    sys.exit(7)
if mode == 'wait':
    import time
    (run/'waiting').touch()
    time.sleep(60)
# Exercise the actual live-commit flock with a synthetic live path and fake rclone.
store.LIVE = path
commit = commit_draft(d, 'gsd-step-run:')
(run/'commit.json').write_text(json.dumps(commit))
(run/'result.json').write_text(json.dumps({'state':'committed', 'commit':commit}))
''',
        )

    def executable(self, name, content):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + content)
        path.chmod(0o700)

    def set_status(self, **overrides):
        values = dict.fromkeys(steady.SOURCES, NOW.timestamp())
        values.update(overrides)
        values = {
            s: datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
            if isinstance(v, str) else v
            for s, v in values.items()
        }
        payload = {
            'schema': 'qmd.status.v1', 'installed_git_sha': None, 'at': NOW.timestamp(), 'alive': True,
            'heartbeat_at': NOW.timestamp(),
            'verify': {'state': 'green', 'at': NOW.timestamp(), 'red_checks': []},
            'sources': {s: {
                'fresh_through': v, 'lag_s': None, 'current': None if v is None else v >= NOW.timestamp()-3600,
                'limit_s': 3600, 'cadence_s': 1800, 'error': None, 'rate_limited': False,
                'last_complete': NOW.timestamp() + 3600,
            } for s, v in values.items()},
        }
        self.assertEqual(problems(steady.STATUS_CONTRACT, payload), [])
        self.status.write_text(json.dumps(payload))

    def set_cut(self, value):
        with store.connect(self.db, True) as db:
            # Fixture setup only; production cuts are append-only.
            db.execute('UPDATE change_sets SET window_until=?', (store.iso(value),))
        self.seed = steady.cursor(self.db)

    def invoke(self, **env):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {**self.env, **env}, clear=True),
            patch.object(steady, 'datetime', Clock),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(output),
        ):
            result = steady.run(self.db)
        if not env.get('STEWARD_RUN_DIR'):
            self.assertEqual(json.loads(output.getvalue()), result)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
        else:
            self.assertEqual(output.getvalue(), '')
        self.assertEqual(problems(RECORD_CONTRACT, result), [])
        summaries = sorted((self.root / 'steady-runs').glob('*.json'))
        self.assertTrue(summaries)
        self.assertEqual(json.loads(summaries[-1].read_text()), result)
        self.assertEqual(summaries[-1].stat().st_mode & 0o777, 0o600)
        self.assertFalse(list((self.root / 'steady-runs').glob('*.tmp')))
        return result

    def commands(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def steps(self):
        return [c for c in self.commands() if c[0] == 'step']

    def assert_session_links(self, record):
        expected = []
        for call in self.steps():
            run = Path(call[call.index('--run') + 1])
            for name in ('a', 'r1'):
                receipt = json.loads((run / f'agent-{name}.json').read_text())
                expected.append({
                    'session': receipt['session_id'], 'agent': 'claude',
                    'label': name, 'step': run.name,
                })
        self.assertEqual(record['links'], expected)

    def cli(self, *args, **env):
        return subprocess.run(
            [str(ROOT / 'bin/gsd'), *args],
            text=True,
            capture_output=True,
            env={**self.env, **env},
            timeout=10,
            check=False,
        )

    def test_normal_window_high_high_tags_and_publish_order(self):
        result = self.invoke()
        self.assertEqual(result['since'], self.seed)
        self.assertEqual(result['until'], store.iso('2026-10-17T18:15Z'))
        self.assertEqual(result['steps'], 1)
        self.assertIsNone(result['empty_reason'])
        self.assertEqual(result['weighted_tokens'], 30114)
        self.assertEqual(result['model_calls'], 3)
        self.assertEqual(
            {k: result['counts'][k] for k in ('proposed', 'accepted', 'amended', 'rejected', 'held', 'records_landed')},
            {
                'proposed': 4,
                'accepted': 1,
                'amended': 1,
                'rejected': 1,
                'held': 1,
                'records_landed': 2,
            },
        )
        self.assertNotIn('context_mode', result)
        self.assertIn('a_changes', result['counts'])
        self.assertIn('r1_added', result['counts'])
        self.assertEqual(result['notion'], 'skipped (missing notion.token, notion-check-reviewed)')
        self.assertEqual(result['counts']['tags_retries'], 0)
        self.assertGreaterEqual(result['minutes'], 0)
        self.assertEqual(result['published'], ['archive', 'publish qmd', 'backup'])
        self.assertEqual([c[0] for c in self.commands()], ['step', 'publish', 'backup'])
        step = self.steps()[0]
        for flag in ('--effort-a', '--effort-review'):
            self.assertEqual(step[step.index(flag) + 1], 'high')
        self.assertIn('--tags', step)
        self.assertNotIn('--a', step)
        self.assertEqual(steady.cursor(self.db), result['until'])
        self.assert_session_links(result)

    def test_morning_checks_after_publish_once_per_day_and_measures_cost(self):
        config = self.home / '.config/gsd'
        config.mkdir(parents=True)
        for name in ('notion.token', 'notion-check-reviewed'):
            (config / name).touch()
        morning = NOW.replace(hour=3, minute=40)
        with patch(__name__ + '.NOW', morning):
            result = self.invoke(FAKE_CHECK_DELAY='0.05')
            commands = self.commands()
            self.assertEqual(commands[-2][:2], ['publish', 'notion'])
            self.assertNotIn('--check', commands[-2])
            self.assertIn('--check', commands[-1])
            self.assertGreaterEqual(result['counts']['notion_check_seconds'], 0.05)
            # The real publisher timestamps its check; use the synthetic clock's day.
            log = self.root / 'notion-publish.jsonl'
            entry = json.loads(log.read_text().splitlines()[-1])
            entry['timestamp'] = morning.isoformat()
            log.write_text(json.dumps(entry) + '\n')
            self.invoke()
            self.assertEqual(sum('--check' in c for c in self.commands()), 1)
        self.invoke()
        self.assertEqual(sum('--check' in c for c in self.commands()), 1)

    def test_qmd_clips_to_oldest_fresh_through_not_last_complete(self):
        self.set_status(whatsapp='2026-10-17T16:00:00+02:00', fathom='2026-10-17T15:00Z')
        status = json.loads(self.status.read_text())
        status['sources']['whatsapp']['rate_limited'] = True
        status['sources']['whatsapp']['error'] = 'Synthetic rate limit'
        self.status.write_text(json.dumps(status))
        result = self.invoke()
        self.assertEqual(result['until'], store.iso('2026-10-17T14:00Z'))
        self.assertEqual(result['steps'], 1)
        self.assertEqual(result['bounded_by'], 'whatsapp')
        self.assertEqual(set(result['fresh_through']), set(steady.SOURCES))
        self.assertLessEqual(steady.timestamp(result['until']).timestamp(), min(result['fresh_through'].values()))

    def test_empty_behind_is_success_with_reason(self):
        self.set_status(whatsapp='2026-10-17T06:15Z')
        result = self.invoke()
        self.assertIn('qmd whatsapp fresh_through bounds', result['empty_reason'])
        self.assertEqual(result['bounded_by'], 'whatsapp')
        self.assertEqual(result['steps'], 0)
        self.assertEqual(result['links'], [])
        self.assertEqual(steady.cursor(self.db), self.seed)
        self.assertEqual([c[0] for c in self.commands()], ['publish', 'backup'])
        self.assertEqual(result['published'], ['archive', 'publish qmd', 'backup'])
        self.set_cut('2026-10-17T18:20Z')
        self.set_status()
        self.assertIn('now minus 15 minutes', self.invoke()['empty_reason'])

    def test_three_day_boundary_and_outage_two_day_pieces(self):
        for days, expected in [(3, 1), (3.01, 2), (5, 3)]:
            with self.subTest(days=days):
                until = NOW - timedelta(minutes=15)
                self.set_cut((until - timedelta(days=days)).isoformat())
                self.log.unlink(missing_ok=True)
                result = self.invoke()
                self.assertEqual(result['steps'], expected)
                calls = self.steps()
                self.assertEqual(len(calls), expected)
                start = self.seed
                for call in calls:
                    since, end = [call[call.index(f) + 1] for f in ('--since', '--until')]
                    self.assertEqual(since, start)
                    if expected > 1:
                        self.assertLessEqual(
                            steady.timestamp(end) - steady.timestamp(since), timedelta(days=2)
                        )
                    start = end
                self.assertEqual(start, result['until'])
                self.assert_session_links(result)
                self.assertEqual(sum(c[0] == 'backup' for c in self.commands()), 1)
                self.assertEqual(sum(c[:2] == ['publish', 'qmd'] for c in self.commands()), 1)

    def test_failed_draft_cut_unchanged_fresh_retry_and_finalise(self):
        with (
            self.assertRaisesRegex(ValueError, 'step failed'),
            patch.dict(os.environ, {**self.env, 'FAKE_MODE': 'failed'}, clear=True),
            patch.object(steady, 'datetime', Clock),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            steady.run(self.db)
        record = json.loads(out.getvalue())
        self.assertEqual(record['steps'], 0)
        self.assertEqual(record['weighted_tokens'], 30114)
        self.assert_session_links(record)
        self.assertEqual(problems(RECORD_CONTRACT, record), [])
        self.assertEqual(steady.cursor(self.db), self.seed)
        self.assertEqual([c[0] for c in self.commands()], ['step', 'publish', 'backup'])
        self.assertEqual(record['published'], ['archive', 'publish qmd', 'backup'])
        old = self.steps()[0]
        abandoned = Path(old[old.index('--run') + 1])
        before = (abandoned / 'step.sqlite').read_bytes()
        self.invoke()
        self.assertNotEqual(self.steps()[1][-2], old[-2])
        self.assertEqual((abandoned / 'step.sqlite').read_bytes(), before)

    def test_archive_failure_still_counts_the_committed_step_and_records(self):
        self.env['FAKE_RCLONE_FAIL'] = '9'
        with (
            self.assertRaisesRegex(ValueError, 'step failed'),
            patch.dict(os.environ, self.env, clear=True),
            patch.object(steady, 'datetime', Clock),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            steady.run(self.db)
        record = json.loads(out.getvalue())
        self.assertEqual(problems(RECORD_CONTRACT, record), [])
        self.assertEqual(steady.cursor(self.db), store.iso('2026-10-17T18:15Z'))
        run = Path(self.steps()[0][-2])
        self.assertFalse((run / 'commit.json').exists())
        self.assertEqual(record['steps'], 1)
        self.assertEqual(record['counts']['records_landed'], 2)
        self.assertEqual(record['weighted_tokens'], 30114)
        self.assert_session_links(record)
        self.assertTrue(record['error'])
        self.assertEqual(record['published'], ['publish qmd'])
        self.assertIn('rclone archive failed (exit 9)', record['error'])
        self.assertIn('archive:', record['error'])
        self.assertIn('backup:', record['error'])
        self.assertNotIn('PRIVATE', out.getvalue())

    def test_failed_upload_healed_by_quiet_followup(self):
        with (
            self.assertRaisesRegex(ValueError, 'rclone archive failed \\(exit 9\\)'),
            patch.dict(os.environ, {**self.env, 'FAKE_RCLONE_FAIL': '9'}, clear=True),
            patch.object(steady, 'datetime', Clock),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            steady.run(self.db)
        archives = list((self.root / 'archive').glob('*.sqlite'))
        pending_backups = list((self.home / '.local/share/gsd/backups').glob('gsd-*.sqlite'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(len(pending_backups), 1)
        frozen = archives[0].read_bytes()
        self.assertFalse((self.root / 'remote').exists())
        # The committed cut already equals until: no step or models on retry.
        result = self.invoke()
        self.assertEqual(result['steps'], 0)
        self.assertEqual(len(self.steps()), 1)
        self.assertEqual(result['published'], ['archive', 'publish qmd', 'backup'])
        self.assertEqual(archives[0].read_bytes(), frozen)
        self.assertEqual((self.root / 'remote/steps' / archives[0].name).read_bytes(), frozen)
        for path in pending_backups:
            self.assertEqual((self.root / 'remote/backups' / path.name).read_bytes(), path.read_bytes())

    def test_qmd_failure_does_not_suppress_backup_or_notion(self):
        gate = self.home / '.config/gsd'
        gate.mkdir(parents=True)
        (gate / 'notion.token').touch()
        (gate / 'notion-check-reviewed').touch()
        with (
            self.assertRaisesRegex(ValueError, 'publish qmd: gsd publish failed'),
            patch.dict(os.environ, {**self.env, 'FAKE_FAIL_VERB': 'publish qmd'}, clear=True),
            patch.object(steady, 'datetime', Clock),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            steady.run(self.db)
        record = json.loads(out.getvalue())
        self.assertEqual(record['published'], ['archive', 'backup', 'publish notion'])
        self.assertEqual([c[:2] for c in self.commands() if c[0] == 'publish'],
                         [['publish', 'qmd'], ['publish', 'notion']])
        self.assertTrue(list((self.root / 'remote/backups').glob('gsd-*.sqlite')))
        self.assertNotIn('PRIVATE', out.getvalue())

    def test_killed_process_group_leaves_only_abandoned_draft(self):
        code = '''from datetime import datetime, timezone
from unittest.mock import patch
from gsd import steady
from pathlib import Path
steady.LAUNCHER = Path(__LAUNCHER__)
class Clock(datetime):
    @classmethod
    def now(cls, tz=None): return datetime(2026, 10, 17, 18, 30, tzinfo=timezone.utc)
with patch.object(steady, 'datetime', Clock):
    steady.run(__DB__)
'''.replace('__DB__', repr(str(self.db))).replace('__LAUNCHER__', repr(str(self.bin / 'gsd')))
        process = subprocess.Popen(
            [sys.executable, '-c', code],
            env={**self.env, 'FAKE_MODE': 'wait'},
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not list((self.root / 'steps').glob('*/waiting')):
                if time.monotonic() > deadline or process.poll() is not None:
                    self.fail('fake step did not reach the kill boundary')
                time.sleep(0.02)
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        self.assertEqual(steady.cursor(self.db), self.seed)
        old = self.steps()[0][-2]
        self.assertTrue((Path(old) / 'step.sqlite').exists())
        # wait() reaps only the parent; the killed step may still hold its copy
        # of the flock until it is scheduled to exit. Wait for that actual boundary.
        deadline = time.monotonic() + 5
        with (self.root / 'gsd.lock').open('a') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail('killed step retained the inherited flock')
                    time.sleep(0.02)
            fcntl.flock(lock, fcntl.LOCK_UN)
        result = self.invoke()
        self.assertEqual(result['steps'], 1)
        self.assertNotEqual(self.steps()[1][-2], old)

    def test_parent_term_records_and_unwinds_real_step_from_own_release(self):
        release = self.root / 'release'
        shutil.copytree(ROOT / 'gsd', release / 'gsd', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'contracts/vendor', release / 'contracts/vendor')
        (release / 'bin').mkdir()
        launcher = release / 'bin/gsd'
        launcher.write_text('#!' + sys.executable + '\n' + '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import step
from gsd.cli import main

def execute(a, run, *args):
    (run/'captures').mkdir()
    (run/'shared.md').write_text('Synthetic shared packet')
    (run/'prompt-a.md').write_text('Synthetic task')
    (run/'check.py').write_text('raise SystemExit(1)')
    return step.launch(run, 'a', 'synthetic', a.until)
step.execute = execute
sys.exit(main())
''')
        launcher.chmod(0o700)
        # A PATH gsd must never run, even though it precedes the real release.
        self.executable('gsd', "raise SystemExit('PATH gsd must not run')\n")
        self.executable('claude', '''import json, time
print(json.dumps({'type':'assistant', 'message':{'id':'one', 'content':[],
    'usage':{'input_tokens':10, 'output_tokens':7}}}), flush=True)
time.sleep(60)
''')
        directory = self.root / 'steward-term'
        self.set_cut('2026-01-01T00:00:00Z')
        process = subprocess.Popen(
            [str(launcher), 'steady', '--once'],
            env={**self.env, 'STEWARD_RUN_DIR': str(directory)},
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pid = None
        try:
            deadline = time.monotonic() + 10
            while True:
                agents = list((self.root / 'steps').glob('*/agent-a.json'))
                if agents:
                    receipt = agents[0]
                    try:
                        pid = json.loads(receipt.read_text()).get('pid')
                    except ValueError:
                        pass
                    stream = receipt.parent / 'captures/a.stream.jsonl'
                    if pid and stream.exists() and stream.stat().st_size:
                        break
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('real step did not launch fake Claude')
                time.sleep(0.02)
            start = time.monotonic()
            os.killpg(process.pid, signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 1)
            self.assertLess(time.monotonic() - start, 5)
            records = (directory / 'records.jsonl').read_text().splitlines()
            self.assertEqual(len(records), 1)
            record = json.loads(records[0])
            self.assertEqual(problems(RECORD_CONTRACT, record), [])
            self.assertTrue(record['terminated'])
            summaries = list((self.root / 'steady-runs').glob('*.json'))
            self.assertEqual(len(summaries), 1)
            self.assertEqual(json.loads(summaries[0].read_text()), record)
            self.assertIn('TERM', record['error'])
            self.assertEqual(record['weighted_tokens'], 17)
            self.assertEqual(record['model_calls'], 1)
            self.assertEqual(record['published'], [])
            self.assertFalse((self.root / 'rclone.jsonl').exists())
            self.assertFalse((self.root / 'archive').exists())
            self.assertEqual(len(list((self.root / 'steps').iterdir())), 1)
            self.assertEqual(steady.cursor(self.db), self.seed)
            agent = json.loads(receipt.read_text())
            self.assertEqual(agent['state'], 'interrupted')
            self.assertEqual(record['links'], [{
                'session': agent['session_id'], 'agent': 'claude',
                'label': 'a', 'step': receipt.parent.name,
            }])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_step_sigterm_unwinds_detached_claude_group(self):
        run = self.root / 'term-step'
        code = '''import os
from types import SimpleNamespace
from gsd import step

def execute(a, run, *args):
    (run/'captures').mkdir()
    (run/'shared.md').write_text('Synthetic shared packet')
    (run/'prompt-a.md').write_text('Synthetic task')
    (run/'check.py').write_text('raise SystemExit(1)')
    return step.launch(run, 'a', 'synthetic', a.until)
step.execute = execute
step.run(SimpleNamespace(since='2026-01-01T00:00:00Z', until='2026-01-02T00:00:00Z', dry=False,
                        explicit_db=True, a='synthetic', review='synthetic', run=__RUN__))
'''.replace('__RUN__', repr(str(run)))
        self.executable('claude', 'import time\ntime.sleep(60)\n')
        process = subprocess.Popen(
            [sys.executable, '-c', code],
            env={**self.env, 'GSD_LOCK_FD': '0'},
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            receipt = run / 'agent-a.json'
            while True:
                try:
                    pid = json.loads(receipt.read_text()).get('pid')
                except (FileNotFoundError, ValueError):
                    pid = None
                if pid:
                    break
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('fake Claude did not launch')
                time.sleep(0.02)
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 143)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertEqual(json.loads(receipt.read_text())['state'], 'interrupted')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def test_empty_selection_and_noadvance_are_distinct(self):
        result = self.invoke(FAKE_MODE='empty')
        self.assertEqual(result['empty_reason'], 'no selected sources')
        self.assertEqual(result['links'], [])
        self.assertEqual(result['steps'], 1)
        self.assertEqual(result['published'], ['archive', 'publish qmd', 'backup'])
        self.assertEqual(steady.cursor(self.db), result['until'])
        self.set_cut('2026-10-17T06:15Z')
        with (
            self.assertRaisesRegex(ValueError, 'did not advance'),
            patch.dict(os.environ, {**self.env, 'FAKE_MODE': 'noadvance'}, clear=True),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            steady.run(self.db)

    def test_empty_outage_pieces_do_not_strand_cursor(self):
        self.set_cut('2026-10-12T18:15Z')
        result = self.invoke(FAKE_MODE='empty')
        self.assertEqual(result['steps'], 3)
        self.assertEqual(result['empty_reason'], 'no selected sources')
        self.assertEqual(steady.cursor(self.db), result['until'])
        self.assertFalse(any(result['counts'].values()))

    def test_invalid_status_is_real_failure_not_empty(self):
        self.status.write_text('{not json}')
        result = self.cli('steady', '--once')
        self.assertEqual(result.returncode, 1)
        self.assertIn('qmd.status.v1', result.stdout)
        self.assertEqual(steady.cursor(self.db), self.seed)
        record = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(record['published'], ['archive', 'publish qmd', 'backup'])
        self.assertTrue(list((self.root / 'remote/backups').glob('gsd-*.sqlite')))

    def test_status_environment_failures_have_reason_and_nonzero_exit(self):
        for failure in ('schema', 'alive', 'unknown', 'missing', 'type'):
            with self.subTest(failure=failure):
                self.set_status()
                status = json.loads(self.status.read_text())
                if failure == 'schema':
                    status['schema'] = 'old'
                elif failure == 'alive':
                    status['alive'] = False
                elif failure == 'unknown':
                    status['sources']['gmail']['fresh_through'] = None
                elif failure == 'missing':
                    del status['sources']['gmail']
                else:
                    status['sources']['gmail']['fresh_through'] = 'not a number'
                self.status.write_text(json.dumps(status))
                directory = self.root / failure
                result = self.cli('steady', '--once', STEWARD_RUN_DIR=str(directory))
                self.assertEqual(result.returncode, 1)
                record = json.loads((directory / 'records.jsonl').read_text())
                self.assertEqual(problems(RECORD_CONTRACT, record), [])
                self.assertTrue(record['error'])
                self.assertEqual(record['steps'], 0)
                self.assertEqual(record['published'], ['archive', 'publish qmd', 'backup'])
                self.assertEqual(len(self.steps()), 0)

    def test_records_jsonl_append_and_notion_gate(self):
        gate = self.home / '.config/gsd'
        gate.mkdir(parents=True)
        (gate / 'notion.token').touch()
        (gate / 'notion-check-reviewed').touch()
        directory = self.root / 'steward-run'
        directory.mkdir()
        records = directory / 'records.jsonl'
        records.write_text('{"kind":"unrelated"}\n')
        result = self.invoke(STEWARD_RUN_DIR=str(directory))
        lines = records.read_text().splitlines()
        self.assertEqual(json.loads(lines[1]), result)
        self.assert_session_links(json.loads(lines[1]))
        self.assertEqual(problems(RECORD_CONTRACT, json.loads(lines[1])), [])
        self.assertEqual(json.loads(lines[0]), {'kind': 'unrelated'})
        self.assertEqual(len(lines), 2)
        self.assertEqual(result['published'], ['archive', 'publish qmd', 'backup', 'publish notion'])
        self.assertEqual(result['notion'], 'published')
        self.assertEqual(
            set(result),
            {
                'schema', 'provider', 'model_calls', 'terminated', 'links', 'error',
                'bounded_by', 'fresh_through', 'steward_run_id',
                'kind',
                'since',
                'until',
                'empty_reason',
                'steps',
                'counts',
                'weighted_tokens',
                'minutes',
                'published',
                'notion',
            },
        )

    def test_notion_skip_names_missing_review_marker(self):
        gate = self.home / '.config/gsd'
        gate.mkdir(parents=True)
        (gate / 'notion.token').touch()
        result = self.invoke()
        self.assertEqual(result['notion'], 'skipped (missing notion-check-reviewed)')
        self.assertNotIn('publish notion', result['published'])

    def test_notion_refusal_or_incomplete_relations_are_not_claimed_as_published(self):
        gate = self.home / '.config/gsd'
        gate.mkdir(parents=True)
        (gate / 'notion.token').touch()
        (gate / 'notion-check-reviewed').touch()
        for mode in ('notion-refused', 'notion-incomplete'):
            with self.subTest(mode=mode):
                self.set_cut('2026-10-17T06:15Z')
                with (
                    self.assertRaisesRegex(ValueError, 'Notion projection incomplete'),
                    patch.dict(os.environ, {**self.env, 'FAKE_MODE': mode}, clear=True),
                    patch.object(steady, 'datetime', Clock),
                    patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    steady.run(self.db)
                record = json.loads(out.getvalue())
                self.assertEqual(problems(RECORD_CONTRACT, record), [])
                self.assertEqual(steady.cursor(self.db), store.iso('2026-10-17T18:15Z'))
                self.assertEqual(record['steps'], 1)
                self.assertEqual(record['published'], ['archive', 'publish qmd', 'backup'])
                self.assertTrue(record['error'])
                self.assertNotIn('PRIVATE', out.getvalue())

    def test_cli_empty_zero_and_lock_held_fast_nonzero_receipt(self):
        self.set_status(whatsapp=self.seed)
        done = self.cli('steady', '--once')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('qmd whatsapp fresh_through bounds', json.loads(done.stdout)['empty_reason'])
        directory = self.root / 'steward-run'
        with (self.root / 'gsd.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            failed = self.cli('steady', '--once', STEWARD_RUN_DIR=str(directory))
            self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('lock is held', failed.stdout)
        self.assertEqual(failed.stderr.splitlines()[-1], 'lock is held')
        record = json.loads(max((self.root / 'steady-runs').glob('*.json')).read_text())  # newest: names are time-ordered
        self.assertEqual(record['error'], failed.stderr.splitlines()[-1])
        self.assertEqual(len((directory / 'records.jsonl').read_text().splitlines()), 1)
        self.assertFalse(self.commands())
        self.assertEqual(json.loads(done.stdout)['published'], ['archive', 'publish qmd', 'backup'])
        self.assertEqual(json.loads((directory / 'records.jsonl').read_text())['published'], [])

    def test_override_arguments_and_reserved_bounds(self):
        args = [
            '--effort-a',
            'low',
            '--effort-review',
            'high',
            '--no-tags',
            '--since',
            'old',
            '--until',
            '{until}',
        ]
        self.invoke(GSD_STEP_ARGV=json.dumps(args))
        call = self.steps()[0]
        self.assertNotIn('--tags', call)
        # Managed bounds are last, so argparse cannot let an old configured cut win.
        self.assertEqual(
            call[-7:-3], ['--since', self.seed, '--until', store.iso('2026-10-17T18:15Z')]
        )

    def test_publish_failure_nonzero_sanitized_and_cut_already_committed(self):
        with (
            self.assertRaisesRegex(ValueError, 'publish failed'),
            patch.dict(os.environ, {**self.env, 'FAKE_FAIL_VERB': 'publish qmd'}, clear=True),
            patch.object(steady, 'datetime', Clock),
            patch.object(steady, 'LAUNCHER', self.bin / 'gsd'),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            steady.run(self.db)
        self.assertNotEqual(steady.cursor(self.db), self.seed)
        self.assertEqual(json.loads(out.getvalue())['steps'], 1)
        self.assertNotIn('PRIVATE', out.getvalue())

    def test_real_step_empty_selection_commits_without_agents_and_keeps_prior_carry(self):
        run = self.root / 'empty-step'
        result = self.cli(
            'step', '--since', self.seed, '--until', '2026-10-17T18:15Z', '--run', str(run)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'empty')
        self.assertTrue((run / 'step.sqlite').exists())
        self.assertFalse(list(run.glob('agent-*')))
        self.assertEqual(steady.cursor(self.db), store.iso('2026-10-17T18:15Z'))
        from gsd import step

        with store.connect(self.db) as db:
            self.assertIsNone(step.previous_folder(db))
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0], 0)

    def test_tally_preserves_links_and_ignores_unstarted_sessions(self):
        run = self.root / 'steady-synthetic'
        run.mkdir()
        for name, receipt in {
            'a': {'session_id': 'started-session', 'pid': 123, 'state': 'failed'},
            'r1': {'session_id': 'unstarted-session', 'state': 'failed'},
            'r2': {'session_id': None, 'pid': 124},
        }.items():
            (run / f'agent-{name}.json').write_text(json.dumps(receipt))
        existing = {'url': 'https://example.org/run'}
        record = {
            'links': [existing], 'weighted_tokens': 0, 'model_calls': 0,
            'counts': dict.fromkeys(
                ('proposed', 'accepted', 'amended', 'rejected', 'held', 'records_landed'), 0
            ),
        }
        steady.tally(record, run)
        self.assertEqual(record['links'], [existing, {
            'session': 'started-session', 'agent': 'claude',
            'label': 'a', 'step': run.name,
        }])
        self.assertEqual(problems(RECORD_CONTRACT, record), [])

    def test_partial_usage_stream_counts_complete_calls_once(self):
        run = self.root / 'partial'
        captures = run / 'captures'
        captures.mkdir(parents=True)
        message = {
            'type': 'assistant',
            'message': {
                'id': 'one',
                'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20},
                'content': [],
            },
        }
        (captures / 'a.stream.jsonl').write_text(
            json.dumps(message) + '\n' + json.dumps(message) + '\n{"partial":'
        )
        self.assertEqual(steady.usage(run), (12, 1))
        (captures / 'a.usage.json').write_text('[{"usage":{"input_tokens":7}}]')
        self.assertEqual(steady.usage(run), (7, 1))


if __name__ == '__main__':
    unittest.main()
