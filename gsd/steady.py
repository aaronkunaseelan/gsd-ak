"""One Steward invocation: committed cut to the oldest complete source, then publish.

Only outage windows over three days are split. Abandoned drafts are evidence,
not resumable work: each step gets a fresh UUID folder and only commits move the cut.
"""

import json
import os
import signal
import sqlite3
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db as store
from . import draft
from .contract_validate import problems
from .tokens import weighted_tokens

LAUNCHER = store.ROOT / 'bin/gsd'
STATUS_CONTRACT = json.loads((store.ROOT / 'contracts/vendor/qmd.status.v1.json').read_text())
SOURCES = ('gmail', 'whatsapp', 'fathom')
DEFAULT_ARGS = ['--effort-a', 'high', '--effort-review', 'high', '--tags']


def cursor(path):
    with store.connect(path) as db:
        value = store.pipeline_cut(db)['until']
    if not value:
        raise ValueError('steady requires an installed pipeline cut; bootstrap first')
    return store.iso(value)


def timestamp(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    return datetime.fromisoformat(store.iso(value))


def qmd_status(timeout=None):
    result = subprocess.run(
        ['qmd', 'status', '--json'], capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode:
        raise ValueError('qmd status failed; provider diagnostics suppressed')
    try:
        status = json.loads(result.stdout)
    except ValueError:
        raise ValueError('qmd status is not valid qmd.status.v1 JSON') from None
    if problems(STATUS_CONTRACT, status):
        raise ValueError('qmd status violates qmd.status.v1')
    if not status['alive']:
        raise ValueError('qmd status: alive is false')
    return status


def cutoff(now):
    status = qmd_status()
    fresh = {name: status['sources'].get(name, {}).get('fresh_through') for name in SOURCES}
    for name, value in fresh.items():
        if value is None:
            raise ValueError(f'qmd {name} fresh_through is unknown')
    try:
        synced, source = min((timestamp(value), name) for name, value in fresh.items())
    except (ValueError, OverflowError, OSError):
        raise ValueError('qmd fresh_through is outside the supported time range') from None
    lag = now - timedelta(minutes=15)
    until = min(lag, synced)
    bounded_by = source if synced <= lag else None
    reason = (
        f'qmd {source} fresh_through bounds the cut'
        if bounded_by
        else 'now minus 15 minutes is not after the committed cut'
    )
    return store.iso(until.isoformat()), reason, bounded_by, fresh


def command(args, **kwargs):
    # Never forward provider output: errors can contain credentials or source bodies.
    result = subprocess.run(
        [str(LAUNCHER), *args], capture_output=True, text=True, check=False, **kwargs
    )
    if result.returncode:
        message = f'gsd {args[0]} failed (exit {result.returncode})'
        # Only the child's final CLI error envelope, never stderr/model output.
        lines = result.stdout.splitlines()
        try:
            final = json.loads(lines[-1]) if lines else None
        except ValueError:
            final = None
        if (
            args[0] == 'step'
            and isinstance(final, dict)
            and final.get('ok') is False
            and isinstance(final.get('error'), str)
        ):
            message += ': ' + final['error']
        else:
            message += '; inspect private step artifacts'
        raise ValueError(message)
    return result


def usage(run):
    """Count each Claude call once, including Haiku retries, not manifest totals."""
    from .step import usage as stream_usage

    class Stream:
        def __init__(self, lines):
            self.lines = lines

        def read_text(self):
            return '\n'.join(self.lines)

    calls = []
    captures = run / 'captures'
    for path in captures.glob('*.usage.json'):
        calls.extend(json.loads(path.read_text()))
    # A killed/failed launch may have a stream but no finished usage capture.
    for path in captures.glob('*.stream.jsonl'):
        if not path.with_name(path.name.replace('.stream.jsonl', '.usage.json')).exists():
            # SIGKILL can leave an incomplete final JSON line. Keep complete events.
            lines = []
            for line in path.read_text().splitlines():
                try:
                    json.loads(line)
                except ValueError:
                    break
                lines.append(line)

            rows, _ = stream_usage(Stream(lines))
            calls.extend(rows)
    tags = run / 'tags-calls.json'
    if tags.is_file():
        calls.extend(json.loads(tags.read_text()))
    return sum(weighted_tokens(c['usage']) for c in calls), len(calls)


def tally(record, run, target=None):
    from .runs import measure
    measured = measure(run)
    for key, value in measured.items():
        if key == 'missing':
            continue
        old = record['counts'].get(key, 0)
        record['counts'][key] = old + value if old is not None and value is not None else None
    record['counts']['measurement_missing'] = record['counts'].get('measurement_missing', 0) + len(measured['missing'])
    from .tags import call_counts
    tag_path = run / 'tags-calls.json'
    tag_counts = call_counts(json.loads(tag_path.read_text()) if tag_path.exists() else [])
    for key, value in tag_counts.items():
        name = 'tags_' + key
        record['counts'][name] = record['counts'].get(name, 0) + value
    for path in sorted(run.glob('agent-*.json')):
        agent = json.loads(path.read_text())
        # A receipt is written before launch; only a PID proves it started.
        if agent.get('session_id') and agent.get('pid'):
            record['links'].append({
                'session': agent['session_id'],
                'agent': 'claude',
                'label': path.stem.removeprefix('agent-'),
                'step': run.name,
            })
    tokens, calls = usage(run)
    record['weighted_tokens'] += tokens
    record['model_calls'] += calls
    status = run / 'status.json'
    counts = {}
    if status.is_file():
        counts = json.loads(status.read_text())['counts']
    elif (run / 'step.sqlite').is_file():
        try:
            with store.connect(run / 'step.sqlite') as db:
                if store.has_table(db, 'draft_change'):
                    counts = draft.status(db)['counts']
        except sqlite3.Error:
            pass  # A failed snapshot may not yet be a complete SQLite draft.
    record['counts']['proposed'] += sum(counts.values())
    for key, decision in (
        ('accepted', 'accept'),
        ('amended', 'amend'),
        ('rejected', 'reject'),
        ('held', 'hold'),
    ):
        record['counts'][key] += counts.get(decision, 0)
    commit = run / 'commit.json'
    landed = None
    if commit.is_file():
        landed = json.loads(commit.read_text())['landed_records']
    elif target is not None:
        # Archive/export can fail after SQLite commits but before commit.json is saved.
        with store.connect(target) as db:
            change = db.execute(
                "SELECT change_set_id FROM change_sets WHERE origin='pipeline' AND note IN (?,?)",
                ('gsd-step-run:' + str(run), 'gsd-empty-run:' + str(run)),
            ).fetchone()
            if change:
                landed = {
                    rid
                    for table in store.TABLES
                    for row in db.execute(
                        f'SELECT * FROM {table} WHERE change_set_id=?', (change[0],)
                    )
                    for rid in draft.touched(table, dict(row))
                }
    if landed is not None:
        record['steps'] += 1
        record['counts']['records_landed'] += len(landed)


def emit(record, path):
    record['steward_run_id'] = os.environ.get('STEWARD_RUN_ID')
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
    directory = os.environ.get('STEWARD_RUN_DIR')
    if directory:
        root = Path(directory).expanduser()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(root / 'records.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as out:
            out.write(line)
    else:
        print(line, end='')
    # gsd keeps the same run record for mech-d's intent funnel; no Steward reads.
    root = path.parent / 'steady-runs'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f'{time.time_ns()}-{uuid.uuid4().hex}'
    temporary = root / (name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(line)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, root / (name + '.json'))


def run(path):
    path = Path(path).expanduser().resolve()
    started = time.monotonic()
    from .runs import DECISIONS, TOKENS

    record = {
        'schema': 'steward.record.v1',
        'kind': 'gsd-steady',
        'provider': 'anthropic',
        'model_calls': 0,
        'terminated': False,
        'links': [],
        'error': None,
        'bounded_by': None,
        'fresh_through': {},
        'since': None,
        'until': None,
        'empty_reason': None,
        'steps': 0,
        'counts': dict.fromkeys(
            ('proposed', 'accepted', 'amended', 'rejected', 'held', 'records_landed',
             'a_undecided', 'a_held', 'measurement_missing',
             'tags_retries', 'tags_malformed_batches', 'tags_unknown_project_ids',
             'tags_name_keys_mapped')
            + DECISIONS
            + tuple(prefix + '_' + key for prefix in ('a', 'r1')
                    for key in (*TOKENS, 'weighted_tokens', 'cost_usd')), 0
        ),
        'weighted_tokens': 0,
        'minutes': 0,
        'published': [],
    }

    def terminate(signum, frame):
        record['terminated'] = True

    def check_running():
        if record['terminated']:
            raise ValueError('steady stopped by TERM')

    def own_command(args, **kwargs):
        check_running()
        return command(args, **kwargs)

    oldterm = signal.signal(signal.SIGTERM, terminate)
    try:
        # Reuse this exact flock in the committing child, rather than deadlocking
        # on a second open of gsd.lock. No Steward heavy lock is acquired here.
        with draft.live_lock(path, blocking=False) as lock_fd:
            errors = []
            try:
                since = record['since'] = cursor(path)
                check_running()
                until, reason, bounded_by, fresh = cutoff(datetime.now(UTC))
                record.update(until=until, bounded_by=bounded_by, fresh_through=fresh)
                check_running()
                if until <= since:
                    record['empty_reason'] = reason
                args = json.loads(os.environ.get('GSD_STEP_ARGV', json.dumps(DEFAULT_ARGS)))
                if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
                    raise ValueError('GSD_STEP_ARGV must be a JSON array of step arguments')
                if any(v in ('--dry', '--restate') for v in args):
                    raise ValueError(
                        'steady requires committing source-window steps, not --dry or --restate'
                    )
                split = timestamp(until) - timestamp(since) > timedelta(days=3)
                while since < until:
                    end = (
                        store.iso(
                            min(timestamp(since) + timedelta(days=2), timestamp(until)).isoformat()
                        )
                        if split
                        else until
                    )
                    run = path.parent / 'steps' / ('steady-' + uuid.uuid4().hex)
                    step_args = [v.replace('{since}', since).replace('{until}', end) for v in args]
                    try:
                        own_command(
                            [
                                'step',
                                *step_args,
                                '--db',
                                str(path),
                                '--since',
                                since,
                                '--until',
                                end,
                                '--run',
                                str(run),
                                '--json',
                            ],
                            env={
                                **os.environ,
                                'GSD_LOCK_FD': str(lock_fd),
                                'PATH': str(LAUNCHER.parent) + os.pathsep + os.environ.get('PATH', ''),
                            },
                            pass_fds=(lock_fd,),
                        )
                    finally:
                        tally(record, run, path)
                    after = cursor(path)
                    result_path = run / 'result.json'
                    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
                    if result.get('state') == 'empty':
                        record['empty_reason'] = 'no selected sources'
                    if after == since:
                        if result.get('state') == 'empty':
                            break
                        raise ValueError('step did not advance committed cursor')
                    if after != end:
                        raise ValueError('step committed an unexpected window cut')
                    since = after
            except (ValueError, OSError, sqlite3.Error) as exc:
                errors.append('loop: ' + str(exc))
            if not record['terminated']:
                sinks = [
                    ('archive', lambda: draft.upload_archives(path)),
                    ('publish qmd', lambda: own_command(['publish', 'qmd', '--db', str(path)])),
                    ('backup', lambda: own_command(['backup', '--db', str(path)])),
                ]
                config = Path.home() / '.config/gsd'
                if (config / 'notion.token').is_file() and (
                    config / 'notion-check-reviewed'
                ).is_file():
                    def publish_notion():
                        result = own_command(['publish', 'notion', '--db', str(path)])
                        report = json.loads(result.stdout)
                        if report.get('refused') or any(page['errors'] for page in report['pages']):
                            raise ValueError(
                                'Notion projection incomplete; inspect publish notion --check privately'
                            )

                    sinks.append(('publish notion', publish_notion))
                for name, sink in sinks:
                    if record['terminated']:
                        break
                    try:
                        sink()
                        record['published'].append(name)
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        errors.append(name + ': ' + str(exc))
            if errors:
                raise ValueError('; '.join(errors))
        check_running()
        return record
    except (ValueError, OSError, sqlite3.Error) as exc:
        from .runs import safe_cause
        record['error'] = safe_cause(exc)
        raise
    finally:
        if record['terminated']:
            record['error'] = 'steady stopped by TERM'
        record['minutes'] = (time.monotonic() - started) / 60
        try:
            emit(record, path)
        finally:
            signal.signal(signal.SIGTERM, oldterm)


if __name__ == '__main__':
    from .cli import main

    raise SystemExit(main(['steady', '--once']))
