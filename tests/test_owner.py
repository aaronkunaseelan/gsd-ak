"""Private settings have one definition and fail only when needed."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsd import db, groups, owner, packets, selection


class OwnerConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owner.json'
        self.patch = patch.dict(os.environ, GSD_OWNER_CONFIG=str(self.path))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def settings(self, **values):
        self.path.write_text(json.dumps(values))
        owner._read.cache_clear()

    def test_missing_file_names_the_needed_key(self):
        with self.assertRaisesRegex(ValueError, 'missing owner config key email'):
            groups.owner_email()

    def test_missing_key_is_not_a_default_owner(self):
        self.settings(email='other@example.test')
        self.assertEqual(groups.owner_email(), 'other@example.test')
        with self.assertRaisesRegex(ValueError, 'missing owner config key timezone'):
            db.owner_time('2026-01-01T00:00Z')

    def test_timezone_comes_from_settings(self):
        self.settings(timezone='UTC')
        self.assertEqual(db.owner_time('2026-01-01T00:00Z', day=False), '00:00 UTC')
        self.assertIs(owner.zone(), owner.zone())

    def test_identity_regexes_escape_values(self):
        self.settings(email='primary@example.test', personal_email='p.at@example.test',
                      name='Pat Synthetic', whatsapp_jid='12025550101@s.whatsapp.net',
                      whatsapp_aliases=['self'])
        self.assertTrue(selection.owner_address().fullmatch('P.AT+tag@example.test'))
        self.assertFalse(selection.owner_address().fullmatch('pxat@example.test'))
        self.assertTrue(selection.owner_mention().search('Ask SYNTHETIC.'))
        self.assertEqual(selection.wa_sender('12025550101:12@s.whatsapp.net'), 'Pat')
        self.assertEqual(selection.wa_sender('self'), 'Pat')

    def test_prompt_uses_all_identity_settings(self):
        self.settings(email='primary@example.test', name='Dana Example', organisation='Example Org')
        text = packets.contract()
        self.assertIn("Dana Example's work and life graph (primary@example.test)", text)
        self.assertIn('not only Example Org', text)
        self.assertIn('Dana is the owner.', text)

    def test_help_does_not_require_owner_configuration(self):
        result = subprocess.run([sys.executable, '-I', str(db.ROOT / 'bin/gsd'), '--help'],
                                capture_output=True, text=True, env=dict(os.environ), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
