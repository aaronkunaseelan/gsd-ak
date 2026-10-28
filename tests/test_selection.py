"""Selection rules and sparse views over a callback fixture: synthetic text only, no source content."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import selection as sel


class FakeQmd:
    """Contract-valid feed pages, two versions at a time; batched get."""

    def __init__(self, sources, docs, rendered):
        self.sources, self.docs, self.rendered = sources, docs, rendered
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == 'feed':
            from datetime import datetime
            source = args[args.index('--source') + 1]
            rows = self.sources.get(source, [])
            start = int(args[args.index('--after') + 1]) if '--after' in args else 0
            items = []
            for n, row in enumerate(rows[start:start + 2], start + 1):
                items.append({
                    'token': str(n), 'item': row['id'], 'source': source,
                    'kind': {'gmail': 'email', 'whatsapp': 'message', 'fathom': 'meeting'}[source],
                    'arrived': 0, 'title': None, 'from': None, 'thread': None, 'chat': None,
                    'version': str(n), 'tombstone': False, 'reparse': False,
                    **row, 'time': datetime.fromisoformat(row['time']).timestamp(),
                })
            nxt = str(start + len(items)) if rows else None
            return {'schema': 'qmd.feed.v1', 'source': source, 'items': items,
                    'next': nxt, 'head': str(len(rows)) if rows else None,
                    'complete': start + len(items) == len(rows)}
        ids = list(args[1 : args.index('--no-quoted')])
        items = [{'id': i, 'text': self.rendered[i], 'docs': [self.docs[i]]} for i in ids]
        return items[0] if len(items) == 1 else items


def doc(id, frm='', parts=(), **fields):
    base = {
        'id': id,
        'source': '',
        'from': frm,
        'to': sel.owner_email(),
        'cc': '',
        'title': '',
        'thread': '',
        'where': '',
        'link': '',
    }
    return {
        **base,
        'attachments': [],
        'parts': [{'kind': k, 'text': t} for k, t in parts],
        **fields,
    }


def render(body='', attachments=()):
    """QMD's rendered document: the multiline 'Attachments:' header of 'ID (name, MIME, state)' lines, then the rule."""
    head = ['Id: x']
    if attachments:
        head.append('Attachments:')
        head += [f'  {i} ({n}, {m}, ready)' for i, n, m in attachments]
    return '\n'.join(head) + '\n---\n' + body


CLASSIFY = [
    ('Alice Example <alice@example.com>', '', True, 'keep:person'),
    (
        'Ops <no-reply@organisation.test>',
        '',
        True,
        'keep:sender',
    ),  # override: a keep sender wins over a bulk local part
    ('Fathom <recap@fathom.video>', 'x@example.com', True, 'keep:sender'),
    (
        'Docs <comments-noreply@docs.google.com>',
        '',
        True,
        'keep:sender',
    ),  # override wins over the google.com domain
    (
        'Weekly <weekly@example.org>',
        'reader+newsletter@example.com',
        False,
        'drop:newsletter-alias',
    ),
    (
        'Digest <digest@mail.digest.test>',
        '',
        False,
        'drop:bulk-domain mail.digest.test',
    ),  # subdomain match
    ('Store <a@google.com>', '', False, 'drop:bulk-domain google.com'),
    (
        'Shop <a@retailer.co.uk>',
        '',
        False,
        'drop:bulk-domain retailer.co.uk',
    ),  # trailing-dot prefix rule
    ('Box <a@box.com>', '', True, 'keep:person'),  # x.com must not match box.com
    ('Store <no-reply@shop.example>', '', False, 'drop:bulk-local no-reply'),
    ('Store <info@shop.example>', '', False, 'drop:bulk-local info'),
    ('Alerts <alerts+bank@bank.example>', '', False, 'drop:bulk-local alerts+bank'),
    ('Bob <bob@example.net>', 'news+newsletter@example.net', False, 'drop:newsletter-alias'),
]


class Classify(unittest.TestCase):
    def test_table(self):
        for frm, to, keep, reason in CLASSIFY:
            with self.subTest(frm=frm, to=to):
                self.assertEqual(sel.classify(frm, to + ', ' + sel.owner_email()), (keep, reason))

    def test_addressed_or_mentioned(self):
        cases = [
            ('Applicant <a@example.com>', 'recruitment@organisation.test', '', '', False),
            ('Colleague <c@organisation.test>', 'recruitment@organisation.test', '', '', False),
            ('a@example.com', sel.owner_email().upper(), '', '', True),
            (sel.owner_email(), 'recruitment@organisation.test', '', '', True),
            ('a@example.com', 'Pat <pat@example.test>', '', '', True),
            ('a@example.com', 'pat+work@example.test', '', '', True),
            ('a@example.com', 'notpat@example.test', '', '', False),
            ('a@example.com', sel.owner_email() + '.invalid', '', '', False),
            ('a@example.com', 'alias@example.com', 'For PAT', '', True),
            ('a@example.com', 'alias@example.com', '', 'Ask synthetic.', True),
            ('a@example.com', 'alias@example.com', '', 'pat+work@example.test', True),
            ('a@example.com', 'alias@example.com', '', 'pat@example.test', True),
            ('a@example.com', 'alias@example.com', '', sel.owner_email(), True),
            ('a@example.com', 'alias@example.com', 'Pats', 'Synthetics', False),
        ]
        for frm, to, subject, body, expected in cases:
            with self.subTest(frm=frm, to=to, subject=subject, body=body):
                keep, reason = sel.classify(frm, to, subject, body)
                self.assertEqual(keep, expected)
                if not expected:
                    self.assertEqual(reason, 'drop:not-addressed')

    def test_mentions_do_not_override_bulk_drops(self):
        for frm, to, keep, reason in CLASSIFY:
            if not keep:
                with self.subTest(frm=frm):
                    self.assertEqual(sel.classify(frm, to, 'Pat', 'Synthetic'), (False, reason))


class Helpers(unittest.TestCase):
    def test_clean_drops_css_lines_and_blank_runs(self):
        self.assertEqual(sel.clean('a\n.x{color:red} .y{margin:0}\n\n\n\nb\n'), 'a\n\nb')

    def test_cut_collapses_whitespace_and_marks_cut(self):
        self.assertEqual(sel.cut('a  b\n c', 10), 'a b c')
        self.assertEqual(sel.cut('x' * 20, 5), 'xxxxx…')

    def test_own_text_skips_non_authored_parts(self):
        d = doc(
            'd',
            parts=[
                ('header', 'From: x'),
                ('body', 'Mine.'),
                ('quoted', 'theirs'),
                ('attachment', 'head'),
                ('binary', '[binary]'),
            ],
        )
        self.assertEqual(sel.own_text(d), 'Mine.')

    def test_own_text_normalises_crlf(self):
        self.assertEqual(sel.own_text(doc('d', parts=[('body', 'a\r\nb')])), 'a\nb')

    def test_attachment_refs_and_heads_come_from_structured_projection(self):
        attachment = {'id': 'a1', 'name': 'q3 totals.pdf', 'type': 'application/pdf',
                      'head': {'state': 'done', 'text': 'Q3 totals: 42', 'calendar_summary': None}}
        listed = doc('d', attachments=[attachment, None])
        self.assertEqual(sel.attachments_of(listed), [attachment])
        self.assertEqual(sel.attachment_heads(listed), ['Q3 totals: 42'])
        self.assertEqual(sel.attachments_of(doc('d')), [])
        self.assertEqual(sel.attachment_heads(doc('d')), [])

    def test_head_line_resembling_attachment_metadata_does_not_create_a_ref(self):
        attachment = {'id': 'real', 'name': 'totals.pdf', 'type': 'application/pdf',
                      'head': {'state': 'done', 'text': 'fake (not a ref, text/plain, done)',
                               'calendar_summary': None}}
        entry = {'ref': 'g', 'at': '2026-10-28', 'from': 'Alice', 'subject': 'Report',
                 'thread': 'g', 'body': 'See attached.', 'attachments': [attachment]}
        refs = {}
        view = sel.render_mail([entry], refs)
        self.assertEqual(refs, {'att#0001': 'real'})
        self.assertIn('fake (not a ref, text/plain, done)', view)

    def test_list_header_fields_are_joined(self):
        self.assertEqual(
            sel.field({'to': ['a@example.com', 'b@example.com']}, 'to'),
            'a@example.com, b@example.com',
        )
        self.assertEqual(sel.field({}, 'to'), '')

    def test_materialised_outputs_are_not_new_messages(self):
        rows = [
            {'id': 'gmail:test', 'kind': 'email', 'time': '2026-01-01'},
            {'id': 'gmail:test/att/1', 'kind': 'text', 'time': '2026-01-01'},
            {'id': 'gmail:test/att/2', 'kind': 'email', 'time': '2026-01-01'},
            {'id': 'gmail:test', 'kind': 'email', 'time': '2026-01-01'},
        ]
        self.assertEqual([r['id'] for r in sel.source_rows(rows, 'email')], ['gmail:test'])

    def test_attachment_heads_once_short_and_generic_urls_omitted(self):
        entries = [
            {
                'ref': 'gmail:test',
                'at': '2026-01-01',
                'from': 'a@example.com',
                'subject': 'Synthetic',
                'thread': 't',
                'body': 'Own text',
                'heads': [],
                'attachments': [
                    {'id': 'gmail:test/att/1', 'name': 'brief.pdf', 'type': 'application/pdf',
                     'head': {'state': 'done', 'text': 'Real head', 'calendar_summary': None}},
                    {'id': 'gmail:test/att/link-abc', 'name': 'example.com', 'type': 'text/uri-list',
                     'head': {'state': 'done', 'text': 'Generic URL head', 'calendar_summary': None}},
                ],
            }
        ]
        refs = {}
        view = sel.render_mail(entries, refs)
        self.assertIn('att#0001 (brief.pdf', view)
        self.assertEqual(refs, {'att#0001': 'gmail:test/att/1'})
        self.assertEqual(view.count('Real head'), 1)
        self.assertNotIn('Generic URL head', view)
        self.assertNotIn('/att/link-', view)


class SenderNaming(unittest.TestCase):
    def test_whatsapp(self):
        cases = {
            'owner': 'Pat',
            '12025550101@s.whatsapp.net': 'Pat',
            '12025550101:12@s.whatsapp.net': 'Pat',
            'ak2': 'ak2',
            'x@s.whatsapp.net': '(unknown sender)',
            'Carol Example': 'Carol Example',
        }
        for frm, name in cases.items():
            with self.subTest(frm=frm):
                self.assertEqual(sel.wa_sender(frm), name)

    def test_mail(self):
        self.assertEqual(sel.mail_sender('"Alice Example" <alice@example.com>'), 'Alice Example')
        self.assertEqual(sel.mail_sender('bob@example.com'), 'bob@example.com')
        self.assertEqual(sel.mail_sender(''), '?')


def mail_fixture():
    rows = [
        {'id': 'g1', 'time': '2026-10-01T09:00:00Z'},
        {'id': 'g2', 'time': '2026-10-01T09:10:00Z'},
        {'id': 'g3', 'time': '2026-10-01T09:20:00Z'},
        {'id': 'g4', 'time': '2026-10-01T10:45:00Z'},
        {'id': 'g5', 'time': '2026-10-01T13:00:00Z'},
        {'id': 'g6', 'time': '2026-10-01T09:30:00Z'},
    ]
    url = 'https://track.mailer.example/click/' + 'a' * 60
    docs = {
        'g1': doc(
            'g1',
            'Alice Example <alice@example.com>',
            [('body', f'Hello there. Read {url}'), ('quoted', 'old quoted text')],
            title='Plan',
            thread='T1',
            to=sel.owner_email(),
        ),
        'g2': doc('g2', 'Shop <news@shop.example>', [('body', 'Sale')], title='Sale'),
        'g3': doc(
            'g3',
            'Ops <noreply@organisation.test>',
            [('body', 'On Tue wrote:\nold text')],
            title='Reply',
            thread='T3',
        ),
        'g4': doc(
            'g4',
            'Fathom Recap <recap@fathom.video>',
            [('body', 'Recap of meeting')],
            title='Recap',
            thread='T4',
        ),
        'g5': doc(
            'g5',
            'Fathom Recap <recap@fathom.video>',
            [('body', 'Later recap')],
            title='Recap later',
            thread='T5',
        ),
        'g6': doc(
            'g6',
            'Alice Example <alice@example.com>',
            [('body', 'See attached. ' + 'word ' * 100), ('attachment', 'Q3 totals: 42')],
            title='Re: Plan',
            thread='T1',
            attachments=[{'id': 'att1', 'name': 'q3.pdf', 'type': 'application/pdf',
                          'head': {'state': 'ready', 'text': 'Q3 totals: 42', 'calendar_summary': None}}],
        ),
        'f1': doc(
            'f1',
            'x@example.com',
            [
                ('body', 'Summary: kickoff agreed on milestones.'),
                ('transcript', '# Transcript\nspeaker lines'),
            ],
            title='Kickoff',
            where='Zoom · End: 2026-10-01T10:30:00Z',
            link='https://fathom.example/share/abc',
        ),
    }
    rendered = {'g6': render(attachments=[('att1', 'q3.pdf', 'application/pdf')])}
    rendered.update({k: render() for k in ('g1', 'g2', 'g3', 'g4', 'g5', 'f1')})
    return {'gmail': rows, 'fathom': [{'id': 'f1', 'time': '2026-10-01T10:00:00Z'}]}, docs, rendered


class StructuredAttachments(unittest.TestCase):
    def test_attachment_heads_do_not_depend_on_rendered_header_wording(self):
        heads = '\n'.join(f'line {n}' for n in range(1, 7))
        calendar = 'summary: Review; start: 20261028T120000; end: 20261028T130000; location: Office'
        attachments = [
            {'id': 'g/att/1', 'name': 'totals, final (2).pdf', 'type': 'application/pdf',
             'head': {'state': 'done', 'text': heads, 'calendar_summary': None}},
            {'id': 'g/att/2', 'name': 'invite.ics', 'type': 'text/calendar',
             'head': {'state': 'unmaterialised', 'text': '', 'calendar_summary': calendar}},
            {'id': 'g/att/3', 'name': 'photo.png', 'type': 'image/png',
             'head': {'state': 'done', 'text': '', 'calendar_summary': None}},
            {'id': 'g/att/4', 'name': 'waiting.docx', 'type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
             'head': {'state': 'queued', 'text': '', 'calendar_summary': None}},
        ]
        message = doc('g', 'alice@example.com', [('body', 'See attached.')],
                      source='gmail', attachments=attachments)
        qmd = FakeQmd({'gmail': [{'id': 'g', 'time': '2026-10-28T10:00:00Z'}]},
                      {'g': message}, {'g': 'Attached files (new wording):\n---\nSee attached.'})
        selected = sel.select(qmd, '2026-10-28T00:00:00Z', '2026-10-29T00:00:00Z')
        self.assertEqual(selected['mail_entries'][0]['attachments'], attachments)
        view = selected['views']['mail.md']
        self.assertIn('g/att/1 (totals, final (2).pdf, application/pdf, done)\n' + heads, view)
        self.assertIn('g/att/2 (invite.ics, text/calendar, unmaterialised)\n' + calendar, view)
        self.assertIn('g/att/3 (photo.png, image/png, done)', view)
        self.assertIn('g/att/4 (waiting.docx,', view)
        self.assertEqual(sum(c[0] == 'get' for c in qmd.calls), 1)
        self.assertEqual(next(c for c in qmd.calls if c[0] == 'get'), ('get', 'g', *sel.GET_ARGS))


class MailInvolvement(unittest.TestCase):
    def test_alias_mentions_exclude_quoted_history_and_attachments(self):
        cases = [
            ({}, [('body', 'Application attached.')], False),
            ({}, [('body', 'Please ask Pat.')], True),
            ({}, [('body', 'Application.\nOn Tue wrote:\nPat asked.')], False),
            ({}, [('body', 'Application.\n> Pat asked.')], False),
            ({}, [('body', 'Application.'), ('quoted', 'Pat asked.')], False),
            ({}, [('body', 'Application.'), ('attachment', 'Pat asked.')], False),
            ({'title': 'For Pat'}, [('body', 'Application.')], True),
            ({'cc': ['colleague@example.com', sel.owner_email().upper()]}, [('body', 'Application.')], True),
        ]
        for fields, parts, expected in cases:
            with self.subTest(fields=fields, parts=parts):
                message = doc('g', 'person@organisation.test', parts,
                              to='recruitment@organisation.test', **fields)
                kept, dropped = sel.mail_entries(
                    [{'id': 'g', 'time': '2026-10-01T09:00:00Z'}], {'g': message}
                )
                self.assertEqual(bool(kept), expected)
                if not expected:
                    self.assertEqual(dropped[0]['why'], 'drop:not-addressed')

    def test_fathom_and_docs_keep_paths(self):
        for sender in ('recap@fathom.video', 'comments-noreply@docs.google.com'):
            with self.subTest(sender=sender):
                self.assertEqual(sel.classify(sender, sel.owner_email()), (True, 'keep:sender'))
                self.assertEqual(sel.classify(sender, 'alias@example.com', 'For Pat'),
                                 (True, 'keep:sender'))
                self.assertEqual(sel.classify(sender, 'alias@example.com'),
                                 (False, 'drop:not-addressed'))


class Mail(unittest.TestCase):
    def setUp(self):
        self.sources, self.docs, self.rendered = mail_fixture()
        self.out = sel.select(
            FakeQmd(self.sources, self.docs, self.rendered),
            '2026-10-01T00:00:00Z',
            '2026-10-02T00:00:00Z',
        )

    def test_mail_selection_chain(self):
        with self.subTest(check='test_rules_and_counts'):
            m = self.out['manifest']['mail']
            self.assertIn('addressed_or_mentioned', self.out['manifest']['rules'])
            self.assertEqual(m['kept_by_reason'], {'keep:person': 2, 'keep:sender': 2})
            self.assertEqual(
                m['dropped_by_reason'],
                {'drop:bulk-local': 1, 'drop:empty-body': 1},
            )
            self.assertEqual(m['threads'], 3)
            self.assertEqual([d['ref'] for d in m['dropped_list']], ['g2', 'g3'])

        with self.subTest(check='test_fathom_recap_kept_even_near_a_meeting_end'):
            self.assertEqual([d['id'] for d in self.out['kept_mail']], ['g1', 'g6', 'g4', 'g5'])

        with self.subTest(check='test_kept_docs_are_qmd_docs_untouched'):
            self.assertIs(self.out['docs']['g1'], self.docs['g1'])
            self.assertIn(
                'https://track.mailer.example/click/', self.out['docs']['g1']['parts'][0]['text']
            )

        with self.subTest(check='test_view_is_sparse_and_collapses_tracking_urls'):
            view = self.out['views']['mail.md']
            self.assertIn('# thread: Plan · 2 selected', view)
            self.assertIn(
                '## g1 · 2026-10-01 09:00Z · Alice Example\nHello there. Read <track.mailer.example/…>',
                view,
            )
            self.assertNotIn('old quoted text', view)
            self.assertNotIn('old text', view)

        with self.subTest(check='test_long_body_cut_and_structured_attachment_head_retained'):
            lines = self.out['views']['mail.md'].split('\n')
            g6 = lines[lines.index(next(l for l in lines if l.startswith('## g6'))) + 1]
            self.assertLessEqual(len(g6), sel.MAIL_CHARS + 1)
            self.assertTrue(g6.endswith('…'))
            self.assertIn(
                '[attachments]\natt1 (q3.pdf, application/pdf, ready)',
                self.out['views']['mail.md'],
            )
            self.assertIn('Q3 totals: 42', self.out['views']['mail.md'])

        with self.subTest(check='test_refs_map_kept_mail_to_itself'):
            self.assertEqual(
                {k: v for k, v in self.out['refs'].items() if k.startswith('g')},
                {'g1': 'g1', 'g6': 'g6', 'g4': 'g4', 'g5': 'g5'},
            )
            self.assertNotIn('g2', self.out['refs'])


class Meetings(unittest.TestCase):
    def test_summary_only_and_extras_labelled(self):
        sources, docs, rendered = mail_fixture()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'minutes.md'
            path.write_text(
                '2026-10-01 11:00 · Design review\nAgreed scope.\n# Transcript\nraw lines\n'
            )
            out = sel.select(
                FakeQmd(sources, docs, rendered),
                '2026-10-01T00:00:00Z',
                '2026-10-02T00:00:00Z',
                extras=[path],
            )
            ref = 'file:' + hashlib.sha256(path.read_bytes()).hexdigest()
        view = out['views']['meetings.md']
        self.assertIn('Summary: kickoff agreed on milestones.', view)
        self.assertNotIn('speaker lines', view)
        self.assertIn('https://fathom.example/share/abc', view)
        self.assertIn(
            f'## {ref}\n2026-10-01 11:00 · Design review · written minutes (not a recording)\n\nAgreed scope.',
            view,
        )
        self.assertNotIn('raw lines', view)
        self.assertEqual(out['refs'][ref], ref)
        self.assertEqual(out['refs']['f1'], 'f1')
        self.assertEqual(out['manifest']['meetings']['extras'], [ref])
        self.assertEqual([d['id'] for d in out['meetings']], ['f1', ref])
        extra = out['docs'][ref]
        self.assertEqual(
            (extra['source'], extra['title'], extra['time']),
            ('file', '2026-10-01 11:00 · Design review', '2026-10-02T00:00:00Z'),
        )
        self.assertEqual(
            extra['parts'],
            [
                {
                    'kind': 'authored',
                    'text': '2026-10-01 11:00 · Design review\nAgreed scope.\n# Transcript\nraw lines\n',
                }
            ],
        )


# (id, chat, time, sender, parts, (attachment id, name, MIME) or None, rendered header variant)
WA_ROWS = [
    (
        'w1',
        'c1@g.us',
        '2026-10-01T09:00:00Z',
        '12025550101:3@s.whatsapp.net',
        [('body', 'hi')],
        None,
        None,
    ),
    (
        'w2',
        'c1@g.us',
        '2026-10-01T09:01:00Z',
        'dan@s.whatsapp.net',
        [('body', '')],
        ('a1', 'a.jpg', 'image/jpeg'),
        'doc',
    ),
    (
        'w3',
        'c1@g.us',
        '2026-10-01T09:02:00Z',
        'dan@s.whatsapp.net',
        [('body', '')],
        ('a2', 'b.jpg', 'image/jpeg'),
        'doc',
    ),
    (
        'w4',
        'c1@g.us',
        '2026-10-01T09:03:00Z',
        'dan@s.whatsapp.net',
        [('body', '')],
        ('a3', 'c.jpg', 'image/jpeg'),
        'doc',
    ),
    (
        'w5',
        'c1@g.us',
        '2026-10-01T09:04:00Z',
        'dan@s.whatsapp.net',
        [('body', '')],
        None,
        None,
    ),  # reaction: undecodable
    (
        'w6',
        'c1@g.us',
        '2026-10-01T09:05:00Z',
        'dan@s.whatsapp.net',
        [('body', '[binary] 3 KB')],
        None,
        None,
    ),  # undecodable
    (
        'w7',
        'c1@g.us',
        '2026-10-01T09:06:00Z',
        'dan@s.whatsapp.net',
        [('body', 'x' * 200)],
        None,
        None,
    ),
    (
        'w8',
        'carol-jid@s.whatsapp.net',
        '2026-10-02T08:00:00Z',
        'Carol Example',
        [('body', 'see you')],
        None,
        None,
    ),
    (
        'w9',
        'carol-jid@s.whatsapp.net',
        '2026-10-02T08:05:00Z',
        'Carol Example',
        [('body', '[binary] audio')],
        ('a4', 'v.ogg', 'audio/ogg'),
        'rendered',
    ),
]


def wa_fixture():
    rows, docs, rendered = [], {}, {}
    for id_, thread, at, frm, parts, media, via in WA_ROWS:
        rows.append(
            {'id': id_, 'time': at, 'context': 'Family Group' if thread == 'c1@g.us' else ''}
        )
        listed = (
            [{'id': media[0], 'name': media[1], 'type': media[2]}] if media else []
        )
        docs[id_] = doc(id_, frm, parts, thread=thread, attachments=listed)
        rendered[id_] = render(attachments=[media] if media and via == 'rendered' else []).replace('Attachments:', 'Attached media:')
    return {'whatsapp': rows}, docs, rendered


class WhatsApp(unittest.TestCase):
    def setUp(self):
        self.out = sel.select(
            FakeQmd(*wa_fixture()), '2026-10-01T00:00:00Z', '2026-10-03T00:00:00Z'
        )
        self.view = self.out['views']['whatsapp.md']
        self.by_ref = {k: v for k, v in self.out['refs'].items() if k.startswith('wa#')}

    def test_whatsapp_selection_chain(self):
        with self.subTest(check='test_manifest'):
            m = self.out['manifest']['whatsapp']
            self.assertEqual(
                (
                    m['source_items'],
                    m['kept'],
                    m['dropped_undecodable'],
                    m['chats'],
                    m['media_only'],
                    m['lines'],
                ),
                (9, 7, 2, 2, 4, 5),
            )

        with self.subTest(check='test_lines_name_chat_and_owner_and_refs'):
            self.assertIn('# chat (group): Family Group · c1@g.us · 5 messages', self.view)
            self.assertIn('[09:00 wa#0001 · Family Group] Pat: hi', self.view)
            self.assertEqual(
                self.by_ref,
                {
                    'wa#0001': 'w1',
                    'wa#0002': 'w2',
                    'wa#0003': 'w7',
                    'wa#0004': 'w8',
                    'wa#0005': 'w9',
                },
            )

        with self.subTest(check='test_media_run_collapses_and_drops_extra_refs'):
            self.assertIn('[09:01 wa#0002 · Family Group] (unknown sender): [image ×3]', self.view)
            self.assertNotIn('w3', self.out['refs'].values())
            self.assertNotIn('w4', self.out['refs'].values())

        with self.subTest(check='test_text_cut_and_voice_marker_from_binary_with_attachment'):
            self.assertIn('x' * 160 + '…', self.view)
            self.assertIn('[08:05 wa#0005 · Carol Example] Carol Example: [voice note]', self.view)

        with self.subTest(check='test_direct_chat_named_by_contact'):
            self.assertIn(
                '# chat (direct chat with): Carol Example · carol-jid@s.whatsapp.net · 2 messages',
                self.view,
            )

        with self.subTest(check='test_undecodable_rows_are_out_of_views_and_refs'):
            self.assertNotIn('[binary]', self.view)
            self.assertNotIn('w5', self.out['refs'].values())
            self.assertNotIn('w6', self.out['refs'].values())
            self.assertEqual(len(self.out['refs']), 5)

        with self.subTest(check='test_people_directory_counts_and_note'):
            people = self.out['views']['people.md']
            self.assertIn('# People directory', people)
            self.assertIn('Pat (1): Family Group', people)
            self.assertIn('(unknown sender) (4): Family Group', people)
            self.assertIn('Carol Example (2): Carol Example', people)
            self.assertIn(sel.PEOPLE_NOTE, people)


class Batching(unittest.TestCase):
    def test_feed_pages_and_get_batches_of_forty(self):
        rows = [{'id': f'g{i}', 'time': f'2026-10-01T09:{i:02d}:00Z'} for i in range(41)]
        docs = {
            r['id']: doc(
                r['id'],
                f'p{r["id"]}@example.com',
                [('body', f'msg {r["id"]}')],
                title='t',
                thread=r['id'],
            )
            for r in rows
        }
        rendered = {k: render() for k in docs}
        fake = FakeQmd({'gmail': rows}, docs, rendered)
        out = sel.select(fake, '2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z')
        gets = [c for c in fake.calls if c[0] == 'get']
        self.assertEqual([c.index('--no-quoted') - 1 for c in gets], [40, 1])
        self.assertEqual(gets[0][-4:], ('--no-quoted', '--attachments', 'head:6', '--json'))
        self.assertTrue(any('--after' in c for c in fake.calls if c[0] == 'feed'))
        self.assertEqual(len(out['kept_mail']), 41)
        self.assertEqual(out['manifest']['mail']['kept_by_reason'], {'keep:person': 41})


if __name__ == '__main__':
    unittest.main()
