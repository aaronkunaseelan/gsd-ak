"""Vendored consumer contracts must match the installed producers when available."""

from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

VENDOR = Path(__file__).resolve().parents[1] / 'contracts/vendor'


class Contracts(unittest.TestCase):
    def compare_installed(self, producer, identity):
        command = shutil.which(producer)
        if command is None:
            self.skipTest(producer + ' command not installed')
        help_result = subprocess.run(
            [command, '--help'], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(help_result.returncode, 0, producer + ' --help failed')
        commands = re.findall(r'\{([^}]+)\}', help_result.stdout)
        if not any('contract' in group.split(',') for group in commands):
            self.skipTest(producer + ' contract command not installed')
        result = subprocess.run(
            [command, 'contract', identity], capture_output=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, producer + ' contract failed')
        self.assertEqual(result.stdout, (VENDOR / (identity + '.json')).read_bytes())

    def test_absent_install_skips_cleanly(self):
        with patch.object(shutil, 'which', return_value=None):
            with self.assertRaises(unittest.SkipTest):
                self.compare_installed('missing-producer', 'missing-contract')

    def test_installed_qmd_contract_matches_vendor(self):
        self.compare_installed('qmd', 'qmd.status.v1')

    def test_installed_steward_contract_matches_vendor(self):
        self.compare_installed('steward', 'steward.record.v1')


if __name__ == '__main__':
    unittest.main()
