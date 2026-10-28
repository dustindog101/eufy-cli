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


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'private' / 'local.json'
        local_config._save(profile(role='guest'), self.path)
        self.original = self.path.read_bytes()
        self.today = dt.date.today()
        self.result = {
            'events': [], 'source': 'local-p2p', 'pages': 1,
            'time_basis': 'homebase_wall_clock', 'timezone_verified': False,
            'query_exhausted': True, 'truncated': False, 'partial_reason': None,
        }
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path))
        self.app = stack.enter_context(patch('eufy_cli.cli.AppCache.load', side_effect=AssertionError('No app fallback')))
        self.backend = stack.enter_context(patch('eufy_cli.events.run_backend', return_value={'ok': True, 'result': self.result}))

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['events', *args])
        return code, out.getvalue(), err.getvalue()

    def test_simple_days_select_one_local_calendar_day_without_writes_or_app_access(self):
        for view, days in ((None, 0), ('today', 0), ('yesterday', 1), ('day-before-yesterday', 2),
                           ((self.today - dt.timedelta(days=3)).isoformat(), 3)):
            with self.subTest(view=view):
                args = [] if view is None else [view]
                code, out, err = self.call(*args)
                self.assertEqual((code, err), (0, ''))
                date = (self.today - dt.timedelta(days=days)).isoformat()
                payload = self.backend.call_args.args[0]
                self.assertEqual(payload['query'], {'startDate': date, 'endDate': date, 'maxItems': 200})
                self.assertEqual(payload['operation'], 'events.local')
                self.assertEqual(payload['role'], 'guest')
                self.assertNotIn('value', payload)
                self.assertIn(date, out)
                self.assertIn('does not prove', out)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.app.assert_not_called()

    def test_times_are_readable_without_utc_conversion_and_duplicate_times_are_preserved(self):
        date = self.today.isoformat()
        row = {'timestamp': date + 'T06:03:04', 'end_timestamp': date + 'T06:03:15', 'type': 'recording',
               'storage_path': '/private/omit', 'device_sn': 'omit-identifier'}
        self.result['events'] = [row, dict(row), {**row, 'timestamp': date + 'T18:15:00', 'end_timestamp': date + 'T18:15:10'}]
        code, out, err = self.call()
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.count('6:03:04 AM'), 2)
        self.assertIn('6:15:00 PM', out)
        self.assertIn('3 recording(s)', out)
        self.assertIn('timezone unverified', out)
        self.assertNotIn('omit', out)
        code, out, err = self.call('--json')
        data = json.loads(out)
        self.assertEqual(data['events'][0]['timestamp'], date + 'T06:03:04')
        self.assertEqual(data['time_basis'], 'homebase_wall_clock')
        self.assertFalse(data['timezone_verified'])
        self.assertFalse(data['complete'])
        self.assertNotIn('omit', out)

    def test_partial_results_explain_missing_records_in_text_and_json(self):
        self.result.update(truncated=True, query_exhausted=False, partial_reason='timeout')
        self.assertIn('Partial results (timeout)', self.call()[1])
        data = json.loads(self.call('--json')[1])
        self.assertTrue(data['truncated'])
        self.assertFalse(data['query_exhausted'])

    def test_invalid_days_limits_and_conflicting_selectors_fail_before_backend(self):
        for args in [('nonsense',), ('--since', self.today.isoformat() + 'T06:00:00Z'),
                     ((self.today + dt.timedelta(days=1)).isoformat(),),
                     ((self.today - dt.timedelta(days=31)).isoformat(),), ('--limit', '201'), ('--limit', '0'),
                     ('yesterday', '--since', self.today.isoformat()), ('today', '--source', 'cache')]:
            with self.subTest(args=args):
                self.assertEqual(self.call(*args)[0], 2)
        self.backend.assert_not_called()

    def test_bridge_rejects_malformed_or_out_of_range_records_instead_of_empty_success(self):
        date = self.today.isoformat()
        valid = {'timestamp': date + 'T06:00:00', 'end_timestamp': date + 'T06:00:15', 'type': 'recording'}
        for row in [None, {}, {**valid, 'timestamp': 'bad'}, {**valid, 'end_timestamp': date + 'T05:59:59'},
                    {**valid, 'timestamp': date + 'T06:00:00Z'}, {**valid, 'type': 'invented'},
                    {**valid, 'timestamp': (self.today - dt.timedelta(days=2)).isoformat() + 'T06:00:00'}]:
            with self.subTest(row=row):
                self.result['events'] = [row]
                code, out, err = self.call('--json')
                self.assertEqual((code, err), (2, ''))
                self.assertEqual(json.loads(out)['error']['code'], 'invalid_history_response')

    def test_since_keeps_range_workflow_and_limit_is_forwarded(self):
        start = (self.today - dt.timedelta(days=5)).isoformat()
        self.assertEqual(self.call('--since', start, '--limit', '50')[0], 0)
        self.assertEqual(self.backend.call_args.args[0]['query'],
                         {'startDate': start, 'endDate': self.today.isoformat(), 'maxItems': 50})
