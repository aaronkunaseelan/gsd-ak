"""One private source window, draft, proposer and sequential review relay.
Failed launches stop without retry; undecided changes never acquire runner decisions.
"""

import json
import os
import re
import signal
from pathlib import Path
import subprocess
import uuid

from . import db as store, read, draft, export, packets, tags, feed
from .capture import capture
from . import reflections

CLAUDE = 'claude'


def save(path, value):
    path.write_text(
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    )
    path.chmod(0o600)


class Qmd:
    def __init__(self, run, until):
        self.run, self.env, self.n = run, dict(os.environ, QMD_UNTIL=until), 0

    def __call__(self, *args, missing_ok=False):
        self.n += 1
        env = self.env
        if args and args[0] == 'feed':
            env = {k: v for k, v in self.env.items() if k != 'QMD_UNTIL'}
        p = subprocess.run(['qmd', *args], text=True, capture_output=True, env=env)
        # Diagnostics are not forwarded: provider errors can contain secrets.
        progress = [
            s
            for s in p.stderr.splitlines()
            if s
            in {'fetch', 'archive', 'type', 'extract', 'spool', 'done', 'unavailable', 'failed'}
        ]
        save(
            self.run / 'captures' / f'qmd-{self.n:04d}.json',
            {'args': list(args), 'exit': p.returncode, 'stdout': p.stdout, 'progress': progress},
        )
        if p.returncode and not (missing_ok and p.returncode in (3, 4)):
            # The final diagnostic is safe only if its whole grammar is known.
            lines = p.stderr.splitlines()
            last = lines[-1] if lines else ''
            safe = re.fullmatch(r'\d+ ids? not found|qmd2: not found|qmd2: unavailable \([A-Za-z]+\)', last)
            cause = last if safe else 'diagnostics suppressed'
            raise ValueError(f'QMD read failed (exit {p.returncode}): {cause}')
        result = json.loads(p.stdout) if p.stdout.strip() else None
        if '--materialise' in args and isinstance(result, dict):
            result['_extracted'] = 'extract' in progress
        return result


def launch(
    run,
    name,
    model,
    until,
    *,
    read_only=False,
    timeout=3600,
    effort_a='medium',
    effort_review='medium',
):
    """One headless session, no bootstrap, resume or automatic relaunch.

    read_only is a cache/plumbing proof on real packets, not a step acceptance.
    It changes only the first user message; system and tools stay identical.
    """
    receipt_path = run / f'agent-{name}.json'
    if receipt_path.exists():
        raise ValueError(f'{name}: launch already attempted; inspect before retrying')
    session = str(uuid.uuid4())
    effort = effort_a if name == 'a' or name.startswith('a-') else effort_review
    if effort not in ('low', 'medium', 'high'):
        raise ValueError('invalid effort')
    receipt = {
        'session_id': session,
        'model': model,
        'state': 'starting',
        'read_only': read_only,
        'effort': effort,
        reflections.FIELD: '',
    }
    save(receipt_path, receipt)
    args = [CLAUDE, '-p', '--model', model, '--effort', effort]
    args += '--permission-mode bypassPermissions --output-format stream-json --verbose'.split()
    args += ['--append-system-prompt-file', str(run / 'shared.md'), '--session-id', session]
    args += ['--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
    args += ['--disable-slash-commands', '--tools', 'Bash,Read,Write,Edit']
    if not read_only:
        args += ['--json-schema', json.dumps(reflections.OUTPUT_SCHEMA)]
    env = dict(
        os.environ,
        QMD_UNTIL=until,
        GSD_DB=str(run / 'step.sqlite'),
        GSD_ACTOR=name,
        # Auto-memory is a channel outside the packet: R1 loaded what A wrote (FR review).
        CLAUDE_CODE_DISABLE_AUTO_MEMORY='1',
        PATH=str(store.ROOT / 'bin') + os.pathsep + os.environ.get('PATH', ''),
    )
    stream = run / 'captures' / f'{name}.stream.jsonl'
    user = run / f'prompt-{name}.md'
    if read_only:
        user = run / f'proof-{name}.md'
        save(
            user,
            'CACHE PROOF ONLY. Read the shared context and the packet below, then stop. '
            'Do not use tools, write files, propose or decide changes, or run acceptance. '
            'Reply only READ. The following task is for a later production run.\n\n'
            + (run / f'prompt-{name}.md').read_text(),
        )
    try:
        code = headless(run, name, args, user, env, receipt, timeout)
        calls, result = usage(stream)
        save(run / 'captures' / f'{name}.usage.json', calls)
        save(run / 'captures' / f'{name}.result.json', result)
        receipt[reflections.FIELD] = reflections.text(result or {})
        if code or not result or result.get('is_error') or result.get('subtype') != 'success':
            raise ValueError('session did not finish successfully')
        if read_only:
            if not calls or any(c['tool_use'] for c in calls):
                raise ValueError('read-only proof used tools or produced no usage')
        else:
            acceptance(run, name)
        receipt['state'] = 'read-only-proof' if read_only else 'proven'
    except (KeyboardInterrupt, SystemExit):
        receipt['state'] = 'interrupted'
        raise
    except Exception:
        receipt['state'] = 'failed'
        raise ValueError(
            f'{name}: stopped; inspect private captures, do not retry blindly'
        ) from None
    finally:
        save(receipt_path, receipt)
    return calls


def headless(run, name, args, user, env, receipt, timeout):
    with (
        user.open() as stdin,
        private_file(run / 'captures' / f'{name}.stream.jsonl') as stdout,
        private_file(run / 'captures' / f'{name}.stderr.txt') as stderr,
    ):
        with subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=run,
            env=env,
            start_new_session=True,
        ) as process:
            try:
                receipt.update(pid=process.pid, state='running')
                save(run / f'agent-{name}.json', receipt)
                return process.wait(timeout=timeout)
            finally:
                # Kill the whole owned group, including tool children, on every exit.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def acceptance(run, name):
    with store.connect(run / 'step.sqlite') as db:
        if name == 'a':
            ok = db.execute('SELECT COUNT(*) FROM draft_change').fetchone()[0] >= 1
            notes = run / 'open.md'
        elif name.startswith('a-'):
            ok = db.execute(
                'SELECT COUNT(*) FROM draft_change WHERE change_id LIKE ?', (name + '-%',)
            ).fetchone()[0] >= 1
            notes = run / ('open-' + name + '.md')
        else:
            ok = db.execute(
                'SELECT COUNT(*) FROM draft_change WHERE grp=? AND decision IS NULL', (name,)
            ).fetchone()[0] == 0
            notes = run / ('notes-' + name + '.md')
    ok = ok and notes.is_file()
    save(
        run / 'captures' / f'{name}.check.json',
        {
            'exit': 0 if ok else 1,
            'stdout': '',
            'stderr': '',
        },
    )
    if not ok:
        raise ValueError('independent acceptance failed')


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w')


def usage(stream):
    """Assistant blocks can repeat an API message id; retain one usage row per call."""
    calls, result = {}, None
    for line in stream.read_text().splitlines():
        event = json.loads(line)
        if event.get('type') == 'assistant':
            message = event['message']
            key = message['id']
            previous = calls.get(key, {})
            calls[key] = {
                'message_id': key,
                'model': message.get('model'),
                'usage': {**previous.get('usage', {}), **message.get('usage', {})},
                'tool_use': previous.get('tool_use', False)
                or any(b.get('type') == 'tool_use' for b in message.get('content', [])),
                '_blocks': {
                    **previous.get('_blocks', {}),
                    **{json.dumps(b, sort_keys=True): b for b in message.get('content', [])},
                },
            }
        elif event.get('type') == 'result':
            result = event
    calls = list(calls.values())
    for call in calls:
        blocks = list(call.pop('_blocks').values())
        thinking = [b for b in blocks if b.get('type') in ('thinking', 'redacted_thinking')]
        call['thinking_blocks'] = len(thinking)
        call['thinking_chars'] = sum(len(b.get('thinking', '')) for b in thinking)
        call['tool_calls'] = [
            {'id': b.get('id'), 'name': b.get('name')}
            for b in blocks
            if b.get('type') == 'tool_use'
        ]
    iterations = (result or {}).get('usage', {}).get('iterations', [])
    messages = [i for i in iterations if i.get('type') == 'message']
    if len(messages) == len(calls):
        for call, iteration in zip(calls, messages):
            call['usage'].update({k: v for k, v in iteration.items() if k != 'type'})
    return calls, result


def previous_folder(db):
    if not store.has_table(db, 'change_sets'):
        return None
    row = db.execute(
        "SELECT note FROM change_sets WHERE origin='pipeline' AND window_until IS NOT NULL AND note GLOB 'gsd-step-run:*' ORDER BY window_until DESC,at DESC,change_set_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    # Only runner commits have carry-over artifacts. Skip intervening manual commits.
    folder = Path(row['note'][len('gsd-step-run:') :])
    # A re-statement's results are the graph itself; its working notes are not a carry-over.
    manifest = folder / 'manifest.json'
    if manifest.is_file() and json.loads(manifest.read_text()).get('mode') == 'restate':
        return None
    return folder


def bundle(db, group, *, selected=None, people=False):
    all_changes = export.changes(db)
    changes = [c for c in all_changes if c['grp'] == group] if selected is None else selected
    if people:
        person_ids = {r['id'] for r in store.rows(db, 'people')} | {
            (d.get('after') or d['before'])['id']
            for c in all_changes for d in c['deltas']
            if d['table'] == 'people'
        }
        context = [
            c
            for c in all_changes
            if c['grp'] != group and any(r['id'] in person_ids for r in c['records'])
        ]
    else:
        context = []
    ids = sorted({r['id'] for c in changes + context for r in c['records']})
    shorts = read.short_ids(read.graph(db))
    aliases = {r['qmd_id']: r['short'] for r in db.execute('SELECT * FROM draft_ref')}
    status = draft.status(db)
    decisions = (
        'counts: '
        + json.dumps(status['counts'])
        + '\n'
        + '\n'.join(
            f"{c['change_id']} {c['decision'] or 'undecided'} by {c['decided_by'] or '-'}"
            for c in status['changes']
        )
    )

    return '\n\n'.join(
        [
            '## Decisions so far (full reasons: gsd status --json)\n' + decisions,
            ('## Your changes' if selected is None else '## Meeting-related changes (context; earlier decisions need sourced corrections)')
            + ' (field deltas, previous values and citations)\n'
            + '\n\n'.join(packets.review_change(c, shorts, aliases) for c in changes),
            '## Already reviewed person changes (amend only for a sourced correction)\n'
            + '\n\n'.join(packets.review_change(c, shorts, aliases) for c in context),
            '## Touched records now (sparse; whole records and old citations: gsd show ID…)\n'
            + read.view(db, sparse=True, ids=ids),
            '## Evidence to open first\n'
            'Open every change’s cited sources, several ids per call: '
            'qmd get ID… --no-quoted --attachments head:6. '
            'Resolve short refs through refs.tsv; file: written minutes are in selected.json. '
            'Cuts above are not evidence; gsd show ID… gives full changed values and ids; '
            'gsd status --json gives uncut why. Citation excerpts open through qmd.',
        ]
    )


def meetings_bundle(db, run):
    changes, state = export.changes(db), read.graph(db)
    docs = json.loads((run / 'selected.json').read_text())
    refs = set(tags.meeting_refs(docs, changes, state))
    if (run / 'meetings.json').exists():
        refs.update(json.loads((run / 'meetings.json').read_text()))
    if not refs:
        return None
    meeting_ids = {m['id'] for m in state['meetings']}
    relevant = [
        c
        for c in changes
        if any(r['id'] in meeting_ids for r in c.get('records', []))
        or any(d['table'] == 'meetings' for d in c.get('deltas', []))
        or any(
            (r.get('evidence_qmd_event_id') or r.get('source_key')) in refs for r in c['citations']
        )
    ]
    return (
        '## Meetings to check (refs or record: ids)\n'
        + '\n'.join(sorted(refs))
        + '\n\n'
        + bundle(db, 'meetings', selected=relevant)
    )


def turn_totals(calls):
    totals = {
        'turns': len(calls),
        'thinking_blocks': sum(c['thinking_blocks'] for c in calls),
        'thinking_chars': sum(c['thinking_chars'] for c in calls),
        'tool_calls': sum(len(c['tool_calls']) for c in calls),
    }
    totals['usage'] = {
        k: sum(c['usage'].get(k, 0) for c in calls)
        for k in (
            'input_tokens',
            'cache_creation_input_tokens',
            'cache_read_input_tokens',
            'output_tokens',
        )
    }
    return totals


def save_packet(run, name, text, sizes):
    boundary = sizes['shared']['chars']
    shared, user = text[:boundary], text[boundary + 2 :]
    path = run / 'shared.md'
    if path.exists() and path.read_text() != shared:
        raise ValueError('shared packet prefix changed during relay')
    if not path.exists():
        save(path, shared)
    save(run / f'prompt-{name}.md', user)
    save(run / f'packet-{name}.md', text)


def interrupted(signum, frame):
    # Steward's TERM must unwind headless() and kill its separate Claude group.
    raise SystemExit(128 + signum)


def run(a):
    from .selection import select
    from .groups import assign

    since, until = store.iso(a.since), store.iso(a.until)
    if since >= until:
        raise ValueError('--since must be earlier than --until')
    if not a.dry and not a.explicit_db:
        raise ValueError('non-dry step requires explicit --db FILE (live default not enabled yet)')
    if a.a != a.review:
        raise ValueError('--a and --review must use the same model for shared-prefix caching')
    step_id = until[:19].replace(':', '').replace('-', '') + '-' + uuid.uuid4().hex[:8]
    run = Path(a.run).expanduser().resolve() if a.run else store.LIVE.parent / 'steps' / step_id
    run.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run.mkdir(mode=0o700)  # Existing runs are evidence, never silently overwritten.
    oldmask = os.umask(0o077)
    oldterm = signal.signal(signal.SIGTERM, interrupted) if 'GSD_LOCK_FD' in os.environ else None
    try:
        return execute(a, run, step_id, since, until, select, assign)
    except Exception:
        save(
            run / 'failed.json',
            {
                'state': 'failed',
                'message': 'Step stopped. Inspect private artifacts; no automatic decisions or relaunch.',
            },
        )
        raise
    finally:
        from .runs import measure
        result_path = run / 'result.json'
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        result['outcomes'] = measure(run)
        save(result_path, result)
        if oldterm is not None:
            signal.signal(signal.SIGTERM, oldterm)
        os.umask(oldmask)


def restate_agents(db):
    """Small per-project seed index; sources are discovered by A, not preselected."""
    state = read.graph(db)
    agents = []
    for project in state['projects']:
        if project.get('status') != 'active':
            continue
        slug = (
            re.sub(r'[^a-z0-9]+', '-', (project.get('slug') or project['name']).lower()).strip('-')
            or 'project'
        )
        name = f'a-{slug}-{project["id"][:8]}'
        ids = {project['id']} | {
            i['id'] for i in state['items'] if i.get('project_id') == project['id']
        }
        # Non-duplicate links still describe participation; owner/project columns
        # are authoritative for ownership and membership.
        owned = set(ids)
        participants = {r['id'] for t in ('people', 'meetings') for r in state[t]}
        item_ids = {r['id'] for r in state['items']}
        for link in state['record_links']:
            if link['from_record_id'] in item_ids and link['relation_kind'] in (
                'belongs_to_project',
                'assigned_to_person',
            ):
                continue
            if link['from_record_id'] in owned and link['to_record_id'] in participants:
                ids.add(link['to_record_id'])
            if link['to_record_id'] in owned and link['from_record_id'] in participants:
                ids.add(link['from_record_id'])
        for row in [project] + [i for i in state['items'] if i['id'] in ids]:
            for field in ('owner_person_id', 'assigned_to'):
                value = row.get(field)
                if value:
                    try:
                        ids.add(store.resolve(db, value, 'person'))
                    except ValueError:
                        pass
        refs = sorted(
            {
                r['evidence_qmd_event_id']
                for r in state['source_refs']
                if r.get('record_id') in ids and r.get('evidence_qmd_event_id')
            }
        )
        task = (
            f'Agent: {name}\nProject: {project["name"]} [{project["id"]}]\n'
            + 'Record ids: '
            + ', '.join(sorted(ids))
            + '\n'
            + 'Primary source seeds (verify dates): '
            + ', '.join(refs)
            + '\n'
            + 'Use graph aliases of these people to discover sources. Search only within the supplied window; no selected window exists.\n'
        )
        agents.append({'name': name, 'task': task})
    return agents


def prepare(a, run, step_id, since, until, select):
    (run / 'captures').mkdir(mode=0o700)
    (run / 'views').mkdir(mode=0o700)
    qmd = Qmd(run, until)
    restate = getattr(a, 'restate', False)
    tagged = not restate and getattr(a, 'tags', True)
    if not restate:
        with store.connect(a.db) as base:
            qmd.feed_positions = feed.positions(base)
            qmd.feed_bootstraps = feed.bootstraps(base)
            qmd.input_history = lambda children: feed.committed_inputs(a.db, children)
            qmd.feed_since = store.pipeline_cut(base)['until']
            if qmd.feed_since is None:
                raise ValueError('feed bootstrap requires a last committed pipeline cut')
    selected = (
        {'views': {'people.md': ''}, 'refs': {}, 'docs': [], 'manifest': {}}
        if restate
        else select(qmd, since, until, a.extra or ())
    )
    if not restate:
        from .inbox import window as comments

        comments(selected, a.db, since, until)
    if not restate and not a.dry and not selected['docs'] and not selected.get('comments'):
        manifest = {
            **selected['manifest'],
            'feed': selected.get('feed'),
            'state': 'empty',
            'run': str(run),
            'since': since,
            'until': until,
        }
        save(run / 'manifest.json', manifest)
        save(run / 'selected.json', [])
        return None, None, None, manifest
    if not restate:
        capture(qmd, selected)
    draft_path = run / 'step.sqlite'
    draft.snapshot(a.db, draft_path, step_id, since, until)
    with store.connect(a.db) as base:
        graph = read.view(base, sparse=True)
        people = '\n'.join(
            f"{p['id']} · {p['display_name']} · {p.get('primary_email') or '-'} · aliases={p.get('aliases') or '[]'} · {p.get('role_title') or '-'}"
            for p in store.rows(base, 'people')
        )
        selected['views']['people.md'] = (
            '# Graph people\n' + people + '\n\n' + selected['views']['people.md']
        )
        agents = restate_agents(base) if restate else []
        previous = None if restate else previous_folder(base)
        if previous:
            if not (previous / 'changes.jsonl').is_file() or not (previous / 'open.md').is_file():
                raise ValueError(
                    'previous pipeline step folder missing carry-over exports: ' + str(previous)
                )
            selected['views']['prev-step.md'] = export.carry(previous)
        formats = read.fields_text(read.fields(base))
        tag_data = tags.tag(run, selected, read.graph(base), qmd, since) if tagged else None
    if tagged:
        grouped, group_sizes = tags.grouped(tag_data)
        # Replace only source views. Directory, carry-over and formats are unchanged.
        selected['views'] = {
            'projects.md': grouped,
            **{
                k: v
                for k, v in selected['views'].items()
                if k not in ('mail.md', 'whatsapp.md', 'meetings.md')
            },
        }
        selected['manifest']['tags'] = {
            k: tag_data[k]
            for k in (
                'model',
                'cards_chars',
                'calls',
                'tokens',
                'weighted_tokens',
                'cost_usd',
                'sticky',
                'unit_counts',
            )
        }
        selected['manifest']['tags']['group_sizes'] = group_sizes
        save(run / 'meetings.json', [d['id'] for d in selected.get('meetings', [])])
    selected['views']['field-formats.md'] = formats
    save(run / 'views' / 'graph.md', graph)
    for name, text in selected['views'].items():
        save(run / 'views' / name, text)
    save(run / 'refs.tsv', ''.join(f'{short}\t{ref}\n' for short, ref in selected['refs'].items()))
    # Preserve complete selected docs locally, including extra written minutes.
    save(run / 'selected.json', selected['docs'])
    with store.connect(draft_path, True) as db:
        db.executemany('INSERT INTO draft_ref VALUES (?,?)', selected['refs'].items())
        store.put_meta(db, 'run_folder', str(run))
        feed.attach(db, selected.get('feed'))
        db.commit()
    window = '\n\n'.join(f'## {name}\n{text}' for name, text in selected['views'].items())
    common_slice = f'Window: {since} → {until}\nWorking directory: {run}\nDraft: step.sqlite\nShort refs: refs.tsv\n'
    if restate:
        if not agents:
            raise ValueError('re-statement requires at least one active project')
        agent_sizes = {}
        for agent in agents:
            text, sizes = packets.packet(
                run,
                graph,
                '',
                common_slice + '\n' + agent['task'],
                restate=True,
                proposer=agent['name'],
            )
            save_packet(run, agent['name'], text, sizes)
            agent_sizes[agent['name']] = sizes
    else:
        text, sizes = packets.packet(run, graph, window, common_slice, tagged=tagged)
        save_packet(run, 'a', text, sizes)
        agent_sizes = {'a': sizes}
    from .groups import assign

    with store.connect(draft_path) as db:
        preview_plan = assign(
            read.graph(db), [], tag_data, meetings=meetings_bundle(db, run) is not None
        )
        names = {p['id']: p['name'] for p in store.rows(db, 'projects')}
    save(run / 'groups.json', preview_plan)
    preview_scope = (
        packets.review_scope(preview_plan, 'r1', names) if preview_plan['groups']
        else 'No scopes yet. After A: whole project groups (largest first), then meetings, then people; target ceil(A changes / 60) reviewers, never split a scope.'
    )
    preview, preview_sizes = packets.packet(
        run, graph, window,
        common_slice + '\nAssignments, decisions, deltas and touched records are filled after A.',
        'r1', restate=restate, tagged=tagged, scope=preview_scope,
    )
    save_packet(run, 'review', preview, preview_sizes)
    manifest = selected['manifest']
    manifest.update(
        step_id=step_id,
        mode='restate' if restate else 'window',
        tagged=tagged,
        effort_a=getattr(a, 'effort_a', 'medium'),
        agents=[agent['name'] for agent in agents] if restate else ['a'],
        run=str(run),
        since=since,
        until=until,
        views={
            name: {'chars': len(text), 'approx_tokens': len(text) / 4}
            for name, text in {'graph.md': graph, **selected['views']}.items()
        },
        packets={**agent_sizes, 'review-preview': preview_sizes},
    )
    save(run / 'manifest.json', manifest)
    return graph, window, common_slice, manifest


def execute(a, run, step_id, since, until, select, assign):
    graph, window, common_slice, manifest = prepare(a, run, step_id, since, until, select)
    draft_path = run / 'step.sqlite'
    if manifest.get('state') == 'empty':
        # Commit an empty draft so an outage with a quiet first piece cannot
        # strand the cut forever. No models run, and the prior carry-over stays
        # authoritative (this note deliberately is not gsd-step-run:).
        draft.snapshot(a.db, draft_path, step_id, since, until)
        with store.connect(draft_path, True) as db:
            feed.attach(db, manifest.get('feed'))
            db.commit()
        commit = draft.commit(draft_path, a.db, until, 'gsd-empty-run:' + str(run), store.PIPELINE_ACTOR)
        save(run / 'commit.json', commit)
        result = {
            'state': 'empty',
            'run': str(run),
            'empty_reason': 'no selected sources',
            'commit': commit,
        }
        save(run / 'result.json', result)
        return result
    if a.dry:
        return {'state': 'dry', 'run': str(run), 'manifest': manifest}
    restate = getattr(a, 'restate', False)
    # Sequential project writes avoid interleaved read/modify/write races on shared
    # people and project roll-ups. The first also warms the common prefix cache.
    manifest['a_turns'] = {}
    for name in manifest['agents']:
        calls = launch(run, name, a.a, until, effort_a=getattr(a, 'effort_a', 'medium'))  # A only
        manifest['a_turns'][name] = calls
        save(run / 'manifest.json', manifest)
    manifest['a_totals'] = turn_totals([c for cs in manifest['a_turns'].values() for c in cs])
    save(run / 'manifest.json', manifest)
    if restate:
        save(
            run / 'open.md',
            '\n\n'.join((run / f'open-{name}.md').read_text() for name in manifest['agents']),
        )
    with store.connect(draft_path, True) as db:
        changes = export.changes(db)
        tag_data = json.loads((run / 'tags.json').read_text()) if manifest['tagged'] else None
        plan = assign(
            read.graph(db), changes, tag_data,
            meetings=meetings_bundle(db, run) is not None,
            people=restate,
        )
        for cid, group in plan['assignments'].items():
            db.execute('UPDATE draft_change SET grp=? WHERE change_id=?', (group, cid))
        db.commit()
    save(run / 'groups.json', plan)
    for group, scopes in plan['scopes'].items():
        with store.connect(draft_path) as db:
            names = {p['id']: p['name'] for p in store.rows(db, 'projects')}
            scope = packets.review_scope(plan, group, names)
            slice_text = common_slice + '\n' + bundle(
                db, group, people=any(s['kind'] == 'people' for s in scopes)
            )
            if any(s['kind'] == 'meetings' for s in scopes):
                slice_text += '\n\n' + (meetings_bundle(db, run) or '')
        text, sizes = packets.packet(
            run, graph, window, slice_text, group, restate=restate,
            tagged=manifest['tagged'], scope=scope,
        )
        save_packet(run, group, text, sizes)
        manifest['packets'][group] = sizes
        save(run / 'manifest.json', manifest)
        launch(run, group, a.review, until, effort_review=getattr(a, 'effort_review', 'medium'))
    with store.connect(draft_path) as db:
        status = draft.status(db)
        # Reflect reviewer additions in the audit map without re-routing any
        # decisions that have already been made by the relay.
        plan['groups'] = {g: [] for g in plan['groups']}
        plan['assignments'] = {}
        for c in status['changes']:
            group = c['grp'] or 'unassigned'
            plan['groups'].setdefault(group, []).append(c['change_id'])
            plan['assignments'][c['change_id']] = group
        save(run / 'groups.json', plan)
        save(run / 'status.json', status)
        save(
            run / 'changes.jsonl',
            ''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in export.changes(db)),
        )
    result = draft.commit(draft_path, a.db, until, 'gsd-step-run:' + str(run), store.PIPELINE_ACTOR)
    save(run / 'commit.json', result)
    with store.connect(a.db) as db:
        save(run / 'state.json', export.state(db))
    report = {
        'state': 'committed',
        'run': str(run),
        'changes': sum(status['counts'].values()),
        'decisions': status['counts'],
        'tag_counts': tags.call_counts(tag_data['calls']) if tag_data else tags.call_counts([]),
        'groups': plan,
        'commit': result,
    }
    if manifest['tagged']:
        with store.connect(draft_path) as db:
            suspects = tags.misroutes(tag_data, export.changes(db), read.graph(db), result)
        save(
            run / 'misroutes.md',
            '# Routed but uncited (suspects, not errors)\n\n'
            + '\n'.join(
                f'{u["id"]}: ' + ', '.join(f'{t["project_id"]} p={t["p"]:.2f}' for t in u['tags'])
                for u in suspects
            )
            + '\n',
        )
        report['misroutes'] = {
            'units': len(suspects),
            'project_tags': sum(len(u['tags']) for u in suspects),
        }
    save(run / 'result.json', report)
    return report
