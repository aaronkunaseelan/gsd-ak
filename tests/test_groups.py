"""Synthetic table tests for D17/D18's shared window/re-statement planner."""

from collections import Counter
import unittest

from gsd.groups import assign
from gsd import packets


def graph(n=4):
    return {
        'projects': [{'id': f'p{i}', 'name': f'Project {i}'} for i in range(n)],
        'items': [{'id': f'i{i}', 'project_id': f'p{i}'} for i in range(n)],
        'people': [{'id': 'person'}],
        'meetings': [{'id': 'meeting'}],
        'record_links': [],
    }


def change(cid, *ids, deltas=()):
    return {'change_id': cid, 'records': [{'id': i} for i in ids], 'deltas': list(deltas)}


def changes(*counts):
    return [change(f'a{p}-{j}', f'i{p}') for p, n in enumerate(counts) for j in range(n)]


def scopes(plan):
    return [s for ss in plan['scopes'].values() for s in ss]


def link(a, b, kind='relates_to'):
    return {'from_record_id': a, 'to_record_id': b, 'relation_kind': kind}


class Groups(unittest.TestCase):
    def test_scope_precedence_majority_and_fallback(self):
        state = graph()
        state['items'].append({'id': 'extra', 'project_id': 'p1'})
        cs = [
            change('a1', 'i0', 'i1', 'extra', 'person', 'meeting'),
            change('a2', 'p0'), change('a3', 'i0'),
            change('a4', 'meeting', 'person'), change('a5', 'person'),
            change('a6'), change('a7', 'unknown'), change('a8', 'person', 'unknown'),
            change('b-r1-1', 'p3'),
        ]
        cs[6]['citations'] = [{'evidence_qmd_event_id': 'synthetic'}]
        plan = assign(state, cs, {'units': [{'tags': [{'project_id': 'p3'}], 'member_refs': ['synthetic']}]})
        ss = scopes(plan)
        self.assertEqual([s['kind'] for s in ss], ['projects', 'projects', 'meetings', 'people'])
        self.assertEqual(ss[0]['projects'], ['p0'])
        self.assertEqual(ss[0]['changes'], ['a2', 'a3', 'a6', 'a7', 'a8'])
        self.assertEqual(ss[1]['projects'], ['p1'])
        self.assertEqual(ss[1]['changes'], ['a1'])
        self.assertEqual(ss[2]['changes'], ['a4'])
        self.assertEqual(ss[3]['changes'], ['a5'])
        self.assertEqual(Counter(plan['assignments'].keys()), Counter(f'a{i}' for i in range(1, 9)))

    def test_new_deleted_and_moved_records(self):
        state = graph(2)
        cs = [change('new', deltas=[{'table': 'projects', 'after': {'id': 'p-new'}}]),
              change('gone', deltas=[{'table': 'items', 'before': {'id': 'gone', 'project_id': 'p1'}, 'after': {'id': 'gone', 'project_id': 'p1', 'op': 'delete'}}]),
              change('move', 'i0', deltas=[{'table': 'items', 'before': {'id': 'i0', 'project_id': 'p0'}, 'after': {'id': 'i0', 'project_id': 'p1'}}]),
              change('person', deltas=[{'table': 'people', 'after': {'id': 'new-person'}}])]
        ss = scopes(assign(state, cs))
        self.assertEqual(ss[0]['projects'], ['p1'])
        self.assertEqual(ss[0]['changes'], ['gone', 'move'])
        self.assertEqual(ss[1]['projects'], ['p-new'])
        self.assertEqual(ss[2]['kind'], 'people')

    def test_no_changes_no_empty_project_scopes(self):
        self.assertEqual(assign(graph(), [])['groups'], {})
        plan = assign(graph(), [], meetings=True)
        self.assertEqual(plan['groups'], {'r1': []})
        self.assertEqual(scopes(plan), [{'kind': 'meetings', 'projects': [], 'changes': []}])
        self.assertEqual(assign(graph(0), [change('a')])['groups'], {'r1': ['a']})
        self.assertEqual(scopes(assign(graph(0), [change('a')]))[0]['projects'], [])

    def test_packing_zero_fourteen_sixtyone_159(self):
        for counts, expected in [
            ((), []), ((8, 6), [14]), ((31, 30), [31, 30]),
            ((55, 50, 40, 14), [55, 90, 14]),
        ]:
            with self.subTest(counts=counts):
                cs = changes(*counts)
                plan = assign(graph(), cs)
                self.assertEqual([len(ids) for ids in plan['groups'].values()], expected)
                self.assertEqual(list(plan['groups']), [f'r{i+1}' for i in range(len(expected))])
                self.assertEqual(Counter(cid for ids in plan['groups'].values() for cid in ids), Counter(c['change_id'] for c in cs))
                for scope in scopes(plan):
                    self.assertEqual(len({plan['assignments'][cid] for cid in scope['changes']}), 1)
        # Scope order is never disturbed to hit a count or to shard a large project.
        self.assertEqual([len(ids) for ids in assign(graph(), changes(159))['groups'].values()], [159])

    def test_meetings_then_people_and_empty_meetings_packed(self):
        cs = changes(31, 29) + [change('meeting', 'meeting'), change('person', 'person')]
        plan = assign(graph(), cs)
        self.assertEqual([s['kind'] for s in scopes(plan)], ['projects', 'projects', 'meetings', 'people'])
        self.assertEqual([len(v) for v in plan['groups'].values()], [31, 31])
        plan = assign(graph(), changes(14), meetings=True)
        self.assertEqual(list(plan['groups']), ['r1'])
        self.assertEqual(scopes(plan)[-1]['kind'], 'meetings')
        self.assertFalse(scopes(plan)[-1]['changes'])

    def test_restate_always_has_people_scope(self):
        plan = assign(graph(), changes(14), people=True)
        self.assertEqual([s['kind'] for s in scopes(plan)], ['projects', 'people'])
        self.assertEqual(list(plan['groups']), ['r1'])
        self.assertFalse(scopes(plan)[-1]['changes'])

    def test_related_pair_shared_unit_and_cap(self):
        data = {'units': [{'tags': [{'project_id': 'p0'}, {'project_id': 'p1'}]}]}
        for counts, expected in [((30, 30, 1), [['p0', 'p1'], ['p2']]),
                                 ((31, 30, 1), [['p0'], ['p1'], ['p2']])]:
            plan = assign(graph(), changes(*counts), data)
            self.assertEqual([s['projects'] for s in scopes(plan)], expected)
            self.assertTrue(all(len(s['changes']) <= 60 for s in scopes(plan)))

    def test_fallback_counts_toward_merge_capacity(self):
        state = graph(2)
        state['record_links'] = [link('p0', 'p1')]
        cs = changes(29, 30) + [change('fallback1'), change('fallback2')]
        ss = scopes(assign(state, cs))
        self.assertEqual([s['projects'] for s in ss], [['p1'], ['p0']])
        self.assertEqual([len(s['changes']) for s in ss], [32, 29])

    def test_distance_descending_all_link_kinds_and_transitive_merge(self):
        state = graph(3)
        state['record_links'] = [link('p0', 'p1'), link('i0', 'i1', 'blocks'),
                                 link('i1', 'p0', 'depends_on')]
        # Three different links (6) outrank five shared units (5).
        data = {'units': [{'tags': [{'project_id': 'p1'}, {'project_id': 'p2'}]} for _ in range(5)]}
        plan = assign(state, changes(25, 25, 20), data)
        self.assertEqual(scopes(plan)[0]['projects'], ['p0', 'p1'])
        self.assertEqual(scopes(plan)[1]['projects'], ['p2'])
        # Re-statement has no tags: links alone count, including project/item links.
        state['record_links'].append(link('p1', 'i2', 'mentions'))
        plan = assign(state, changes(20, 20, 20))
        self.assertEqual(scopes(plan)[0]['projects'], ['p0', 'p1', 'p2'])
        self.assertEqual(len(scopes(plan)[0]['changes']), 60)

    def test_unrelated_projects_never_merge_or_use_people_similarity(self):
        state = graph(6)
        for p in state['projects']:
            p['owner_person_id'] = 'person'
        plan = assign(state, changes(1, 1, 1, 1, 1, 1))
        self.assertEqual(len(scopes(plan)), 6)
        self.assertEqual(len(plan['groups']), 1)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            assign(state, [change('a'), change('a')])

    def test_role_goals_invariants_generated_scopes_and_relay(self):
        state = graph()
        cs = changes(31, 28) + [change('m', 'meeting'), change('p', 'person')]
        plan = assign(state, cs)
        names = {p['id']: p['name'] for p in state['projects']}
        role = packets.review_scope(plan, 'r2', names)
        self.assertIn('Project 1: these projects and their items agree with the evidence', role)
        self.assertIn("each of the window's meetings", role)
        self.assertIn('including what earlier reviewers wrote', role)
        self.assertIn('Before: r1 (Project 0)', role)
        self.assertIn('After: none', role)
        self.assertEqual(len(role.splitlines()), 5)
        first = packets.review_scope(plan, 'r1', names)
        self.assertIn('After: r2 (Project 1; meetings; people)', first)
        text, _ = packets.packet(__import__('pathlib').Path('/tmp'), 'graph', 'window', 'slice', 'r2', scope=role)
        self.assertIn('b-r2-N', text)
        self.assertIn('grp=r2', text)
        self.assertIn('notes-r2.md', text)
        self.assertIn('Principles, with reasons; cite them by name', text)
        self.assertIn('not their why', text)
        self.assertIn('same-op closure and assignment quotes', ' '.join(text.split()))
        self.assertIn('Before: r1', text)
        self.assertLess(len(packets.REVIEW_ROLE), 1200)
        self.assertNotIn('FIRST check', packets.REVIEW_ROLE)


if __name__ == '__main__':
    unittest.main()
