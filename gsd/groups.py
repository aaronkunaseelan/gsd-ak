"""Deterministic scopes, distance merges (at most 60 changes), then whole-scope packing."""

from collections import Counter
from itertools import combinations
import math

from . import owner


def owner_email():
    return owner.get('email')


def owner_name():
    return owner.get('name')

TYPED = ('projects', 'items', 'people', 'meetings')


def assign(state, changes, data=None, *, meetings=False, people=False):
    changes = [c for c in changes if not c['change_id'].startswith('b-')]
    if len({c['change_id'] for c in changes}) != len(changes):
        raise ValueError('duplicate change_id')
    # Include deleted/new records for routing, but score only current graph links.
    rows = {t: {r['id']: r for r in state.get(t, [])} for t in TYPED}
    for c in changes:
        for d in c.get('deltas', []):
            if d['table'] in rows:
                row = d.get('after') or d.get('before')
                if row:
                    rows[d['table']].setdefault(row['id'], row)
    kind = {rid: t for t in rows for rid in rows[t]}
    homes = {p: p for p in rows['projects']}
    homes.update({i: r.get('project_id') for i, r in rows['items'].items()})
    seeds, other = {}, {'meetings': [], 'people': [], 'fallback': []}
    for c in changes:
        local = dict(homes)
        ids = {r['id'] for r in c.get('records', [])}
        for d in c.get('deltas', []):
            row = d.get('after') or d.get('before')
            if row and d['table'] in TYPED:
                ids.add(row['id'])
                if d['table'] == 'items':
                    local[row['id']] = row.get('project_id')
        hits = Counter(local[r] for r in ids if local.get(r))
        if hits:
            p = min(hits, key=lambda p: (-hits[p], p))
            seeds.setdefault(p, []).append(c['change_id'])
        else:
            kinds = {kind.get(r) for r in ids}
            scope = (
                'meetings'
                if 'meetings' in kinds and kinds <= {'meetings', 'people'}
                else 'people'
                if kinds == {'people'}
                else 'fallback'
            )
            other[scope].append(c['change_id'])

    if other['fallback']:
        p = (
            min(seeds, key=lambda p: (-len(seeds[p]), p))
            if seeds else next(iter(sorted(rows['projects'])), None)
        )
        seeds.setdefault(p, []).extend(other['fallback'])

    weights = Counter(
        pair
        for u in (data or {}).get('units', [])
        for pair in combinations(sorted({t['project_id'] for t in u['tags']}), 2)
    )
    for link in state.get('record_links', []):
        p, q = homes.get(link['from_record_id']), homes.get(link['to_record_id'])
        if p and q and p != q:
            weights[tuple(sorted((p, q)))] += 2
    clusters = {
        p: {'kind': 'projects', 'projects': [p] if p else [], 'changes': ids}
        for p, ids in seeds.items()
    }
    home = {p: p for p in seeds}
    for (p, q), weight in sorted(weights.items(), key=lambda x: (-x[1], x[0])):
        if weight <= 0 or p not in home or q not in home:
            continue
        a, b = home[p], home[q]
        if a == b or len(clusters[a]['changes']) + len(clusters[b]['changes']) > 60:
            continue
        clusters[a]['projects'] += clusters[b]['projects']
        clusters[a]['changes'] += clusters[b]['changes']
        for project in clusters[b]['projects']:
            home[project] = a
        del clusters[b]
    scopes = sorted(clusters.values(), key=lambda s: (-len(s['changes']), sorted(s['projects'])))
    for s in scopes:
        s['projects'].sort()
    for k in ('meetings', 'people'):
        if other[k] or (k == 'meetings' and meetings) or (k == 'people' and people):
            scopes.append({'kind': k, 'projects': [], 'changes': other[k]})

    n = max(1, math.ceil(len(changes) / 60)) if scopes else 0
    share = len(changes) / n if n else 0
    out, packed = {}, {}
    for scope in scopes:
        if not out or (len(out[next(reversed(out))]) >= share and len(out) < n):
            group = f'r{len(out) + 1}'
            out[group], packed[group] = [], []
        out[group] += scope['changes']
        packed[group].append(scope)
    return {
        'assignments': {cid: g for g, ids in out.items() for cid in ids},
        'groups': out,
        'projects': {g: [p for s in ss for p in s['projects']] for g, ss in packed.items()},
        'scopes': packed,
    }
