from __future__ import annotations

import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from eufy_cli import local_config
from eufy_cli.cli import main
from tests.test_session_transfer import profile


class DailyCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'local.json'
        local_config._save(profile(role='guest'), self.path)
        self.original = self.path.read_bytes()
        self.today = dt.date.today().isoformat()
        self.result = {'scope': 'homebase', 'complete': False, 'source': 'local-p2p',
                       'counts': [{'date': self.today, 'count': 4, 'private': 'omit-this-value'}]}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path))
        self.app = stack.enter_context(patch('eufy_cli.cli.AppCache.load', side_effect=AssertionError('No app fallback')))
        self.discover = stack.enter_context(patch('eufy_cli.cli.discover', side_effect=AssertionError('No discovery')))
        self.backend = stack.enter_context(patch('eufy_cli.events.run_backend', return_value={'ok': True, 'result': self.result}))

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['events', 'counts', *argv])
        return code, out.getvalue(), err.getvalue()

    def test_counts_are_read_only_for_guest_and_keep_homebase_scope_explicit(self):
        code, out, err = self.call()
        self.assertEqual((code, err), (0, ''))
        self.assertIn(self.today, out)
        self.assertNotIn(f'{self.today}: 4', out)
        self.assertIn('Event totals unavailable', out)
        self.assertIn('HomeBase-wide', out)
        self.assertIn('Missing days are unknown, not zero', out)
        payload = self.backend.call_args.args[0]
        self.assertEqual(payload['operation'], 'events.counts')
        self.assertEqual(payload['role'], 'guest')
        self.assertEqual(payload['device']['device_sn'], 'synthetic-doorbell')
        self.assertNotIn('value', payload)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.app.assert_not_called()
        self.discover.assert_not_called()

    def test_json_dates_are_filtered_projections_without_invented_zero_days(self):
        code, out, err = self.call('--json')
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual(data['counts'], [{'date': self.today, 'count': 4}])
        self.assertEqual(data['scope'], 'homebase')
        self.assertFalse(data['complete'])
        self.assertEqual(data['count_meaning'], 'unverified')
        self.assertFalse(data['event_totals_available'])
        self.assertNotIn('omit-this-value', out)
        self.assertNotIn('device_alias', data)
        self.assertNotIn('total', data)
        start = dt.date.fromisoformat(self.backend.call_args.args[0]['query']['startDate'])
        self.assertEqual((dt.date.today() - start).days, 6)

    def test_explicit_start_date_and_missing_counts_are_honest(self):
        self.result['counts'] = []
        code, out, err = self.call('--since', self.today)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('No activity dates returned', out)
        self.assertIn('unknown, not zero', out)
        self.assertEqual(self.backend.call_args.args[0]['query']['startDate'], self.today)

    def test_days_command_and_counts_alias_show_same_dates_without_numeric_totals(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['events', 'days'])
        self.assertEqual((code, err.getvalue()), (0, ''))
        self.assertEqual(out.getvalue(), self.call()[1])

    def test_empty_local_history_is_not_presented_as_authoritative(self):
        self.backend.return_value = {'ok': True, 'result': {'events': [], 'count': 0, 'source': 'local-p2p'}}
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['events', '--json'])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['events'], [])
        self.assertFalse(result['authoritative_history'])
        self.assertFalse(result['complete'])

    def test_device_filter_cache_and_oversized_date_range_are_refused(self):
        for argv in (('--device', 'Front Door'), ('--source', 'cache'), ('--since', '2020-01-01')):
            with self.subTest(argv=argv):
                self.assertEqual(self.call(*argv)[0], 2)
        self.backend.assert_not_called()
        self.app.assert_not_called()

    def test_invalid_count_buckets_are_errors_not_empty_history(self):
        for rows in ([{'date': self.today, 'count': -1}], [{'date': self.today, 'count': True}],
                     [{'date': 'invalid', 'count': 1}], [None], [{'date': '2020-01-01', 'count': 1}],
                     [{'date': self.today, 'count': 1}] * 2, [{'date': self.today, 'count': 1}] * 32):
            with self.subTest(rows=rows):
                self.result['counts'] = rows
                code, out, err = self.call('--json')
                self.assertEqual((code, err), (2, ''))
                self.assertEqual(json.loads(out)['error']['code'], 'invalid_counts_response')
