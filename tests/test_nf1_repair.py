"""One-off NF1 repair safety: synthetic bodies only, no live writes."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('nf1', Path(__file__).resolve().parents[1] / 'scripts/repair_nf1.py')
nf1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nf1)

HEADING = '## <span discussion-urls="https://notion.so/discussion">Summary</span>'
BODY = HEADING + '\nStale\n\n---\n\n## Details\nKeep\n\n---\n\n## Summary\nCurrent'


class Client:
    def __init__(self):
        self.md, self.writes = BODY, []
    def paginate(self, path):
        if path == '/blocks/page/children':
            return [{'id': 'heading', 'type': 'heading_2', 'has_children': False,
                     'heading_2': {'rich_text': [{'plain_text': 'Summary'}]}}]
        if path == '/comments?block_id=heading':
            return [{'id': 'owner-comment'}]
        return []
    def body(self, page):
        return self.md
    def update_section(self, page, old, new):
        self.writes.append((old, new))
        self.md = self.md.replace(old, new)
        return {}


class RepairTests(unittest.TestCase):
    def test_readonly_default_and_single_page_apply_keeps_comment_and_heading(self):
        client = Client()
        result = nf1.repair_page(client, 'page', {'Summary': 'New'}, BODY)
        self.assertEqual(client.writes, [])
        self.assertTrue(result[0]['before']['comment_anchored_on_top_heading'])
        self.assertEqual(result[0]['before']['comment_count'], 1)
        with redirect_stdout(io.StringIO()):
            applied = nf1.repair_page(client, 'page', {'Summary': 'New'}, BODY, True)
        self.assertEqual(applied[0]['before'], applied[0]['after'])
        self.assertEqual(len(client.writes), 2)
        self.assertEqual(client.writes[0], ('## Summary\nCurrent', ''))
        self.assertEqual(client.writes[1][1], HEADING + '\n\nNew')
        self.assertIn('## Details\nKeep', client.md)
        self.assertEqual(len(nf1.notion.section_ranges(client.md)['Summary']), 1)

    def test_refuses_unexpected_or_commented_bottom_duplicate(self):
        for body in (BODY.replace('Summary', 'Unknown'),
                     BODY.replace('## Summary\nCurrent', HEADING + '\nCurrent')):
            with self.assertRaises(ValueError):
                nf1.duplicate_plan(body, {'Summary': 'New'})

    def test_newline_payloads_are_per_record_normal_write_shapes(self):
        graph = {table: [] for table in nf1.db.KINDS.values()}
        graph['items'] = [{'id': 'r1', 'body_markdown': r'one\ntwo'}]
        graph['people'] = [{'id': 'r2', 'metadata_json': json.dumps({'note': 'one\ntwo'})}]
        with patch.object(nf1.read, 'graph', return_value=graph):
            payloads = nf1.newline_payloads(None)
        self.assertEqual(len(payloads), 1)  # Valid JSON-encoded newline is not a literal backslash-n.
        rid, payload = payloads[0]
        self.assertEqual(rid, 'r1')
        op = payload['changes'][0]['ops'][0]
        self.assertEqual(op, {'upsert': 'item', 'id': 'r1', 'set': {'body_markdown': 'one\ntwo'}})
        json.loads(json.dumps(payload))
