"""Capture selected mail documents and shared meetings; skip images."""

import re

SHARE = re.compile(r'https?://fathom\.video/share/[A-Za-z0-9_-]+')


def capture(qmd, selected, cap=300):
    ledger = {
        'page_cap': cap,
        'pages_spent': 0,
        'pages_covered': 0,
        'budget_charged': 0,
        'stopped_at_cap': False,
        'results': [],
        'shared_meetings': 0,
    }
    seen = set()
    for mail in selected['kept_mail']:
        for ref, kind in capture_candidates(mail):
            if ref in seen:
                continue
            seen.add(ref)
            if ref not in selected['docs'] and kind != 'share' and ledger['budget_charged'] >= cap:
                ledger['stopped_at_cap'] = True
                ledger['results'].append({'id': ref, 'state': 'skipped:page-cap'})
                continue
            if ref in selected['docs']:
                # A late arrival already supplied its content under this parent.
                cached, result = True, {'docs': [selected['docs'][ref]]}
            else:
                # Reuse qmd's extraction cache, not a second acquisition queue.
                cached = qmd('get', ref, '--json', missing_ok=True)
                cached = isinstance(cached, dict) and bool(cached.get('docs'))
                result = qmd(
                    'get', ref, '--materialise',
                    *(['--pages', '4'] if kind == 'pdf' else []),
                    '--json', missing_ok=True,
                )
            if not isinstance(result, dict):
                result = {'id': ref, 'state': 'unknown'}
            if '_extracted' in result:
                # QMD's bounded progress vocabulary distinguishes a cached queue
                # result from fresh extraction, even before ingest indexes it.
                cached = not result['_extracted']
            docs = result.get('docs') or []
            covered = sum(d.get('pages', {}).get('covered', 0) for d in docs)
            ledger['pages_covered'] += covered
            spent = 0 if cached else covered
            ledger['pages_spent'] += spent
            # Unknown failed PDF calls reserve four pages conservatively. DOCX can
            # exceed the soft cap by its final document; do not start another.
            ledger['budget_charged'] += (
                0 if cached else max(spent, 4 if kind == 'pdf' else 1 if kind == 'docx' else 0)
            )
            ledger['results'].append(
                {
                    'id': ref,
                    'kind': kind,
                    'state': 'done' if docs else result.get('state', 'unknown'),
                    'reason': result.get('reason'),
                    'pages': covered,
                    'spent': spent,
                    'cached': cached,
                }
            )
            record_captured_docs(docs, kind, selected, ledger)
    selected['manifest']['meetings']['kept'] += ledger['shared_meetings']
    selected['manifest']['capture'] = ledger
    render_heads(selected)
    return ledger


def capture_candidates(mail):
    candidates = []
    for a in mail.get('attachments', []):
        name, mime = a.get('name', '').lower(), a.get('type', '').lower()
        if mime == 'application/pdf' or name.endswith('.pdf'):
            candidates.append((a['id'], 'pdf'))
        elif 'wordprocessingml' in mime or name.endswith('.docx'):
            candidates.append((a['id'], 'docx'))
    text = '\n'.join(p.get('text', '') for p in mail.get('parts', []))
    candidates += [(url, 'share') for url in SHARE.findall(text)]
    return candidates


def record_captured_docs(docs, kind, selected, ledger):
    for d in docs:
        selected['docs'][d['id']] = d
        selected['refs'][d['id']] = d['id']
        if kind == 'share':
            selected['meetings'].append(d)
            summary = re.split(
                r'(?im)^(?:#+\s*)?Transcript\s*:?\s*$',
                '\n'.join(
                    p.get('text', '')
                    for p in d.get('parts', [])
                    if 'transcript' not in p['kind'].lower()
                ),
                maxsplit=1,
            )[0]
            selected['views']['meetings.md'] += (
                f"\n\n## {d['id']} · shared recorded meeting · {d.get('title', '')}\n{summary}"
            )
            ledger['shared_meetings'] += 1


def render_heads(selected):
    # Derive heads once from the documents capture actually selected, not an
    # earlier parent preview or a second qmd read.
    from .selection import render_mail
    from .tags import meeting_summary

    meetings = {d['id'] for d in selected['meetings']}
    parents = {d['id']: d for d in selected['kept_mail']}
    late = [(source, row) for source in ('mail', 'whatsapp', 'meetings')
            for row in selected['manifest'].get(source, {}).get('late_children', []) if row['state'] == 'selected']
    children = {row['ref'] for _, row in late} | {ref for ref in selected['docs'] if '/att/' in ref}
    children.update(a['id'] for d in parents.values() for a in d.get('attachments', []))
    heads = selected['attachment_heads'] = {}
    for ref in selected['docs']:
        if ref not in children:
            continue
        text = meeting_summary(selected['docs'][ref])
        if ref not in meetings:
            text = '\n'.join(text.splitlines()[:6])
        heads[ref] = f'[attachment {ref}]\n{text}'
    for entry in selected.get('mail_entries', []):
        parent = parents[entry['ref']]
        ids = {a['id'] for a in parent.get('attachments', [])}
        entry['heads'] = [head for ref, head in heads.items()
                          if ref in ids or ref.startswith(parent['id'] + '/att/')]
    if 'mail_entries' in selected:
        selected['views']['mail.md'] = render_mail(selected['mail_entries'], selected['refs'])
    for source, row in late:
        if source != 'mail':
            view = 'whatsapp.md' if source == 'whatsapp' else 'meetings.md'
            selected['views'][view] += f'\n\n## Attached to {row["parent"]}\n{heads[row["ref"]]}'
