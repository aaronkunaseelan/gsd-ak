"""The root gsd Notion page: last run, now, rolling 7 days, recent runs and comments (gsd D23).

Rendered from what already exists: steady's own run records, Steward's intent (mode and
next run), gsd's intent document (the same numbers mech-d shows), the publish log and the
inbox. Written section by section only when the text changed, so the database links and
anything else on the page are never touched.
"""

import json
import statistics
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db as store
from . import intent, notion
from .runs import outcome
from .contract_validate import problems

INTENT_CONTRACT = json.loads((store.ROOT / 'contracts/vendor/intent-1.json').read_text())

SPARK = '▁▂▃▄▅▆▇█'


def at(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    return datetime.fromisoformat(store.iso(value))


hm = store.owner_time


def spark(values):
    if not values:
        return '—'
    low, high = min(values), max(values)
    return ''.join(SPARK[0 if high == low else round((v - low) / (high - low) * 7)] for v in values)


def tokens(value):
    return f'{value / 1e6:.1f} M' if value >= 1e5 else f'{value / 1e3:.0f} K'


def records(directory, since):
    """steady's own run records, oldest first, each with the time it was written."""
    out = []
    for path in sorted(Path(directory).glob('*.json')):
        ended = datetime.fromtimestamp(int(path.name.split('-')[0]) / 1e9, UTC)
        if ended < since:
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        out.append(dict(record, ended=ended))
    return out


def steward_view():
    try:
        result = subprocess.run(['steward', 'intent', '--json'], capture_output=True, text=True, timeout=30, check=True)
        doc = json.loads(result.stdout)
        if problems(INTENT_CONTRACT, doc) or doc['system'] != 'steward':
            return None
        blocks = {b['kind']: b for b in doc['blocks']}
        jobs = []
        for kind in ('runs', 'schedule'):
            block = blocks[kind]
            for item in block['items']:
                if item['job'] != 'gsd-steady':
                    continue
                if kind == 'runs':
                    if not item['id'].startswith('run-'):
                        return None
                    at(item['started_at'])
                    at(item['ended_at'])
                else:
                    at(item['at'])
                jobs.append(item)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, subprocess.SubprocessError):
        return None
    return {
        'runs': [i for i in jobs if 'status' in i],
        'next': next((i for i in jobs if 'at' in i and 'status' not in i), None),
    }


def mode(reason):
    return 'automatic' if (reason or '').startswith('default') else 'manual'


def match(run, history):
    """Join only on the producer's run identity; old records remain unmatched."""
    for record in history:
        identity = record.get('steward_run_id')
        if identity and run.get('id') == 'run-' + identity:
            return record
    return None


def comments_summary(db, cut, reading):
    if db is None:
        return None
    counts = dict(db.execute(
        "SELECT CASE WHEN cleared LIKE 'cleared%' THEN 'cleared' WHEN cleared IS NOT NULL THEN 'kept' "
        "WHEN seen_at > ? THEN 'waiting' ELSE 'read' END, count(*) FROM comments GROUP BY 1", (cut or '',)
    ).fetchall())
    through = db.execute("SELECT value FROM meta WHERE key='comments_through'").fetchone()
    return {'counts': counts, 'reading': reading,
            'through': through[0] if through else None}


def render(doc, history, steward, publishes, comments, now):
    sections = {}
    runs = (steward or {}).get('runs') or []
    latest = next((r for r in runs if r['status'] not in ('skipped', 'cancelled')), None)
    record = match(latest, history) if latest else (history[-1] if history else None)
    if latest and latest['status'] == 'started':
        head = f"<callout icon=\"⏳\" color=\"blue_bg\">\n\t**Running since {hm(at(latest['started_at']))}** · {mode(latest.get('reason'))}\n</callout>"
    elif record:
        verdict = outcome(record)
        icon, what = verdict['icon'], verdict['reason']
        color = {'✅': 'green_bg', '❌': 'red_bg'}.get(icon, 'gray_bg')
        how = f" · {mode(latest.get('reason'))}" if latest else ''
        head = (f"<callout icon=\"{icon}\" color=\"{color}\">\n\t**{hm(record['ended'] - timedelta(minutes=record.get('minutes') or 0))} · {what} · "
                f"{record.get('minutes') or 0:.0f} min**{how}\n</callout>")
    elif latest:
        icon, color = ('✅', 'green_bg') if latest['status'] == 'succeeded' else ('❌', 'red_bg')
        head = (f'<callout icon="{icon}" color="{color}">\n\t**{hm(at(latest["started_at"]))} · '
                f'{latest["status"]}** · {mode(latest.get("reason"))} · gsd detail unknown\n</callout>')
    else:
        head = '<callout icon="➰" color="gray_bg">\n\tNo run recorded yet.\n</callout>'
    nxt = (steward or {}).get('next')
    later = (f"Next automatic run: **{hm(at(nxt['at']))}**" + (f" (about {nxt['est_min']:.0f} min)" if nxt.get('est_min') else '')
             if nxt else 'Next automatic run: unknown (Steward read failed or invalid)' if steward is None
             else 'Next automatic run: not planned yet')
    sections['Last run'] = (head + '\n' + later + '. Run now from the the tools menu, *Run gsd now…*. '
                            "Manual runs don't wake Steward; automatic runs then wait 6 h.")

    blocks = {b['kind']: b for b in doc['blocks']}
    stock = blocks['stock']
    rows = [r for r in blocks['rows']['items'] if r['you']][:5]
    lines = [(f"**{stock['open']}** open · **{stock['you']}** waiting on you · **{stock['past_due']}** past due · "
              f"**{stock['due_7d']}** due in 7 days")]
    if rows:
        lines.append('Waiting on you: ' + ' · '.join(
            (f"[{r['name'].replace('[', '(').replace(']', ')')}]({r['link']})" if r['link'] else r['name']) + f" {r['you']}"
            for r in rows))
    hops = [h for h in blocks['chain']['hops'] if h['name'] != 'gsd cut']
    lines.append('Sources: ' + ' · '.join(f"{h['name'].split(' · ')[-1]} {h['note']}" for h in hops)
                 + (f" · gsd cut {hm(at(stock['fresh_through']))}" if stock['fresh_through'] else ''))
    sections['Now'] = '\n'.join(lines)

    week = [r for r in history if now - timedelta(days=7) <= r['ended'] <= now]
    landed = [r['counts']['records_landed'] for r in week]
    minutes = [r.get('minutes') or 0 for r in week]
    weighted = [r.get('weighted_tokens') or 0 for r in week]
    requests = [p['requests']['total'] for p in publishes if p.get('requests')
                and now - timedelta(days=7) <= at(p['timestamp']) <= now]
    failed = sum(outcome(r)['status'] == 'failed' for r in week)
    sections['Last 7 days'] = '\n'.join([
        f"`{spark(landed[-14:])}` records landed · {sum(landed)}",
        f"`{spark(minutes[-14:])}` minutes per run · median {statistics.median(minutes) if minutes else 0:.0f}",
        f"`{spark(weighted[-14:])}` weighted tokens per run · median {tokens(statistics.median(weighted) if weighted else 0)}",
        f"`{spark(requests[-14:])}` Notion requests per publish · median {statistics.median(requests) if requests else 0:.0f}",
        f"**{len(week)}** runs: {len(week) - failed} ✓ · {failed} ✗",
    ])

    recent = []
    for run in runs[:8]:
        if run['status'] in ('skipped', 'cancelled'):
            # A skip replaces the run's reason, so its mode is unknown.
            recent.append(f"- ⏭ {hm(at(run['started_at']))} · {run['status']}: {run.get('reason') or ''}")
            continue
        if run['status'] == 'started':
            recent.append(f"- ⏳ {hm(at(run['started_at']))} · {mode(run.get('reason'))} · running")
            continue
        found = match(run, history)
        if not found:
            recent.append(f"- {'✅' if run['status'] == 'succeeded' else '❌'} {hm(at(run['started_at']))} · {mode(run.get('reason'))} · {run['status']} · gsd detail unknown")
            continue
        verdict = outcome(found)
        icon, what = verdict['icon'], verdict['reason']
        recent.append(f"- {icon} {hm(at(run['started_at']))} · {mode(run.get('reason'))} · {what} · "
                      f"{found.get('minutes') or 0:.0f} min · {tokens(found.get('weighted_tokens') or 0)} tokens")
    if not runs:
        for found in reversed(history[-8:]):
            verdict = outcome(found)
            icon, what = verdict['icon'], verdict['reason']
            recent.append(f"- {icon} {hm(found['ended'])} · {what} · {found.get('minutes') or 0:.0f} min")
    sections['Recent runs'] = '\n'.join(recent) or 'None yet.'

    if comments is not None:
        counts = comments['counts']
        if comments['reading'] == 'no_capability':
            text = 'Comment reading is off: turn on *Read comments* for the gsd publisher integration in Notion.'
        else:
            text = (f"**{counts.get('waiting', 0)}** waiting for the next run · **{counts.get('read', 0)}** read by runs · "
                    f"**{counts.get('cleared', 0)}** cleared · **{counts.get('kept', 0)}** kept"
                    + (f" · read through {hm(at(comments['through']))}" if comments['through'] else ''))
        sections['Your comments'] = text + '\nComment anywhere: gsd reads it within 15 minutes and the next run acts on it. Then a comment on a line is cleared; a comment under a page\'s title stays.'
    return sections


def publish(path, client, root, inbox_db, now, reading=None):
    """Write the sections that changed; return 'published' or 'unchanged'."""
    path = Path(path)
    doc = intent.document(path, now)
    history = records(path.parent / 'steady-runs', now - timedelta(days=8))
    publishes = []
    log = path.parent / 'notion-publish.jsonl'
    if log.is_file():
        for line in log.read_text().splitlines():
            entry = json.loads(line)
            if at(entry['timestamp']) >= now - timedelta(days=7):
                publishes.append(entry)
    with store.connect(path) as gdb:
        cut = store.pipeline_cut(gdb)['until']
    sections = render(doc, history, steward_view(), publishes, comments_summary(inbox_db, cut, reading), now)
    text = json.dumps(sections, sort_keys=True)
    last = inbox_db.execute("SELECT value FROM meta WHERE key='status_sections'").fetchone()
    sent = json.loads(last[0]) if last else {}
    # Notion normalizes markdown, so compare with what was sent, not with the page.
    changed = {k: v for k, v in sections.items() if sent.get(k) != v}
    if not changed:
        return 'unchanged'
    errors = []
    notion.update_body(client, root, changed, errors=errors)
    if errors:
        raise ValueError('status page incomplete: ' + ', '.join(errors))
    with inbox_db:
        inbox_db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('status_sections', text))
    return 'published'
