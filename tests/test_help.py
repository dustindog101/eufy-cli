from __future__ import annotations

import io
import json
import shlex
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

from eufy_cli.cli import main
from eufy_cli.help_text import EXAMPLES


class OfflineHelpTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target in ('_cache', 'run_backend', 'discover'):
            stack.enter_context(patch('eufy_cli.cli.' + target, side_effect=AssertionError('Help must stay offline')))
        stack.enter_context(patch('eufy_cli.cli.AppCache.load', side_effect=AssertionError('No app access')))
        stack.enter_context(patch('subprocess.run', side_effect=AssertionError('No Node or other subprocess')))

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_root_help_forms_are_equivalent_and_concise(self):
        root = self.call()
        self.assertEqual((root[0], root[2]), (0, ''))
        self.assertLess(len(root[1].splitlines()), 40)
        for args in (('help',), ('--help',), ('-h',)):
            self.assertEqual(self.call(*args), root)

    def test_nested_and_topic_help_match_direct_help_without_configuration(self):
        for path in (('status',), ('motion',), ('events',), ('configure',), ('session',),
                     ('session', 'export'), ('session', 'import'), ('discover',), ('devices',), ('limits',)):
            with self.subTest(path=path):
                direct = self.call(*path, '--help')
                self.assertEqual((direct[0], direct[2]), (0, ''))
                self.assertIn('Examples:', direct[1])
                self.assertEqual(self.call('help', *path), direct)
        self.assertEqual(self.call('help', 'motion', 'off'), self.call('motion', '--help'))
        self.assertEqual(self.call('help', 'events', 'yesterday'), self.call('events', '--help'))

    def test_all_advertised_command_examples_parse_as_offline_help(self):
        for examples in EXAMPLES.values():
            for line in examples.splitlines():
                if not line.startswith('eufy '):
                    continue
                command = shlex.split(line.split('#')[0])[1:]
                with self.subTest(command=command):
                    code, out, err = self.call(*command, '--help')
                    self.assertEqual((code, err), (0, ''))
                    self.assertIn('usage: eufy', out)

    def test_guides_are_offline_and_help_remains_text_when_json_requested(self):
        for guide in ('setup', 'troubleshooting', 'agents'):
            code, out, err = self.call('help', guide)
            self.assertEqual((code, err), (0, ''))
            self.assertTrue(out.startswith('Eufy '))
            self.assertEqual(self.call('--json', 'help', guide), (code, out, err))
        self.assertEqual(self.call('help', 'help'), self.call('help'))

    def test_incomplete_and_unknown_commands_offer_relevant_usage_without_echoing_values(self):
        for args, expected in ((('motion',), 'eufy motion'), (('session',), 'eufy session'),
                               (('session', 'import'), 'eufy session import'),
                               (('configure',), 'eufy configure'),
                               (('motion', 'private-invalid-value'), 'eufy motion'),
                               (('help', 'private-invalid-value'), 'eufy')):
            with self.subTest(args=args):
                code, out, err = self.call(*args)
                self.assertEqual((code, out), (2, ''))
                self.assertIn('usage: ' + expected, err)
                self.assertIn('Run ' + expected + ' --help', err)
                self.assertNotIn('private-invalid-value', err)
                code, out, err = self.call(*args, '--json')
                self.assertEqual((code, err), (2, ''))
                data = json.loads(out)
                self.assertEqual(data['error']['code'], 'invalid_arguments')
                self.assertIn('usage: ' + expected, data['error']['message'])
                self.assertNotIn('private-invalid-value', out)
