"""Read-only outcome measurements from gsd's existing private run artifacts.

R1 denotes the whole sequential reviewer relay (r1, r2, ...). Decisions on A
changes exclude reviewer additions. Missing evidence is null, never a success.
No store migration, Steward schema edit, provider call or archive write.
"""
import json
import re
import sqlite3
from pathlib import Path

from . import db as store, export
from .tokens import weighted_tokens

DECISIONS = ('a_changes', 'r1_accepted', 'r1_amended', 'r1_rejected', 'r1_added')
TOKENS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


def safe_cause(error):
    """Only gsd-authored envelopes and finite diagnostics may leave private captures."""
    text = str(error)
    patterns = (
        r'QMD read failed \(exit \d+\): (?:\d+ ids? not found|qmd2: not found|qmd2: unavailable \([A-Za-z]+\)|diagnostics suppressed)',
        r'(?:gsd (?:step|publish|backup)|rclone archive) failed \(exit \d+\)',
        r'steady (?:terminated|stopped) by TERM',
        r'qmd (?:status failed|status is not valid qmd\.status\.v1 JSON|status violates qmd\.status\.v1|status: alive is false|(?:gmail|whatsapp|fathom) fresh_through is unknown|fresh_through is outside the supported time range)',
        r'step (?:did not advance committed cursor|committed an unexpected window cut)',
        r'steady requires (?:an installed pipeline cut|committing source-window steps, not --dry or --restate)',
        r'GSD_STEP_ARGV must be a JSON array of step arguments',
        r'Notion projection incomplete',
        r'lock is held',
    )
    parts = []
    for part in text.split('; '):
        found = next((m.group() for p in patterns if (m := re.search(p, part))), None)
        stages = [s for s in ('loop', 'archive', 'publish qmd', 'backup', 'publish notion')
                  if s + ':' in part]
        if found:
            found = found.replace('terminated by TERM', 'stopped by TERM')
        if found or stages:
            parts.append((', '.join(stages) + ': ' if stages else '') + (found or 'failed'))
    return '; '.join(parts) or 'failed'


def outcome(record):
    """One execution verdict and owner-safe business result for every surface."""
    if record.get('terminated'):
        return {'status': 'failed', 'result': 'stopped', 'reason': 'steady stopped by TERM', 'icon': '❌'}
    if record.get('error'):
        reason = safe_cause(record['error'])
        return {'status': 'failed', 'result': 'failed', 'reason': reason, 'icon': '❌'}
    landed = (record.get('counts') or {}).get('records_landed', 0)
    quiet = bool(record.get('empty_reason')) and not landed
    return {'status': 'succeeded', 'result': 'quiet' if quiet else f'{landed} records landed',
            'reason': 'quiet' if quiet else f'{landed} records landed', 'icon': '💤' if quiet else '✅'}


def load(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def is_a(change):
    return change['change_id'].lower().startswith('a') or any(
        (d.get('after') or {}).get('actor', '').lower() == 'a'
        or (d.get('after') or {}).get('actor', '').lower().startswith('a-')
        for d in change.get('deltas', [])
    )


def stage(run, names):
    rows, costs, missing = [], [], []
    for name in names:
        result = load(run / 'captures' / f'{name}.result.json', {})
        calls = load(run / 'captures' / f'{name}.usage.json')
        usage = result.get('usage')
        if usage is not None:
            rows.append(usage)
        elif calls is not None:
            rows.extend(c['usage'] for c in calls)
        else:
            stream = run / 'captures' / f'{name}.stream.jsonl'
            if stream.is_file():
                from .step import usage as stream_usage
                from types import SimpleNamespace
                lines = []
                for line in stream.read_text().splitlines():
                    try:
                        json.loads(line)
                    except ValueError:
                        missing.append(name + ': truncated usage stream (partial totals)')
                        break
                    lines.append(line)
                calls, _ = stream_usage(SimpleNamespace(read_text=lambda: '\n'.join(lines)))
                rows.extend(c['usage'] for c in calls)
            else:
                missing.append(name + ': usage missing')
        if 'total_cost_usd' in result:
            costs.append(result['total_cost_usd'])
        else:
            missing.append(name + ': cost missing')
    values = {k: sum(r.get(k, 0) for r in rows) for k in TOKENS}
    if any('usage missing' in m for m in missing):
        values = dict.fromkeys(TOKENS)
    values['weighted_tokens'] = sum(weighted_tokens(r) for r in rows) if all(v is not None for v in values.values()) else None
    values['cost_usd'] = sum(costs) if len(costs) == len(names) else None
    return values, missing


def measure(run):
    run = Path(run)
    manifest = load(run / 'manifest.json', {})
    result = {'missing': []}
    changes_path = run / 'changes.jsonl'
    changes = None
    if changes_path.is_file():
        changes = [json.loads(line) for line in changes_path.read_text().splitlines() if line.strip()]
    elif (run / 'step.sqlite').is_file():
        try:
            with store.connect(run / 'step.sqlite') as db:
                if store.has_table(db, 'draft_change'):
                    changes = export.changes(db)
        except sqlite3.Error:
            result['missing'].append('draft unreadable')
    if manifest.get('state') == 'empty':
        changes = []
    result.update(dict.fromkeys(DECISIONS))
    if changes is None:
        result['missing'].append('changes missing')
    else:
        a = [c for c in changes if is_a(c)]
        result.update(a_changes=len(a),
                      r1_accepted=sum(c.get('decision') == 'accept' for c in a),
                      r1_amended=sum(c.get('decision') == 'amend' for c in a),
                      r1_rejected=sum(c.get('decision') == 'reject' for c in a),
                      r1_added=sum(not is_a(c) for c in changes),
                      a_undecided=sum(c.get('decision') is None for c in a),
                      a_held=sum(c.get('decision') == 'hold' for c in a))
    names = {p.stem.removeprefix('agent-') for p in run.glob('agent-*.json')}
    names.update(p.name.split('.')[0] for p in (run / 'captures').glob('*.result.json'))
    for prefix, agents in (
        ('a', sorted(n for n in names if n == 'a' or n.startswith('a-'))),
        ('r1', sorted(n for n in names if re.fullmatch(r'r\d+', n) or n == 'review')),
    ):
        values, missing = stage(run, agents)
        result.update({prefix + '_' + k: v for k, v in values.items()})
        result['missing'].extend(missing)
    return result


def combine(measurements):
    if not measurements:
        return {}
    keys = set().union(*(m.keys() for m in measurements)) - {'missing'}
    return {k: sum(m.get(k, 0) for m in measurements) if all(m.get(k) is not None for m in measurements) else None for k in keys}


def command(a):
    from . import owner

    a.since = a.since or owner.get('runs_since')
    root = Path(a.db).expanduser().resolve().parent
    rows, seen = [], set()
    for path in sorted((root / 'steady-runs').glob('*.json')):
        record = load(path)
        if (record.get('until') or record.get('since') or '') < a.since:
            continue
        steps = sorted({l['step'] for l in record.get('links', []) if l.get('step')})
        measurements = [measure(root / 'steps' / s) for s in steps]
        seen.update(steps)
        counts = {**record.get('counts', {}), **combine(measurements)}
        rows.append({'run': path.stem, 'since': record.get('since'), 'until': record.get('until'),
                     'counts': counts,
                     'steps': steps, 'error': outcome(record)['reason'] if outcome(record)['status'] == 'failed' else None,
                     'missing': [s + ': ' + m for s, v in zip(steps, measurements) for m in v['missing']]})
    # Older failed/empty steps may predate persisted steady records or lack links.
    for run in sorted((root / 'steps').glob('steady-*')):
        if run.name in seen:
            continue
        manifest = load(run / 'manifest.json', {})
        if (manifest.get('until') or '') < a.since:
            continue
        measured = measure(run)
        rows.append({'run': run.name, 'since': manifest.get('since'), 'until': manifest.get('until'),
                     'counts': {k: v for k, v in measured.items() if k != 'missing'},
                     'steps': [run.name], 'error': 'no linked steady record', 'missing': measured['missing']})
    return {'schema': 'gsd.runs.v1', 'since': a.since, 'runs': sorted(rows, key=lambda r: r.get('until') or '')}
