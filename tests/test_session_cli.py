from __future__ import annotations
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from eufy_cli import local_config
from eufy_cli.cli import main
from tests.test_session_transfer import profile


class SessionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'state' / 'local.json'
        self.file = self.root / 'export.json'

    def call(self, argv):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.config))
            stack.enter_context(patch('eufy_cli.cache.AppCache.load', side_effect=AssertionError('No app access')))
            stack.enter_context(patch('eufy_cli.cli.discover', side_effect=AssertionError('No discovery')))
            stack.enter_context(patch('eufy_cli.cli.run_backend', side_effect=AssertionError('No device access')))
            stack.enter_context(redirect_stdout(output))
            code = main(["--json", *argv])
        return code, output.getvalue()

    def test_cli_roundtrip_is_offline_and_output_contains_no_profile_values(self):
        source = profile(role='guest')
        local_config._save(source, self.config)
        original = self.config.read_bytes()
        code, output = self.call(['session', 'export', str(self.file)])
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result['contents'], 'standalone_profile')
        self.assertFalse(result['network_used'])
        self.assertFalse(result['login_performed'])
        self.assertFalse(result['encrypted'])
        for private in ('synthetic-homebase', 'synthetic-doorbell', 'synthetic-local-did', 'synthetic-user-id'):
            self.assertNotIn(private, output)
        self.assertEqual(self.config.read_bytes(), original)
        self.config.unlink()
        code, output = self.call(['session', 'import', str(self.file)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.config.read_text()), source)
        self.assertTrue(json.loads(output)['ok'])

    def test_cli_explicit_overwrite_and_replace_options(self):
        local_config._save(profile(), self.config)
        self.assertEqual(self.call(['session', 'export', str(self.file)])[0], 0)
        self.assertEqual(self.call(['session', 'export', str(self.file)])[0], 2)
        self.assertEqual(self.call(['session', 'export', str(self.file), '--overwrite'])[0], 0)
        old = self.config.read_bytes()
        code, output = self.call(['session', 'import', str(self.file)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)['error']['code'], 'session_transfer_failed')
        self.assertEqual(self.config.read_bytes(), old)
        local_config._save(profile(role='unknown'), self.config)
        self.assertEqual(self.call(['session', 'import', str(self.file), '--replace'])[0], 0)
        self.assertEqual(json.loads(self.config.read_text())['role'], 'guest')

    def test_cli_invalid_import_does_not_echo_file_contents_or_mutate_profile(self):
        local_config._save(profile(), self.config)
        before = self.config.read_bytes()
        self.file.write_text('sensitive-invalid-body')
        code, output = self.call(['session', 'import', str(self.file), '--replace'])
        self.assertEqual(code, 2)
        self.assertNotIn('sensitive-invalid-body', output)
        self.assertEqual(self.config.read_bytes(), before)

    def test_session_help_is_available_without_configuration(self):
        for argv in [['help', 'session'], ['session', 'export', '--help'], ['session', 'import', '--help']]:
            with self.subTest(argv=argv):
                code, output = self.call(argv)
                self.assertEqual(code, 0)
                self.assertIn('usage: eufy session', output)
