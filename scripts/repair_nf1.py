#!/usr/bin/env python3
"""NF1 one-off repairs. Default is remote-read-only; never publishes mappings.

--apply [--page ID] removes bottom duplicates, then calls ordinary update_body.
--newline-payloads DIR only prepares one validated-write payload per record.
Keep payloads in machine-local workbench/tmp: they contain private graph text.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db, notion, read


def duplicate_plan(body, sections):
    plans = []
    for heading, entries in notion.section_ranges(body).items():
        if len(entries) < 2:
            continue
        if heading not in ('Summary', 'Item Notes') or heading not in sections or len(entries) != 2:
            raise ValueError('unexpected duplicate section: ' + heading)
        top, bottom = entries
        if 'discussion-urls=' in bottom:
            raise ValueError('bottom duplicate has a discussion; owner review needed')
        if body.count(top) != 1 or body.count(bottom) != 1:
            raise ValueError('repair needs unique exact section strings')
        plans.append({'heading': heading, 'top': top, 'bottom': bottom,
                      'value': sections[heading]})
    return plans


def comments_snapshot(client, page, heading):
    """Enumerate page and descendant comments; page-level listing alone omits inline."""
    comments, top, blocks = set(), None, [page]
    while blocks:
        block = blocks.pop()
        comments.update(c['id'] for c in client.paginate('/comments?block_id=' + block))
        if block == page:
            children = list(client.paginate('/blocks/' + block + '/children'))
        else:
            children = []
        pending = list(children)
        while pending:
            child = pending.pop(0)
            blocks.append(child['id'])
            if child.get('has_children'):
                pending.extend(client.paginate('/blocks/' + child['id'] + '/children'))
            if top is None and child['type'] == 'heading_2':
                text = ''.join(v.get('plain_text', v.get('text', {}).get('content', ''))
                               for v in child['heading_2']['rich_text'])
                if heading in notion.section_ranges('## ' + text + '\n'):
                    top = child['id']
    if top is None:
        raise ValueError('top heading block not found')
    anchored = list(client.paginate('/comments?block_id=' + top))
    return {'comment_count': len(comments), 'top_heading_id': top,
            'top_heading_comment_count': len(anchored),
            'top_comment_ids': sorted(c['id'] for c in anchored)}


def snapshot(client, page, plan, body):
    result = comments_snapshot(client, page, plan['heading'])
    line = notion.section_ranges(body)[plan['heading']][0].split('\n', 1)[0]
    result['discussion_anchors'] = re.findall(r'discussion-urls=([\"\'])(.*?)\1', line)
    result['discussion_anchors'] = [value for _, value in result['discussion_anchors']]
    result['comment_anchored_on_top_heading'] = bool(result['discussion_anchors'] or result['top_heading_comment_count'])
    return result


def repair_page(client, page, sections, body, apply=False):
    plans = duplicate_plan(body, sections)
    results = []
    for plan in plans:
        before = snapshot(client, page, plan, body)
        result = {'heading': plan['heading'], 'before': before, 'applied': False}
        if apply:
            warnings = []
            notion.markdown_warnings(client.update_section(page, plan['bottom'], ''), warnings)
            if warnings:
                raise ValueError('duplicate removal warnings: ' + ', '.join(warnings))
            notion.update_body(client, page, {plan['heading']: plan['value']}, errors=warnings)
            if warnings:
                raise ValueError('section rewrite warnings: ' + ', '.join(warnings))
            body = client.body(page)
            after = snapshot(client, page, plan, body)
            result.update(after=after, applied=True)
            print(json.dumps({'page_id': page, **result}), flush=True)
            if (before != after or len(notion.section_ranges(body)[plan['heading']]) != 1):
                raise ValueError('comment/heading snapshot changed; stop and review before continuing')
        results.append(result)
    return results


def fix_newlines(value):
    if isinstance(value, str):
        return value.replace('\\n', '\n')
    if isinstance(value, list):
        return [fix_newlines(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_newlines(v) for k, v in value.items()}
    return value


def newline_payloads(conn):
    state = read.graph(conn)
    result = []
    for kind, table in db.KINDS.items():
        for row in state[table]:
            fields = {}
            for key, value in row.items():
                if not isinstance(value, str) or key in db.META or key == 'id':
                    continue
                structured = key.endswith('_json') or key == 'aliases'
                decoded = json.loads(value) if structured else value
                fixed = fix_newlines(decoded)
                if fixed != decoded:
                    fields[key] = json.dumps(fixed, ensure_ascii=False) if structured else fixed
            if fields:
                result.append((row['id'], {'changes': [{
                    'id': 'NF1-literal-newlines-' + row['id'],
                    'why': 'F-100: replace literal backslash-n with real newlines; no semantic change',
                    'ops': [{'upsert': kind, 'id': row['id'], 'set': fields}]}]}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--page', help='Only inspect/repair this mapped page')
    parser.add_argument('--newline-payloads', type=Path)
    args = parser.parse_args()
    path = args.db.expanduser().resolve()
    with (path.parent / 'gsd.lock').open('a') as lock:
        # Refuse rather than overlap an active steady run.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.newline_payloads:
            if args.apply or args.page:
                parser.error('--newline-payloads cannot be combined with --apply or --page')
            with db.connect(path) as conn:
                payloads = newline_payloads(conn)
            args.newline_payloads.mkdir(parents=True, exist_ok=True, mode=0o700)
            for rid, payload in payloads:
                target = args.newline_payloads / (rid + '.json')
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as out:
                    json.dump(payload, out, indent=2)
                    out.write('\n')
                fields = payload['changes'][0]['ops'][0]['set']
                print(json.dumps({'record_id': rid, 'fields': sorted(fields), 'payload': str(target)}))
            print(json.dumps({'records': len(payloads), 'applied': False}))
            return
        with Path(str(path) + '.notion.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with db.connect(path) as conn:
                rendered = {row['record_id']: row for row in notion.render(conn)}
                mapped = list(conn.execute('SELECT record_id,page_id FROM notion_map'))
            if args.page:
                mapped = [m for m in mapped if notion.identity(m['page_id']) == notion.identity(args.page)]
                if not mapped:
                    raise ValueError('--page must name a mapped page')
            if args.apply:
                notion.gate()
            client = notion.Client(notion.token())
            pages = 0
            for row in mapped:
                if row['record_id'] not in rendered:
                    continue
                body = client.body(row['page_id'])
                results = repair_page(client, row['page_id'], rendered[row['record_id']]['sections'], body, args.apply)
                if results:
                    pages += 1
                    if not args.apply:
                        print(json.dumps({'page_id': row['page_id'], 'record_id': row['record_id'], 'sections': results}), flush=True)
            print(json.dumps({'pages': pages, 'applied': args.apply, 'requests': client.requests()}))


if __name__ == '__main__':
    main()
