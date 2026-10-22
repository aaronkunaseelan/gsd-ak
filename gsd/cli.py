"""The verb table is both the agent contract and the CLI dispatch source."""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from . import db as store, read, write, draft


def stdin_json(required=True):
    raw = sys.stdin.read() if not sys.stdin.isatty() else ''
    if not raw.strip():
        if required:
            raise ValueError('expected JSON on stdin')
        return {}
    return json.loads(raw)


def sets(groups):
    result = {}
    for arg in (x for group in (groups or []) for x in group):
        if '=' not in arg:
            raise ValueError('--set expects FIELD=VALUE')
        f, v = arg.split('=', 1)
        try:
            v = json.loads(v)
        except ValueError:
            pass
        result[f] = v
    return result


def writable(a, path=None):
    if path is None and not a.explicit_db:
        raise ValueError('writes require --db FILE or GSD_DB; the live default is read-only')
    return store.connect(path or a.db, True)


def do_migrate(a):
    if not a.explicit_db:
        raise ValueError('migrate requires --db FILE or GSD_DB')
    path = Path(a.db).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        folded = store.migrate(db)
    os.chmod(path, 0o600)
    return {'schema_version': 4, 'db': str(path), 'folded': folded}


def do_read(a):
    with store.connect(a.db) as db:
        if a.command == 'view':
            return read.view(db, a.sparse)
        if a.command == 'show':
            return read.show(db, a.ids)
        if a.command == 'fields':
            data = read.fields(db)
            return data if a.json else read.fields_text(data)
        if a.command == 'cursor':
            return store.pipeline_cut(db)['until']
        data = draft.status(db, a.group)
        if a.json:
            return (
                {'counts': data['counts'], 'summary': draft.status_text(data, True)}
                if a.brief
                else data
            )
        return draft.status_text(data, a.brief)


def do_contract(a):
    path = store.ROOT / 'contracts' / (a.id + '.json')
    if not re.fullmatch(r'gsd\.[a-z]+\.v[0-9]+', a.id) or not path.is_file():
        raise ValueError('unknown contract id')
    return path.read_text()


def do_write(a):
    if a.command == 'write':
        payload = stdin_json()
    else:
        if a.command == 'upsert':
            fields = stdin_json(False)
            if not isinstance(fields, dict):
                raise ValueError('upsert stdin must be a field object')
            fields.update(sets(a.set))
            op = {'upsert': a.kind, 'set': fields, 'cite': [write.citation_flag(c) for c in a.cite]}
            if a.id:
                op['id'] = a.id
        elif a.command in ('link', 'end-link'):
            op = {a.command.replace('-', '_'): [a.frm, a.to, a.relation]}
        else:
            op = {'tombstone': a.id}
        payload = {
            'changes': [{'id': a.change_set, 'ops': [op]}] if a.change_set else [{'ops': [op]}]
        }
    with writable(a) as db:
        return write.batch(db, payload, a.actor, a.change_set)


def do_snapshot(a):
    return draft.snapshot(a.db, a.out, a.step_id, a.since, a.until)


def do_decide(a):
    if not a.change:
        payload = stdin_json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {'decisions'}
            or not isinstance(payload['decisions'], list)
        ):
            raise ValueError('expected {"decisions": [...]}')
        decisions = payload['decisions']
    else:
        if not a.decision:
            raise ValueError('decision is required')
        d = {'id': a.change, 'decision': a.decision, 'by': a.by or a.actor}
        if a.quote:
            d['quote'] = a.quote
        if a.set:
            d['set'] = sets(a.set)
        if a.note is not None:
            d['note'] = a.note
        decisions = [d]
    with writable(a) as db:
        return draft.decide(db, decisions, a.by or a.actor)


def do_commit(a):
    if not a.into and not a.explicit_db:
        raise ValueError('commit requires --into DB, --db FILE or GSD_DB')
    return draft.commit(a.draft, a.into or a.db, a.window_until, a.note, a.actor)


def do_publish(a):
    if a.target == 'notion':
        from .notion import publish

        return publish(
            a.db,
            a.check,
            a.seed,
            a.probe_comments,
            hardcut=a.hardcut,
            plan=a.plan,
            yes=a.yes,
        )
    from .qmd_publish import publish

    return publish(a.db, a.spool)


def do_archive_recover(a):
    if not a.explicit_db:
        raise ValueError('archive-recover requires --db FILE or GSD_DB')
    return draft.recover_archive(a.db, a.step_id)


def do_backup(a):
    from .backup import backup

    return backup(a.db)


# Each arguments entry is (flag/positional names, argparse settings).
# This table defines parsing, dispatch, help, examples and `verbs` output.
VERBS = [
    {
        'name': 'runs',
        'arguments': [(['--since'], {'default': None})],
        'effect': 'Read-only outcomes and costs from existing gsd run folders; missing evidence is null.',
        'example': 'gsd runs --json',
        'handler': lambda a: __import__('gsd.runs', fromlist=['command']).command(a),
    },
    {
        'name': 'steady',
        'arguments': [(['--once'], {'action': 'store_true', 'required': True})],
        'effect': 'One Steward run clipped to QMD fresh_through; finalise archive by step id, QMD, backup and gated Notion independently on every lock-held invocation, including quiet or failed runs. TERM skips finalisation.',
        'example': 'gsd steady --once',
        'handler': lambda a: __import__('gsd.steady', fromlist=['run']).run(a.db),
    },
    {
        'name': 'step',
        'arguments': [
            (['--since'], {'required': True}),
            (['--until'], {'required': True}),
            (['--run'], {}),
            (['--extra'], {'nargs': '+', 'action': 'extend'}),
            (['--a'], {'default': 'claude-opus-5-5'}),
            (['--review'], {'default': 'claude-opus-5-5'}),
            (['--dry'], {'action': 'store_true'}),
            (['--restate'], {'action': 'store_true'}),
            (['--tags'], {'dest': 'tags', 'action': 'store_true', 'default': True}),
            (['--no-tags'], {'dest': 'tags', 'action': 'store_false'}),
            (['--effort-a'], {'choices': ['low', 'medium', 'high'], 'default': 'medium'}),
            (['--effort-review'], {'choices': ['low', 'medium', 'high'], 'default': 'medium'}),
        ],
        'effect': 'Select/capture a private QMD window, snapshot, propose, review sequentially, commit and export. --restate skips selection and re-states each active project from its own sources, then one deep reviewer. --tags (default) calls Haiku even with --dry, groups the window and uses cluster/people/meetings review. --no-tags preserves the old window/review path. --effort-a sets the proposer effort; --effort-review sets the effort of every reviewer. --dry launches no proposer or reviewer.',
        'example': 'gsd step --since 2026-08-13 --until 2026-08-17 --db scratch.sqlite --dry',
        'handler': lambda a: __import__('gsd.step', fromlist=['run']).run(a),
    },
    {
        'name': 'intent',
        'arguments': [],
        'effect': 'Read-only intent/1 document for mech-d Page 2: obligation counts, project rows, flow, freshness chain and own last-run summary. No item titles or people.',
        'example': 'gsd intent --json',
        'handler': lambda a: __import__('gsd.intent', fromlist=['command']).command(a),
    },
    {
        'name': 'worklist',
        'arguments': [],
        'effect': 'Consistent gsd.worklist.v1 snapshot: pipeline cut, active projects, open items and 48h closure events for Steward and mech-d.',
        'example': 'gsd worklist --json',
        'handler': lambda a: __import__('gsd.worklist', fromlist=['command']).command(a),
    },
    {
        'name': 'brief',
        'arguments': [(['ids'], {'nargs': '+'})],
        'effect': 'gsd.brief.v1: per item, what it needs next (summary, next_actions, waiting_on), its people and qmd citations; an unknown or ambiguous id is an entry with error.',
        'example': 'gsd brief ID ID --json',
        'handler': lambda a: __import__('gsd.brief', fromlist=['command']).command(a),
    },
    {
        'name': 'closures',
        'arguments': [(['--since'], {'required': True})],
        'effect': "gsd.closures.v1: items closed (done or canceled) since a zoned ISO time, each at the closure's own time, with the pipeline cut.",
        'example': 'gsd closures --since 2026-10-20T00:00:00Z --json',
        'handler': lambda a: __import__('gsd.worklist', fromlist=['closures_command']).closures_command(a),
    },
    {
        'name': 'contract',
        'arguments': [(['id'], {})],
        'effect': 'Print a shipped contract by ID (no database required).',
        'example': 'gsd contract gsd.worklist.v1',
        'handler': do_contract,
    },
    {
        'name': 'export',
        'arguments': [],
        'effect': 'Current rows in the experiments state.json shape (projects, items, people, meetings, record_links, source_refs).',
        'example': 'gsd export --db scratch.sqlite --json',
        'handler': lambda a: __import__('gsd.export', fromlist=['command']).command(a),
    },
    {
        'name': 'publish',
        'arguments': [
            (['target'], {'choices': ['notion', 'qmd']}),
            (['--check'], {'action': 'store_true'}),
            (['--seed'], {'action': 'store_true'}),
            (
                ['--hardcut'],
                {'action': 'store_true', 'help': 'D16: export, then make Notion match gsd exactly'},
            ),
            (
                ['--plan'],
                {'action': 'store_true', 'help': 'Read-only --hardcut plan (requires token)'},
            ),
            (['--yes'], {'action': 'store_true', 'help': 'Authorize the destructive --hardcut'}),
            (
                ['--probe-comments'],
                {
                    'metavar': 'PAGE_ID',
                    'help': 'Owner-named live comment-survival probe; requires token and review flag',
                },
            ),
            (['--spool'], {}),
        ],
        'effect': 'Render projections; Notion --check is read-only, --seed imports port mapping.',
        'example': 'gsd publish notion --check',
        'handler': do_publish,
    },
    {
        'name': 'inbox',
        'arguments': [
            (['action'], {'choices': ['pull', 'status']}),
            (['--sweep'], {'action': 'store_true', 'help': 'read comments on every mapped page'}),
            (['--clear'], {'action': 'store_true', 'help': 'D22: clear comments a committed run has read'}),
        ],
        'effect': 'Notion inbox (gsd D22, D23): pull refreshes the root status page when its text changed and reads owner comments into inbox.sqlite (pages edited since the last pass; every page at least every 6 h); skips while publish holds the Notion lock. --clear recreates each commented block a committed run has read. status is read-only.',
        'example': 'gsd inbox pull --db ~/.local/share/gsd/gsd.sqlite',
        'handler': lambda a: __import__('gsd.inbox', fromlist=['command']).command(a),
    },
    {
        'name': 'backup',
        'arguments': [],
        'effect': 'VACUUM INTO private snapshot, verify, rclone copy --immutable --checksum to B2.',
        'example': 'gsd backup',
        'handler': do_backup,
    },
    {
        'name': 'migrate',
        'arguments': [],
        'effect': 'Idempotently create/migrate schema v3 (the only DDL path).',
        'example': 'gsd migrate --db scratch.sqlite',
        'handler': do_migrate,
    },
    {
        'name': 'view',
        'arguments': [(['--sparse'], {'action': 'store_true'})],
        'effect': 'Graph grouped by project, people, meetings, unattached items; written fields uncut unless sparse. JSON is the rendered string.',
        'example': 'gsd view --db scratch.sqlite --sparse',
        'handler': do_read,
    },
    {
        'name': 'show',
        'arguments': [(['ids'], {'nargs': '+'})],
        'effect': 'Whole records, bidirectional named links and QMD citations; ambiguous names/id prefixes list matches.',
        'example': 'gsd show NAME --db scratch.sqlite',
        'handler': do_read,
    },
    {
        'name': 'fields',
        'arguments': [],
        'effect': 'Every column, SQL type, vocabulary, default and real example per table.',
        'example': 'gsd fields --db scratch.sqlite --json',
        'handler': do_read,
    },
    {
        'name': 'verbs',
        'arguments': [],
        'effect': 'This exact dispatch contract as JSON (including common options and stdin shapes).',
        'example': 'gsd verbs --json',
        'handler': lambda a: contract(),
    },
    {
        'name': 'upsert',
        'arguments': [
            (['kind'], {'choices': list(store.KINDS)}),
            (['--id'], {}),
            (['--set'], {'action': 'append', 'nargs': '+'}),
            (['--cite'], {'action': 'append', 'default': []}),
        ],
        'effect': 'Partial update, null clears; new UUID4 record. Stdin is a field object. Required defaults are listed by fields; title/name is required. Citation REF[:role[:excerpt]] uses QMD ids or draft_ref short refs. Item closure needs a same-op decides excerpt; changed nonempty assigned_to needs commits or decides.',
        'example': 'gsd upsert item --set title=Example kind=task --cite gmail:example --db scratch.sqlite',
        'handler': do_write,
    },
    {
        'name': 'link',
        'arguments': [(['frm'], {}), (['to'], {}), (['relation'], {})],
        'effect': 'Append a directed link. Item belongs_to_project/assigned_to_person are refused: set project_id/assigned_to instead. Meeting belongs_to_project stays valid. Ids, prefixes or unambiguous names accepted.',
        'example': 'gsd link FROM TO relates_to --db scratch.sqlite',
        'handler': do_write,
    },
    {
        'name': 'end-link',
        'arguments': [(['frm'], {}), (['to'], {}), (['relation'], {})],
        'effect': 'Tombstone an existing link.',
        'example': 'gsd end-link FROM TO relates_to --db scratch.sqlite',
        'handler': do_write,
    },
    {
        'name': 'tombstone',
        'arguments': [(['id'], {})],
        'effect': 'Append entity and records tombstones; never delete history.',
        'example': 'gsd tombstone ID --db scratch.sqlite',
        'handler': do_write,
    },
    {
        'name': 'write',
        'arguments': [],
        'effect': 'Stdin {changes:[{id?,why?,confidence?,importance?,grp?,ops:[{upsert:KIND,id?,set:{FIELD:VALUE},cite:[{ref,role?,excerpt?}]},{link:[FROM,TO,RELATION]},{end_link:[FROM,TO,RELATION]},{tombstone:ID}]}]}. Each change is independently atomic; results contain ok or errors(field,value,why). Notes require a draft. Duplicate ids are errors.',
        'example': 'gsd write --db step.sqlite < changes.json',
        'handler': do_write,
    },
    {
        'name': 'snapshot',
        'arguments': [
            (['--out'], {'required': True}),
            (['--step-id'], {'required': True}),
            (['--since'], {'required': True}),
            (['--until'], {'required': True}),
        ],
        'effect': 'VACUUM INTO private draft; add review tables and snapshot maxima without modifying source.',
        'example': 'gsd snapshot --db scratch.sqlite --out steps/example.sqlite --step-id example --since 2026-08-13 --until 2026-08-17',
        'handler': do_snapshot,
    },
    {
        'name': 'decide',
        'arguments': [
            (['change'], {'nargs': '?'}),
            (['decision'], {'nargs': '?', 'choices': ['accept', 'amend', 'hold', 'reject']}),
            (['--quote'], {'nargs': 2, 'metavar': ('REF', 'TEXT')}),
            (['--set'], {'action': 'append', 'nargs': '+'}),
            (['--note'], {}),
            (['--by'], {}),
        ],
        'effect': 'Review one change, or stdin {decisions:[{id,decision,quote?:{ref,text},set?:{FIELD:VALUE},note?,by?}]}. A amend/reject requires quote. Amend requires fields and exactly one entity; appends corrected version under same change id with reviewer actor. Reviewer additions use write then self-accept.',
        'example': 'gsd decide a1 accept --by reviewer --db step.sqlite',
        'handler': do_decide,
    },
    {
        'name': 'status',
        'arguments': [(['--brief'], {'action': 'store_true'}), (['--group'], {})],
        'effect': 'Decision counts, undecided and change summaries; brief relay text is bounded for 60 changes. JSON brief returns counts and a summary string; full JSON returns all change fields.',
        'example': 'gsd status --brief --db step.sqlite',
        'handler': do_read,
    },
    {
        'name': 'commit',
        'arguments': [
            (['draft'], {}),
            (['--into'], {}),
            (['--window-until'], {}),
            (['--note'], {}),
        ],
        'effect': 'Refuse undecided draft. Land accepted/amended deltas in one pipeline change set; list moved records and land the rest. Live commits lock gsd.lock, freeze archive/<step_id>.sqlite locally before commit, then rclone copy the archive directory; non-live skips archive.',
        'example': 'gsd commit steps/example.sqlite --into scratch.sqlite',
        'handler': do_commit,
    },
    {
        'name': 'archive-check',
        'arguments': [],
        'effect': 'Read-only list of pipeline step ids missing archive/<step_id>.sqlite and their local run directories.',
        'example': 'gsd archive-check --json',
        'handler': lambda a: draft.archive_check(a.db),
    },
    {
        'name': 'archive-recover',
        'arguments': [(['step_id'], {})],
        'effect': 'Create missing archive/<step_id>.sqlite from the committed step note run/step.sqlite. No commit, cursor change or upload.',
        'example': 'gsd archive-recover EXAMPLE_STEP_ID --db FILE',
        'handler': do_archive_recover,
    },
    {
        'name': 'cursor',
        'arguments': [],
        'effect': 'max(window_until) from change sets the pipeline itself committed (actor pipeline), or null; hand-landed drafts never move it.',
        'example': 'gsd cursor --db scratch.sqlite',
        'handler': do_read,
    },
]
COMMON = [
    (['--db'], {'help': 'FILE, else GSD_DB, else live (read-only default)'}),
    (['--json'], {'action': 'store_true'}),
    (['--actor'], {'default': os.environ.get('GSD_ACTOR', 'owner')}),
    (['--change-set'], {'help': 'write lineage; batch id takes precedence'}),
]


def contract():
    common = [{'names': flags, **kw} for flags, kw in COMMON]
    return [
        {k: v for k, v in spec.items() if k != 'handler' and k != 'arguments'}
        | {
            'arguments': [{'names': flags, **kw} for flags, kw in spec['arguments']],
            'common': common,
        }
        for spec in VERBS
    ]


def main(argv=None):
    try:
        common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        for flags, kw in COMMON:
            common.add_argument(*flags, **kw)
        a, rest = common.parse_known_args(argv)
        a.explicit_db = bool(a.db or os.environ.get('GSD_DB'))
        a.db = a.db or os.environ.get('GSD_DB') or str(store.LIVE)
        parser = argparse.ArgumentParser(
            prog='gsd',
            allow_abbrev=False,
            description='Append-only work graph. Common flags may appear anywhere: --db FILE --json --actor NAME --change-set NAME',
        )
        sub = parser.add_subparsers(dest='command', required=True)
        for spec in VERBS:
            p = sub.add_parser(spec['name'], help=spec['effect'], allow_abbrev=False)
            for flags, kw in spec['arguments']:
                p.add_argument(*flags, **kw)
            p.set_defaults(handler=spec['handler'])
        a = parser.parse_args(rest, namespace=a)
        result = a.handler(a)
        if a.command == 'steady':
            return 1 if result['terminated'] else 0
        if a.command == 'contract':
            print(result, end='')
        elif a.json or not isinstance(result, str) or a.command == 'verbs':
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print(result, end='' if result.endswith('\n') else '\n')
        if isinstance(result, list) and any(
            isinstance(r, dict) and r.get('ok') is False for r in result
        ):
            return 1
        return 0
    except write.Invalid as e:
        print(json.dumps({'ok': False, 'errors': e.errors}, ensure_ascii=False))
        return 1
    except (ValueError, OSError, sqlite3.Error) as e:
        if getattr(a, 'command', None) == 'intent':
            print('gsd intent: snapshot unreadable', file=sys.stderr)
        else:
            print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            if getattr(a, 'command', None) == 'steady':
                from .runs import safe_cause
                print(safe_cause(e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
