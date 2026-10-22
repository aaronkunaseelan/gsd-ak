"""Synthetic tests for the base scripts: QMD is mocked, the store is a fresh migrated copy."""

import contextlib
import io
import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gsd import db as store, groups, write  # noqa: E402
if not (ROOT / 'port').is_dir():
    raise unittest.SkipTest('private legacy importer is not included in this archive')
from port import aliases, common, dedupe_links, rekey  # noqa: E402

CUT = common.CUT
UNTIL = CUT


class FakeQmd:
    """Stands in for the qmd CLI: browse rows per source, and the set of ids that get resolves."""

    def __init__(self, browse=None, resolvable=(), texts=None):
        self.browse_rows = browse or {}
        self.resolvable = set(resolvable)
        self.texts = texts or {}
        self.envs = []

    def __call__(self, argv, capture_output=True, text=True, env=None):
        self.envs.append(env.get('QMD_UNTIL'))
        if argv[0] != 'qmd':
            raise AssertionError(argv)
        if argv[1] == 'browse':
            source = argv[argv.index('--source') + 1]
            body = {'results': self.browse_rows.get(source, []), 'cursor': None}
            return mock.Mock(returncode=0, stdout=json.dumps(body))
        if argv[1] == 'get' and '--json' in argv:
            ids = list(itertools.takewhile(lambda a: not a.startswith('--'), argv[2:]))
            body = [{'id': i, 'text': self.texts.get(i, ''), 'truncated': False} for i in ids]
            return mock.Mock(returncode=0, stdout=json.dumps(body))
        if argv[1] == 'get':
            if argv[2] in self.texts:
                return mock.Mock(returncode=0, stdout=self.texts[argv[2]])
            return mock.Mock(returncode=0 if argv[2] in self.resolvable else 3, stdout='')
        raise AssertionError(argv)


def migrated(root):
    path = Path(root) / 'copy.sqlite'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    store.migrate(conn)
    cols = store.columns(conn, 'people')
    if 'other_emails' in cols:
        # Emulates the migration that renames people.other_emails to aliases.
        conn.execute('ALTER TABLE people RENAME COLUMN other_emails TO aliases')
    conn.commit()
    conn.close()
    return path


def open_rw(path):
    conn = store.connect(path, writable=True)
    return conn


def apply_ops(conn, ops):
    conn.execute('BEGIN IMMEDIATE')
    cid = str(uuid.uuid4())
    store.change_set(conn, cid, 'owner')
    ids = [write.operation(conn, op, cid, 'owner') for op in ops]
    conn.commit()
    return ids


def add_ref(conn, ref_id, record_id, system, surface, key):
    conn.execute('BEGIN IMMEDIATE')
    cid = str(uuid.uuid4())
    store.change_set(conn, cid, 'owner')
    data = write.base('source_refs', 'owner')
    data.update(
        source_ref_id=ref_id,
        record_id=record_id,
        source_system=system,
        source_surface=surface,
        source_key=key,
        role='states',
    )
    write.insert(conn, 'source_refs', data, cid, 'test', 'owner')
    conn.commit()


def add_link(conn, frm, to, rel):
    conn.execute('BEGIN IMMEDIATE')
    cid = str(uuid.uuid4())
    store.change_set(conn, cid, 'owner')
    data = write.base('record_links', 'owner')
    data.update(from_record_id=frm, to_record_id=to, relation_kind=rel)
    write.insert(conn, 'record_links', data, cid, 'test', 'owner')
    conn.commit()


def count(conn, table):
    return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


class Plumbing(unittest.TestCase):
    def test_two_months_before_cut(self):
        self.assertEqual(common.two_months_before(CUT), '2026-06-13T10:10:37Z')
        self.assertEqual(common.two_months_before('2026-12-31T00:00:00Z'), '2026-10-31T00:00:00Z')
        self.assertEqual(common.two_months_before('2026-05-31T00:00:00Z'), '2026-03-31T00:00:00Z')

    def test_fold_ignores_accents_case_and_punctuation(self):
        self.assertEqual(common.fold('Álice  EXAMPLE'), common.fold('alice example'))
        self.assertEqual(common.fold("O'Neil"), 'o neil')

    def test_refuses_live_store_and_its_directory(self):
        for path in (store.LIVE, store.LIVE.parent / 'other-copy.sqlite'):
            with self.assertRaises(ValueError):
                common.open_store(path)

    def test_refuses_missing_and_unmigrated_store(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                common.open_store(Path(root) / 'missing.sqlite')
            raw = Path(root) / 'raw.sqlite'
            sqlite3.connect(raw).close()
            with self.assertRaises(Exception):
                common.open_store(raw)

    def test_review_file_only_under_private_scratch(self):
        with tempfile.TemporaryDirectory() as scratch:
            scratch = Path(scratch)
            with mock.patch.object(common, 'REVIEW_ROOT', scratch):
                for bad in (ROOT / 'private-review.jsonl', scratch.parent / 'elsewhere.jsonl'):
                    with self.assertRaises(ValueError):
                        common.write_review(bad, [{'a': 1}])
                self.assertEqual(common.write_review(scratch / 'ok.jsonl', [{'a': 1}]), 1)
            self.assertFalse((ROOT / 'private-review.jsonl').exists())

    def test_window_defaults_from_effective_until(self):
        self.assertEqual(
            common.window(None, '2026-06-30T00:00:00Z'),
            ('2026-04-30T00:00:00Z', '2026-06-30T00:00:00Z'),
        )
        self.assertEqual(common.window(None, CUT)[0], '2026-06-13T10:10:37Z')

    def test_window_rejects_since_not_before_until(self):
        with self.assertRaises(ValueError):
            common.window('2026-08-13T10:10:37Z', CUT)
        with self.assertRaises(ValueError):
            common.window('2026-09-01T00:00:00Z', CUT)

    def test_cli_rejects_bad_or_inverted_windows(self):
        missing = str(ROOT / 'missing.sqlite')
        for main in (rekey.main, aliases.main):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main([missing, '--since', 'last tuesday'])
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main([missing, '--since', '2026-09-01T00:00:00Z', '--until', CUT])


class Rekey(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = migrated(self.temp.name)
        conn = open_rw(self.path)
        (person,) = apply_ops(
            conn, [{'upsert': 'person', 'set': {'display_name': 'Synthetic Person'}}]
        )
        self.record = person
        conn.close()
        browse = {
            'whatsapp': [
                {'id': 'wa:1:1@x.test:ABC'},
                {'id': 'wa:2:2@x.test:DUP'},
                {'id': 'wa:3:3@x.test:DUP'},
            ],
            'fathom': [{'id': 'fathom:acct@x.test:123'}],
        }
        self.fake = FakeQmd(
            browse,
            resolvable={'gmail:acct@x.test:m1', 'fathom:acct@x.test:123', 'wa:1:1@x.test:ABC'},
        )
        conn = open_rw(self.path)
        add_ref(conn, 1, self.record, 'gmail', 'email.message', 'acct@x.test/m1')
        add_ref(conn, 2, self.record, 'gmail', 'email.message', 'acct@x.test/m2')
        add_ref(conn, 3, self.record, 'fathom', 'recording', '123')
        add_ref(conn, 4, self.record, 'fathom', 'action_item', 'recording_id:123/item-9')
        add_ref(conn, 5, self.record, 'whatsapp', 'message', 'ABC')
        add_ref(conn, 6, self.record, 'whatsapp', 'message', 'DUP')
        add_ref(conn, 7, self.record, 'cot_sync', 'gmail.message', 'acct@x.test/m9')
        conn.close()

    def run_rekey(self, *args):
        out = io.StringIO()
        with mock.patch('subprocess.run', side_effect=self.fake), contextlib.redirect_stdout(out):
            rekey.main([str(self.path), *args, '--until', UNTIL])
        return json.loads(out.getvalue())

    def test_apply_maps_verified_ids_and_leaves_the_rest(self):
        conn = open_rw(self.path)
        before = count(conn, 'source_refs')
        conn.close()
        report = self.run_rekey()
        self.assertEqual(set(self.fake.envs), {UNTIL})
        self.assertEqual(report['by_system']['gmail'], {'mapped': 1, 'unresolved': 1})
        self.assertEqual(report['by_system']['fathom'], {'mapped': 2})
        self.assertEqual(report['by_system']['whatsapp'], {'mapped': 1, 'unresolved': 1})
        self.assertEqual(report['by_system']['cot_sync'], {'unmappable': 1})
        conn = open_rw(self.path)
        self.assertEqual(count(conn, 'source_refs'), before + 4)
        keyed = {
            r['source_ref_id']: (r['evidence_qmd_event_id'], r['source_key'])
            for r in store.rows(conn, 'source_refs')
            if r['evidence_qmd_event_id']
        }
        self.assertEqual(
            keyed,
            {
                1: ('gmail:acct@x.test:m1', 'gmail:acct@x.test:m1'),
                3: ('fathom:acct@x.test:123', 'fathom:acct@x.test:123'),
                4: ('fathom:acct@x.test:123', 'fathom:acct@x.test:123'),
                5: ('wa:1:1@x.test:ABC', 'wa:1:1@x.test:ABC'),
            },
        )
        self.assertTrue(report['change_set'])
        cs = conn.execute(
            'SELECT origin, note FROM change_sets WHERE change_set_id=?', (report['change_set'],)
        ).fetchone()
        self.assertEqual((cs['origin'], cs['note']), ('port', 'port/rekey.py'))
        conn.close()

    def test_dry_writes_nothing(self):
        conn = open_rw(self.path)
        before = count(conn, 'source_refs')
        conn.close()
        report = self.run_rekey('--dry')
        self.assertEqual(report['mode'], 'dry')
        self.assertNotIn('change_set', report)
        conn = open_rw(self.path)
        self.assertEqual(count(conn, 'source_refs'), before)
        conn.close()

    def test_failure_mid_run_rolls_back_everything(self):
        real = write.insert
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('boom')
            return real(*args, **kwargs)

        conn = open_rw(self.path)
        before = count(conn, 'source_refs')
        changes = count(conn, 'change_sets')
        conn.close()
        with mock.patch.object(write, 'insert', side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.run_rekey()
        conn = open_rw(self.path)
        self.assertEqual(count(conn, 'source_refs'), before)
        self.assertEqual(count(conn, 'change_sets'), changes)
        conn.close()


class Aliases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = migrated(self.temp.name)
        patcher = mock.patch.object(common, 'REVIEW_ROOT', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = open_rw(self.path)
        ids = apply_ops(
            conn,
            [
                {
                    'upsert': 'person',
                    'set': {
                        'display_name': 'Alice Example',
                        'primary_email': 'alice@synthetic.test',
                    },
                },
                {
                    'upsert': 'person',
                    'set': {'display_name': 'Bob Sample', 'primary_email': 'bob@synthetic.test'},
                },
                {
                    'upsert': 'person',
                    'set': {
                        'display_name': 'Carol Test',
                        'primary_email': 'carol-main@synthetic.test',
                    },
                },
                {
                    'upsert': 'person',
                    'set': {'display_name': 'Dana Twin', 'primary_email': 'dana1@synthetic.test'},
                },
                {
                    'upsert': 'person',
                    'set': {'display_name': 'Dana Twin', 'primary_email': 'dana2@synthetic.test'},
                },
                {
                    'upsert': 'person',
                    'set': {'display_name': groups.owner_name(), 'primary_email': groups.owner_email()},
                },
            ],
        )
        self.alice, self.bob, self.carol, self.dana1, self.dana2, self.owner = ids
        conn.close()
        mail = [
            {'id': 'gmail:acct:m1', 'from': 'Álice  EXAMPLE <alice.work@synthetic.test>'},
            {'id': 'gmail:acct:m2', 'from': 'Bob Sample <bob.work@synthetic.test>'},
            {'id': 'gmail:acct:m3', 'from': 'Alice <alice.short@synthetic.test>'},
            {'id': 'gmail:acct:m4', 'from': 'Dana Twin <dana.x@synthetic.test>'},
            {'id': 'gmail:acct:m5', 'from': 'noreply Bob Sample <noreply@synthetic.test>'},
            {'id': 'gmail:acct:m6', 'from': 'Carol Test <carol@synthetic.test>'},
            {'id': 'gmail:acct:m7', 'from': 'Bob Sample <shared@synthetic.test>'},
            {'id': 'gmail:acct:m8', 'from': 'Alice Example <shared@synthetic.test>'},
            {'id': 'gmail:acct:m9', 'from': 'Ali <ali@synthetic.test>'},
            {'id': 'gmail:acct:m10', 'from': 'Alice Example <alice@synthetic.test>'},
        ]
        wa = [
            # One-to-one chat: the counterparty's name, the chat JID is the alias.
            {'id': 'wa:acct:4477123@s.whatsapp.net:W1', 'from': 'Bob Sample'},
            # Outgoing: the owner's own message in a one-to-one chat is never used.
            {'id': 'wa:acct:4477123@s.whatsapp.net:W2', 'from': groups.owner_name()},
            # Group chat: never used.
            {'id': 'wa:acct:120363@g.us:W3', 'from': 'Carol Test'},
            # The account part contains a colon; the chat is still the second-last segment.
            {'id': 'wa:acc:1:4477555@s.whatsapp.net:W4', 'from': 'Carol Test'},
            # Sender is a bare number, not a name: unlinked.
            {'id': 'wa:acct:4477:9@s.whatsapp.net:W5', 'from': '4477:1@s.whatsapp.net'},
            # Ambiguous name (two Dana Twins).
            {'id': 'wa:acct:4478000@s.whatsapp.net:W6', 'from': 'Dana Twin'},
            # Name with a bare number appended does not exactly match: no alias.
            {'id': 'wa:acct:4479000@s.whatsapp.net:W7', 'from': 'Carol Test +44 7700 900123'},
        ]
        self.fake = FakeQmd({'gmail': mail, 'whatsapp': wa})

    def run_aliases(self, *args):
        out = io.StringIO()
        with mock.patch('subprocess.run', side_effect=self.fake), contextlib.redirect_stdout(out):
            aliases.main([str(self.path), *args, '--until', UNTIL])
        return json.loads(out.getvalue())

    def aliases_of(self, pid):
        conn = open_rw(self.path)
        row = conn.execute('SELECT aliases FROM cur_people WHERE id=?', (pid,)).fetchone()
        conn.close()
        return json.loads(row['aliases'])

    def test_exact_full_names_only_and_cited(self):
        report = self.run_aliases()
        self.assertEqual(set(self.fake.envs), {UNTIL})
        self.assertEqual(self.aliases_of(self.alice), ['alice.work@synthetic.test'])
        self.assertEqual(
            self.aliases_of(self.bob), ['4477123@s.whatsapp.net', 'bob.work@synthetic.test']
        )
        self.assertEqual(
            self.aliases_of(self.carol), ['4477555@s.whatsapp.net', 'carol@synthetic.test']
        )
        self.assertEqual(self.aliases_of(self.dana1), [])
        self.assertEqual(self.aliases_of(self.dana2), [])
        self.assertEqual(self.aliases_of(self.owner), [])
        conn = open_rw(self.path)
        cites = conn.execute(
            "SELECT record_id, evidence_qmd_event_id FROM cur_source_refs WHERE role='states' AND record_id IN (?,?,?)",
            (self.alice, self.bob, self.carol),
        ).fetchall()
        conn.close()
        self.assertEqual(
            sorted((r['record_id'], r['evidence_qmd_event_id']) for r in cites),
            sorted(
                [
                    (self.alice, 'gmail:acct:m1'),
                    (self.bob, 'gmail:acct:m2'),
                    (self.bob, 'wa:acct:4477123@s.whatsapp.net:W1'),
                    (self.carol, 'gmail:acct:m6'),
                    (self.carol, 'wa:acc:1:4477555@s.whatsapp.net:W4'),
                ]
            ),
        )
        counts = report['counts']
        self.assertEqual(counts['aliases_added'], 5)
        self.assertEqual(counts['whatsapp_jid_added'], 2)
        self.assertEqual(counts['email_added'], 3)
        self.assertEqual(counts['people_with_new_alias'], 3)
        self.assertEqual(counts['whatsapp_group_skipped'], 1)
        self.assertEqual(counts['whatsapp_outgoing_skipped'], 1)
        self.assertEqual(counts['whatsapp_number_unlinked'], 1)
        self.assertEqual(counts['whatsapp_no_name_match'], 1)
        self.assertEqual(counts['ambiguous'], 2)
        self.assertEqual(counts['partial'], 1)
        self.assertEqual(counts['conflict'], 1)
        self.assertEqual(counts['automated_skipped'], 1)
        self.assertNotIn('ali@synthetic.test', json.dumps(report))
        self.assertNotIn('120363', json.dumps(report))

    def test_rerun_adds_only_new_jids(self):
        self.run_aliases()
        report = self.run_aliases()
        self.assertEqual(report['counts']['aliases_added'], 0)
        self.assertEqual(report['counts']['whatsapp_jid_added'], 0)
        self.assertNotIn('change_set', report)

    def test_partial_ambiguous_conflict_and_nickname_are_not_written(self):
        review = Path(self.temp.name) / 'review.jsonl'
        self.run_aliases('--review', str(review))
        kinds = [json.loads(line)['kind'] for line in review.read_text().splitlines()]
        self.assertIn('partial', kinds)
        self.assertIn('ambiguous', kinds)
        self.assertIn('conflict', kinds)
        self.assertNotIn('ali@synthetic.test', self.aliases_of(self.alice))

    def test_existing_aliases_are_kept_and_merged(self):
        conn = open_rw(self.path)
        apply_ops(
            conn,
            [
                {
                    'upsert': 'person',
                    'id': self.bob,
                    'set': {'aliases': json.dumps(['old@synthetic.test'])},
                }
            ],
        )
        conn.close()
        self.run_aliases()
        self.assertEqual(
            self.aliases_of(self.bob),
            ['4477123@s.whatsapp.net', 'bob.work@synthetic.test', 'old@synthetic.test'],
        )

    def test_dry_writes_nothing(self):
        report = self.run_aliases('--dry')
        self.assertEqual(report['counts']['aliases_added'], 5)
        self.assertNotIn('change_set', report)
        self.assertEqual(self.aliases_of(self.alice), [])

    def test_default_window_is_two_months_before_cut(self):
        out = io.StringIO()
        with mock.patch('subprocess.run', side_effect=self.fake), contextlib.redirect_stdout(out):
            aliases.main([str(self.path), '--dry', '--until', UNTIL])
        self.assertEqual(json.loads(out.getvalue())['since'], '2026-06-13T10:10:37Z')


class Dedupe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = migrated(self.temp.name)
        patcher = mock.patch.object(common, 'REVIEW_ROOT', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = open_rw(self.path)
        p_alpha, p_beta, alice, bob, carol = apply_ops(
            conn,
            [
                {'upsert': 'project', 'set': {'name': 'Project Alpha'}},
                {'upsert': 'project', 'set': {'name': 'Project Beta'}},
                {'upsert': 'person', 'set': {'display_name': 'Alice Example'}},
                {'upsert': 'person', 'set': {'display_name': 'Bob Sample'}},
                {'upsert': 'person', 'set': {'display_name': 'Carol Test'}},
            ],
        )
        cite = [{'ref': 'gmail:acct:m1', 'role': 'commits', 'excerpt': 'synthetic'}]
        i1, i2, i3 = apply_ops(
            conn,
            [
                {
                    'upsert': 'item',
                    'set': {'title': 'One', 'project_id': p_alpha, 'assigned_to': 'Alice Example'},
                    'cite': cite,
                },
                {
                    'upsert': 'item',
                    'set': {'title': 'Two', 'project_id': p_beta, 'assigned_to': 'Bob Sample'},
                    'cite': cite,
                },
                {'upsert': 'item', 'set': {'title': 'Three'}},
            ],
        )
        conn.close()
        conn = open_rw(self.path)
        add_link(conn, i1, p_alpha, 'belongs_to_project')
        add_link(conn, i2, p_alpha, 'belongs_to_project')
        add_link(conn, i3, p_alpha, 'belongs_to_project')
        add_link(conn, i1, alice, 'assigned_to_person')
        add_link(conn, i2, carol, 'assigned_to_person')
        conn.close()
        self.ids = dict(i1=i1, i2=i2, i3=i3, alpha=p_alpha, beta=p_beta, alice=alice)

    def run_dedupe(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dedupe_links.main([str(self.path), *args])
        return json.loads(out.getvalue())

    def live_links(self):
        conn = open_rw(self.path)
        rows = {
            (r['from_record_id'], r['to_record_id'], r['relation_kind'])
            for r in store.rows(conn, 'record_links')
        }
        conn.close()
        return rows

    def item_row(self, key):
        conn = open_rw(self.path)
        row = conn.execute('SELECT * FROM cur_items WHERE id=?', (self.ids[key],)).fetchone()
        conn.close()
        return dict(row)

    def test_each_rule_is_applied_and_counted_separately(self):
        before = self.live_links()
        review = Path(self.temp.name) / 'dedupe.jsonl'
        report = self.run_dedupe('--review', str(review))
        self.assertEqual(
            before - self.live_links(),
            {
                (self.ids['i1'], self.ids['alpha'], 'belongs_to_project'),
                (self.ids['i1'], self.ids['alice'], 'assigned_to_person'),
                (self.ids['i2'], self.ids['alpha'], 'belongs_to_project'),
                (self.ids['i3'], self.ids['alpha'], 'belongs_to_project'),
            },
        )
        # Rule 2: column kept, old target kept as relates_to.
        self.assertIn((self.ids['i2'], self.ids['alpha'], 'relates_to'), self.live_links())
        self.assertEqual(self.item_row('i2')['project_id'], self.ids['beta'])
        counts = report['counts']
        self.assertEqual(counts['belongs_to_project_total'], 3)
        self.assertEqual(counts['belongs_to_project_ended'], 1)
        self.assertEqual(counts['belongs_to_project_set_column'], 1)
        self.assertEqual(counts['belongs_to_project_relinked'], 1)
        self.assertEqual(counts.get('belongs_to_project_listed', 0), 0)
        self.assertEqual(counts['assigned_to_person_ended'], 1)
        self.assertEqual(counts['assigned_to_person_listed'], 1)
        listed = [json.loads(l) for l in review.read_text().splitlines()]
        self.assertEqual([row['relation'] for row in listed], ['assigned_to_person'])

    def test_empty_project_column_is_set_in_the_same_change_set(self):
        report = self.run_dedupe()
        row = self.item_row('i3')
        self.assertEqual(row['project_id'], self.ids['alpha'])
        conn = open_rw(self.path)
        same = conn.execute(
            'SELECT COUNT(*) FROM items WHERE id=? AND change_set_id=?',
            (self.ids['i3'], report['change_set']),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(same, 1)

    def test_meeting_project_link_is_retained_not_listed(self):
        conn = open_rw(self.path)
        (meeting,) = apply_ops(conn, [{'upsert': 'meeting', 'set': {'title': 'Synthetic sync'}}])
        conn.close()
        conn = open_rw(self.path)
        add_link(conn, meeting, self.ids['alpha'], 'belongs_to_project')
        conn.close()
        before = self.live_links()
        review = Path(self.temp.name) / 'meeting.jsonl'
        report = self.run_dedupe('--review', str(review))
        self.assertIn((meeting, self.ids['alpha'], 'belongs_to_project'), self.live_links())
        self.assertEqual(
            self.live_links(),
            before
            - {
                (self.ids['i1'], self.ids['alpha'], 'belongs_to_project'),
                (self.ids['i1'], self.ids['alice'], 'assigned_to_person'),
                (self.ids['i2'], self.ids['alpha'], 'belongs_to_project'),
                (self.ids['i3'], self.ids['alpha'], 'belongs_to_project'),
            }
            | {(self.ids['i2'], self.ids['alpha'], 'relates_to')},
        )
        self.assertEqual(report['counts']['belongs_to_project_retained'], 1)
        self.assertEqual(report['counts'].get('belongs_to_project_listed', 0), 0)
        listed = [json.loads(l) for l in review.read_text().splitlines()]
        self.assertNotIn(meeting, {row['from'] for row in listed})

    def test_dry_writes_nothing(self):
        before = self.live_links()
        report = self.run_dedupe('--dry')
        self.assertEqual(report['counts']['belongs_to_project_ended'], 1)
        self.assertEqual(self.live_links(), before)


if __name__ == '__main__':
    unittest.main()
