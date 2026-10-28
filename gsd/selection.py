"""Select QMD work-source arrivals and render sparse review views.

Enumerate each source feed, then fetch bodies in batches through qmd(*args).
Only extras paths are opened locally, as written minutes rather than recordings.
"""

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path

from . import db as store
from .groups import owner_email
from . import owner
from .feed import Feed


def owner_address():
    local, domain = owner.get('personal_email').split('@')
    return re.compile(rf'{re.escape(owner_email())}|{re.escape(local)}(?:\+[^@\s]+)?@{re.escape(domain)}', re.I)


def owner_mention():
    names = '|'.join(re.escape(n) for n in owner.get('name').split())
    return re.compile(rf'\b(?:{names}|{owner_address().pattern})\b', re.I)


QUOTE = re.compile(
    r'^(On [^\n]{0,200}(\n[^\n]{0,200})?wrote:\s*$|^>|-{2,}\s*(?i:original|forwarded) message|From: .+\n(Sent|Date): )',
    re.M,
)
CSS_LINE = re.compile(r'(\{[^{}]*\}.*){2,}')
SUBJECT_PREFIX = re.compile(r'^\s*((re|fwd?|aw|tr|sv|wg|rv)\s*(\[\d+\])?\s*:\s*)+', re.I)
LONG_URL = re.compile(
    r'<?https?://(?!(?:docs|drive)\.google\.com)([^/\s>]+)[^\s>]{40,}>?'
)  # tracking links: domain only
def owner_sender():
    number, domain = owner.get('whatsapp_jid').split('@')
    aliases = '|'.join(re.escape(a) for a in owner.get('whatsapp_aliases'))
    return re.compile(rf'^({aliases}|{re.escape(number)}(:\d+)?@{re.escape(domain)})$')


MEDIA = {
    'image': '[image]',
    'audio': '[voice note]',
    'video': '[video]',
    'application': '[document]',
}
MARKERS = set(MEDIA.values()) | {'[attachment]'}
MAIL_CHARS, WA_CHARS, PERSON_CHATS = 280, 160, 6
BATCH = 40
GET_ARGS = ('--no-quoted', '--attachments', 'head:6', '--json')
OWN_SKIP = {'header', 'quoted', 'attachment', 'binary'}  # parts that are not the author's own text
EXTRA_LABEL = 'written minutes (not a recording)'
PEOPLE_NOTE = (
    'Matching a name here to a graph person is your inference unless an email address or signature settles it. '
    'Look-alike names are often different people, and some names are also places.'
)


def render_whatsapp(chats):
    cites, lines, n, senders = {}, [], 0, {}
    kept_docs = [m['doc'] for msgs in chats.values() for m in msgs]
    for jid, msgs in sorted(chats.items(), key=lambda kv: -len(kv[1])):
        name = _chat_name(jid, msgs)
        kind = 'group' if jid.endswith('@g.us') else 'direct chat with'
        lines.append(f'# chat ({kind}): {name} · {jid} · {len(msgs)} messages')
        # Every line names its chat, so a quote keeps its thread.
        short = re.sub(r'\s+', ' ', name).strip()[:24]
        day, last = None, None
        for m in msgs:
            senders.setdefault(m['who'], Counter())[name] += 1
            if m['at'][:10] != day:
                day, last = m['at'][:10], None
                lines.append(f'## {day}')
            if (
                m['text'] in MARKERS and last and last['key'] == (m['who'], m['text'])
            ):  # a run of media is one line
                last['count'] += 1
                lines[last['line']] = last['head'] + f'{m["text"][:-1]} ×{last["count"]}]'
                continue
            n += 1
            ref = f'wa#{n:04d}'
            cites[ref] = m['id']
            head = f'[{m["at"][11:16]} {ref} · {short}] {m["who"]}: '
            lines.append(head + cut(m['text'], WA_CHARS))
            last = (
                {'key': (m['who'], m['text']), 'count': 1, 'line': len(lines) - 1, 'head': head}
                if m['text'] in MARKERS
                else None
            )
        lines.append('')
    return cites, lines, n, senders, kept_docs


def whatsapp_chats(rows, docs):
    chats, undecodable, media = {}, 0, 0
    for row in rows:
        doc = docs.get(row['id'])
        text = own_text(doc)
        if text.startswith('[binary]'):
            text = ''
        if not text:
            types = [
                a['type'] for a in attachments_of(doc) if a.get('type')
            ]
            # No text or attachment: an undecodable reaction, poll or edit.
            if not types:
                undecodable += 1
                continue
            text = MEDIA.get(types[0].split('/')[0], '[attachment]')
            media += 1
        frm = field(doc, 'from') or '?'
        chats.setdefault(field(doc, 'thread') or '?', []).append(
            {
                'at': row['time'],
                'who': wa_sender(frm),
                'text': text,
                'id': row['id'],
                'context': row.get('context') or '',
                'doc': doc,
            }
        )
    return chats, undecodable, media


def mail_entries(rows, docs):
    kept, dropped = [], []
    for row in rows:
        doc = docs.get(row['id'], {})
        frm = field(doc, 'from')
        text = own_text(doc)
        q = QUOTE.search(text)
        body = text[: q.start()] if q else text
        keep, why = (True, row['why']) if row.get('selected_in') else classify(
            frm, field(doc, 'to') + ', ' + field(doc, 'cc'), field(doc, 'title'), body
        )
        entry = {
            'ref': row['id'],
            'at': row['time'][:16].replace('T', ' '),
            'from': frm,
            'to': field(doc, 'to'),
            'subject': field(doc, 'title'),
            'thread': field(doc, 'thread') or row['id'],
            'why': why,
            'selected_in': row.get('selected_in'),
        }
        if not keep:
            dropped.append(entry)
            continue
        entry['body'] = clean(body)
        if not entry['body']:
            entry['why'] = 'drop:empty-body'
            dropped.append(entry)
            continue
        entry['attachments'] = attachments_of(doc)
        kept.append((entry, doc))
    return kept, dropped


def extra_minutes(extras, until, refs, blocks):
    extra_refs, extra_docs = [], []
    for path in extras:
        raw = Path(path).read_bytes()
        ref = 'file:' + hashlib.sha256(raw).hexdigest()
        if ref in refs:
            continue
        text = raw.decode('utf-8')
        first, _, rest = text.strip().partition('\n')
        extra_refs.append(ref)
        refs[ref] = ref
        # A file doc so reviewer bundles can open the minutes without QMD.
        extra_docs.append(
            {
                'id': ref,
                'source': 'file',
                'title': first,
                'time': until,
                'from': '',
                'to': '',
                'cc': '',
                'thread': '',
                'where': '',
                'link': '',
                'attachments': [],
                'parts': [{'kind': 'authored', 'text': text}],
            }
        )
        blocks.append(
            f'## {ref}\n{first} · {EXTRA_LABEL}\n\n{clean(rest.split("# Transcript")[0])}'
        )
    return extra_refs, extra_docs


def classify(frm, to, subject='', body=''):
    """Existing bulk drops first; then require owner addressing or an own-text mention."""
    addr = parseaddr(frm)[1].lower()
    local, _, domain = addr.partition('@')
    sender = any(addr.endswith(k) or addr == k for k in owner.get('keep_from'))
    if not sender:  # preserve the existing Fathom/Docs and organisation bulk overrides
        if '+newsletter' in to.lower():
            return False, 'drop:newsletter-alias'
        if any(
            domain == d or domain.endswith('.' + d) or (d.endswith('.') and d in domain)
            for d in owner.get('bulk_domains')
        ):
            return False, f'drop:bulk-domain {domain}'
        if re.match(owner.get('bulk_local'), local):
            return False, f'drop:bulk-local {local}'
    addressed = any(owner_address().fullmatch(a) for _, a in getaddresses([frm, to]))
    if not addressed and not owner_mention().search(subject + '\n' + body):
        return False, 'drop:not-addressed'
    return True, 'keep:sender' if sender else 'keep:person'


def clean(text):
    text = '\n'.join(line for line in text.splitlines() if not CSS_LINE.search(line))
    return re.sub(r'\n\s*\n(\s*\n)+', '\n\n', text).strip()


def cut(text, n):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text if len(text) <= n else text[:n].rstrip() + '…'


def when(text):
    return datetime.fromisoformat(store.iso(text))


def attachments_of(doc):
    """QMD's structured attachment refs, including its bounded head projection."""
    return [a for a in doc.get('attachments') or [] if isinstance(a, dict)]


def attachment_text(attachment):
    head = attachment.get('head') or {}
    return head.get('calendar_summary') or head.get('text', '')


def field(doc, key):
    """A header field as text; a list (several addresses) is joined."""
    value = doc.get(key) or ''
    return ', '.join(str(v) for v in value) if isinstance(value, list) else str(value)


def own_text(doc, skip=OWN_SKIP):
    """The author's own text from parts, with CRLF normalised and CSS lines removed."""
    text = '\n'.join(p.get('text', '') for p in doc.get('parts') or [] if p.get('kind') not in skip)
    return clean(text.replace('\r\n', '\n'))


def attachment_heads(doc):
    return [text for a in attachments_of(doc) if (text := attachment_text(a).strip())]


def mail_sender(frm):
    name, addr = parseaddr(frm)
    return name.strip() or addr or frm.strip() or '?'


def wa_sender(frm):
    if owner_sender().match(frm):
        return owner.get('name').split()[0]
    return '(unknown sender)' if '@' in frm else frm


def materialised_parent(row):
    # /att/ belongs to the primary message, including nested /ocr or /transcript.
    # The search-result item may itself be an attachment rather than the message.
    if '/att/' in row['id']:
        return row['id'].split('/att/')[0]
    item = row.get('item', row['id'])
    return item if item != row['id'] else None


def source_rows(scanned, kind):
    # Primary messages first. Derived arrivals are joined to their selected
    # parent in late_inputs, not mistaken for independent messages.
    unique = {r['id']: r for r in scanned if not materialised_parent(r) and r.get('kind', kind) == kind}
    return sorted(unique.values(), key=lambda r: r['time'])


def source_counts(scanned, rows, kind):
    excluded = sum(bool(materialised_parent(r)) or r.get('kind', kind) != kind for r in scanned)
    return {
        'excluded_materialised': excluded,
        'dropped_duplicate_ids': len(scanned) - excluded - len(rows),
    }


def fetch(qmd, ids, docs):
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), BATCH):
        res = qmd('get', *ids[i : i + BATCH], *GET_ARGS)
        for item in [res] if isinstance(res, dict) else res:
            for doc in item.get('docs') or []:
                docs[doc['id']] = doc
    missing = [ref for ref in ids if not docs.get(ref)]
    if missing:
        raise ValueError('missing primary document: ' + ', '.join(missing))


def meetings(qmd, feed, until, docs, extras):
    scanned = feed.rows('fathom')
    rows = source_rows(scanned, 'meeting')
    fetch(qmd, [r['id'] for r in rows], docs)
    kept, blocks, refs = [], [], {}
    for row in rows:
        doc = docs.get(row['id'])
        summary = clean(own_text(doc, OWN_SKIP | {'transcript'}).split('# Transcript')[0])
        where = field(doc, 'where') or row['time']
        blocks.append(
            f'## {row["id"]}\n{where} · {field(doc, "title")} · {field(doc, "link")}\n\n{summary}'
        )
        kept.append(doc)
        refs[row['id']] = row['id']
    extra_refs, extra_docs = extra_minutes(extras, until, refs, blocks)
    manifest = {
        'source_items': len(rows) + len(extra_refs),
        **source_counts(scanned, rows, 'meeting'),
        **feed.counts['fathom'],
        'kept': len(kept) + len(extra_refs),
        'extras': extra_refs,
        'note': 'Fathom meetings, summary only (transcripts left out); extras are written minutes, not recordings',
    }
    return {
        'docs': kept + extra_docs,
        'refs': refs,
        'view': '\n\n'.join(blocks),
        'manifest': manifest,
    }


def mail(qmd, feed, docs):
    scanned = feed.rows('gmail')
    rows = source_rows(scanned, 'email')
    fetch(qmd, [r['id'] for r in rows], docs)
    kept, dropped = mail_entries(rows, docs)
    entries = [entry for entry, _ in kept]
    manifest = {
        'source_items': len(rows),
        **source_counts(scanned, rows, 'email'),
        **feed.counts['gmail'],
        'kept': len(kept),
        'dropped': len(dropped),
        'threads': len({e['thread'] for e in entries}),
        'kept_by_reason': _count(entries),
        'dropped_by_reason': _count(dropped),
        'dropped_list': [
            {k: e[k] for k in ('ref', 'at', 'from', 'subject', 'why')} for e in dropped
        ],
    }
    return {
        'docs': [doc for _, doc in kept],
        'refs': {e['ref']: e['ref'] for e in entries},
        'entries': entries,
        'view': render_mail(entries),
        'manifest': manifest,
    }


def render_mail(entries, refs=None):
    threads = {}
    for entry in entries:
        threads.setdefault(entry['thread'], []).append(entry)
    view = []
    for thread in sorted(threads.values(), key=lambda t: t[0]['at']):
        subject = SUBJECT_PREFIX.sub('', thread[0]['subject']).strip() or '(no subject)'
        view.append(f'# thread: {subject} · {len(thread)} selected')
        for entry in thread:
            lines = [
                f'## {entry["ref"]} · {entry["at"]}Z · {mail_sender(entry["from"])}',
                cut(LONG_URL.sub(lambda u: f'<{u.group(1)}/…>', entry['body']), MAIL_CHARS),
            ]
            if entry.get('selected_in'):
                lines.append(f'Parent selection: {entry["why"]} in {entry["selected_in"]}; new attached evidence follows.')
            attachments = []
            reverse = {v: k for k, v in (refs or {}).items() if k.startswith('att#')}
            for attachment in entry['attachments']:
                ref = attachment['id']
                # Derived generic URL refs duplicate links in the body. Shared
                # meetings get their own meeting block; real attachments stay.
                if '/att/link-' in ref:
                    continue
                if refs is not None:
                    short = reverse.get(ref)
                    if not short:
                        short = f'att#{1 + sum(k.startswith("att#") for k in refs):04d}'
                        refs[short] = ref
                        reverse[ref] = short
                    ref = short
                state = (attachment.get('head') or {}).get('state', 'unknown')
                attachments.append(f'{ref} ({attachment.get("name", "")}, {attachment.get("type", "")}, {state})')
                attachments.extend(line.strip() for line in attachment_text(attachment).splitlines())
            if attachments:
                lines += ['[attachments]', *attachments]
            lines += entry.get('heads', [])
            view.append('\n'.join(lines))
    return '\n'.join(view)


def whatsapp(qmd, feed, docs):
    scanned = feed.rows('whatsapp')
    rows = source_rows(scanned, 'message')
    fetch(qmd, [r['id'] for r in rows], docs)
    chats, undecodable, media = whatsapp_chats(rows, docs)
    cites, lines, n, senders, kept_docs = render_whatsapp(chats)
    manifest = {
        'source_items': len(rows),
        **source_counts(scanned, rows, 'message'),
        **feed.counts['whatsapp'],
        'kept': len(rows) - undecodable,
        'dropped_undecodable': undecodable,
        'chats': len(chats),
        'media_only': media,
        'lines': n,
        'note': 'every readable message kept; media bytes expired in QMD, so media-only messages are markers, '
        'and a run of them from one sender on one day is one line',
    }
    return {
        'docs': kept_docs,
        'refs': cites,
        'senders': senders,
        'view': '\n'.join(lines),
        'manifest': manifest,
    }


def _chat_name(jid, msgs):
    if jid.endswith('@g.us'):
        names = [m['context'] for m in msgs if m['context']]
    else:
        names = [m['who'] for m in msgs if m['who'] != owner.get('name').split()[0]]
    return Counter(names).most_common(1)[0][0] if names else jid


def _count(rows):
    return dict(Counter(r['why'].split(' ')[0] for r in rows))


def people(mail_entries, wa_senders):
    out = ['# People directory', '', '## Selected email senders (count)']
    senders = Counter(mail_sender(e['from']) for e in mail_entries)
    out += [f'{name} ({n})' for name, n in senders.most_common()]
    out += ['', '## Selected WhatsApp senders (messages, chats)']
    for name, chats in sorted(wa_senders.items(), key=lambda kv: -sum(kv[1].values())):
        out.append(
            f'{name} ({sum(chats.values())}): '
            + '; '.join(cut(c, 30) for c, _ in chats.most_common(PERSON_CHATS))
        )
    out += ['', PEOPLE_NOTE]
    return '\n'.join(out)


def late_inputs(qmd, feed, docs, packs):
    """Admit unseen child content under its parent's selection provenance."""
    children = []
    for source, rows in feed.arrivals.items():
        for row in rows:
            parent = materialised_parent(row)
            if parent:
                children.append({**row, 'parent': parent})
    proofs = qmd.input_history(children) if children and hasattr(qmd, 'input_history') else {}
    admitted = []
    for row in children:
        source, parent, ref = row['source'], row['parent'], row['id']
        pack = packs[source]
        proof = proofs.get(ref, {})
        outcome = {'ref': ref, 'parent': parent, 'parent_commit': proof.get('parent_commit')}
        pack['manifest'].setdefault('late_children', []).append(outcome)
        if proof.get('included'):
            # Compare the same document view on both sides. Feed versions are
            # opaque strings, not the structured version returned by qmd get.
            fetch(qmd, [ref], docs)
            committed = next((cid for child, cid in proof['included'] if child == docs[ref]), None)
            if committed:
                outcome.update(state='excluded', why='included at parent commit ' + committed)
                continue
        parents = {d['id'] for d in pack['docs']}
        if parent not in parents:
            if proof.get('doc'):
                docs[parent] = proof['doc']
            elif parent not in docs:
                fetch(qmd, [parent], docs)
            doc = docs[parent]
            context_row = {'id': parent, 'time': doc.get('time') or row['time'],
                           'selected_in': proof.get('parent_commit'), 'why': proof.get('why')}
            if source == 'gmail':
                kept, dropped = mail_entries([context_row], docs)
                if not kept:
                    outcome.update(state='excluded', why='parent: ' + dropped[0]['why'])
                    continue
                pack['entries'].append(kept[0][0])
            elif source == 'whatsapp':
                chats, _, _ = whatsapp_chats([context_row], docs)
                if not chats:
                    raise ValueError('late attachment parent is unreadable: ' + parent)
            pack['docs'].append(doc)
            if source != 'whatsapp':
                pack['refs'][parent] = parent
            if source == 'fathom':
                summary = clean(own_text(doc, OWN_SKIP | {'transcript'}).split('# Transcript')[0])
                pack['view'] += f'\n\n## {parent}\n{field(doc, "where")} · {field(doc, "title")}\n{summary}'
            pack['manifest']['context_parents'] = pack['manifest'].get('context_parents', 0) + 1
        # Fetch the child, not just its parent's old preview. This is new evidence
        # even though both its own time and the parent are before the current cut.
        if ref not in docs:
            fetch(qmd, [ref], docs)
        outcome.update(state='selected', why=proof.get('why') or next(
            (e['why'] for e in pack.get('entries', []) if e['ref'] == parent), 'keep:parent'))
        pack['manifest']['excluded_materialised'] -= 1
        admitted.append(docs[ref])
    gmail, wa = packs['gmail'], packs['whatsapp']
    gmail['view'] = render_mail(gmail['entries'])
    gmail['manifest']['kept_list'] = [
        {k: e[k] for k in ('ref', 'why', 'selected_in')} for e in gmail['entries']
    ]
    if wa['manifest'].get('context_parents'):
        times = {r['id']: r['time'] for r in feed.arrivals['whatsapp']}
        rows = [{'id': d['id'], 'time': d.get('time') or times[d['id']]} for d in wa['docs']]
        chats, _, _ = whatsapp_chats(rows, docs)
        cites, lines, _, _, _ = render_whatsapp(chats)
        wa['refs'], wa['view'] = cites, '\n'.join(lines)
    return admitted


def select(qmd, since, until, extras=()):
    """Arrival pack views for one run: see the module docstring for the rules."""
    docs = {}
    feed = Feed(qmd, since, until)
    fathom = meetings(qmd, feed, until, docs, extras)
    gmail = mail(qmd, feed, docs)
    wa = whatsapp(qmd, feed, docs)
    children = late_inputs(qmd, feed, docs, {'gmail': gmail, 'fathom': fathom, 'whatsapp': wa})
    kept = gmail['docs'] + fathom['docs'] + wa['docs'] + children
    manifest = {
        'since': since,
        'until': until,
        'via': 'qmd --json',
        'rules': {
            'keep_from': owner.get('keep_from'),
            'addressed_or_mentioned': 'owner in From/To/Cc, or name/address in subject or own text before quotes',
            'bulk_domains': owner.get('bulk_domains'),
            'bulk_local': owner.get('bulk_local'),
            'caps': {'mail': MAIL_CHARS, 'whatsapp': WA_CHARS},
            'whatsapp': 'all readable kept; media markers from MIME',
            'fathom': 'summary only; recap emails follow the mail rules',
        },
        'mail': gmail['manifest'],
        'whatsapp': wa['manifest'],
        'meetings': fathom['manifest'],
    }
    refs = {**fathom['refs'], **gmail['refs'], **wa['refs'], **{d['id']: d['id'] for d in children}}
    views = {
        'mail.md': gmail['view'],
        'whatsapp.md': wa['view'],
        'meetings.md': fathom['view'],
        'people.md': people(gmail['entries'], wa['senders']),
    }
    return {
        'manifest': manifest,
        'feed': feed.state,
        'refs': refs,
        'views': views,
        'kept_mail': gmail['docs'],
        'mail_entries': gmail['entries'],
        'meetings': fathom['docs'],
        'docs': {doc['id']: doc for doc in kept},
    }
