from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from eufy_cli import local_config, session_transfer
from eufy_cli.backend import BackendError, run_backend
from eufy_cli.cli import main
from tests.test_session_transfer import profile


READ = {"ok": True, "result": {"enabled": False, "fresh": True, "source": "local-p2p"}}
WRITE = {"ok": True, "result": {"enabled": False, "previous": True, "changed": True,
                                 "fresh": True, "source": "local-p2p"}}


class EverydayCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "state" / "local.json"
        self.original = profile(role="unknown")
        local_config._save(self.original, self.config)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(local_config, "DEFAULT_CONFIG_PATH", self.config))
        self.app = self.stack.enter_context(patch("eufy_cli.cli.AppCache.load", side_effect=AssertionError("No app fallback")))
        self.probe = self.stack.enter_context(patch("eufy_cli.cli.discover", return_value={"responders": [
            {"host": "10.254.50.26", "port": 51800, "matched_cached_device": True,
             "device_alias": self.original["alias"]}]}))
        self.backend = self.stack.enter_context(patch("eufy_cli.cli.run_backend", return_value=READ))

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_bare_invocation_shows_short_help_without_io(self):
        code, out, err = self.call()
        self.assertEqual(code, 0)
        self.assertIn("eufy motion off", out)
        self.assertLess(len(out.splitlines()), 40)
        self.assertEqual(err, "")
        self.backend.assert_not_called()
        self.probe.assert_not_called()
        self.app.assert_not_called()

    def test_status_is_fresh_text_by_default_and_fast_path_does_not_discover(self):
        code, out, err = self.call("status")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "Motion detection is off. Verified on the doorbell.\n")
        self.backend.assert_called_once()
        self.assertEqual(self.backend.call_args.args[0]["operation"], "motion.get")
        self.probe.assert_not_called()
        self.app.assert_not_called()

    def test_json_can_be_requested_before_or_after_command(self):
        for argv in (("--json", "status"), ("status", "--json"), ("status", "--live", "--json")):
            with self.subTest(argv=argv):
                code, out, err = self.call(*argv)
                self.assertEqual((code, err), (0, ""))
                self.assertIs(json.loads(out)["device"]["live_motion"]["value"], False)

    def test_cached_status_and_dry_run_are_offline_and_honest(self):
        code, out, _ = self.call("status", "--cache-only")
        self.assertEqual(code, 0)
        self.assertIn("Saved metadata only", out)
        code, out, _ = self.call("motion", "off", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("No device contacted", out)
        self.assertIn("unconfirmed", out)
        self.backend.assert_not_called()
        self.probe.assert_not_called()

    def test_unknown_permission_has_one_time_actionable_recovery(self):
        code, out, err = self.call("motion", "off")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("eufy configure --confirm-admin", err)
        self.backend.assert_not_called()
        self.probe.assert_not_called()

    def test_confirmed_shared_admin_is_durable_without_changing_role_or_identity(self):
        self.assertEqual(self.call("configure", "--confirm-admin")[0], 0)
        after = json.loads(self.config.read_text())
        self.assertEqual(after.pop("permissions"), {"admin_confirmed": True})
        self.assertEqual(after, self.original)
        self.backend.assert_not_called()
        self.probe.assert_not_called()
        self.backend.return_value = WRITE
        for _ in range(2):
            code, out, err = self.call("motion", "off")
            self.assertEqual((code, err), (0, ""))
            self.assertIn("Now off", out)
            payload = self.backend.call_args.args[0]
            self.assertEqual(payload["role"], "admin")
            self.assertEqual(payload["station"]["raw"]["member"]["admin_user_id"], self.original["station"]["member"]["admin_user_id"])
            self.assertEqual(payload["device"]["device_sn"], self.original["device"]["device_sn"])
        self.assertEqual(self.call("configure", "--clear-admin")[0], 0)
        self.assertEqual(json.loads(self.config.read_text()), self.original)
        self.assertEqual(self.call("motion", "off")[0], 2)

    def test_explicit_guest_cannot_be_confirmed_or_overridden(self):
        local_config._save(profile(role="guest"), self.config)
        before = self.config.read_bytes()
        for argv in (("configure", "--confirm-admin"), ("motion", "off", "--shared-admin")):
            with self.subTest(argv=argv):
                code, out, err = self.call(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("Guest", err)
                self.assertEqual(self.config.read_bytes(), before)
        self.probe.assert_not_called()
        self.backend.assert_not_called()

    def test_permission_confirmation_roundtrips_privately_and_rejects_guest(self):
        self.call("configure", "--confirm-admin")
        export = Path(self.tmp.name) / "transfer.json"
        session_transfer.export_session(export)
        destination = Path(self.tmp.name) / "other" / "local.json"
        session_transfer.import_session(export, destination=destination)
        self.assertTrue(local_config.load(destination).select(None).admin_confirmed)
        data = json.loads(export.read_text())
        data["profile"]["role"] = "guest"
        export.write_text(json.dumps(data))
        with self.assertRaises(session_transfer.SessionTransferError):
            session_transfer.import_session(export, replace=True, destination=destination)

    def test_connection_failure_recovers_once_matching_identity_without_changing_profile(self):
        original = self.config.read_bytes()
        self.backend.side_effect = [BackendError("LOCAL_TIMEOUT", "connection timed out"), READ]
        code, out, err = self.call("status")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("off", out)
        self.probe.assert_called_once()
        self.assertEqual(self.backend.call_count, 2)
        first, second = [call.args[0] for call in self.backend.call_args_list]
        self.assertEqual(second["station"]["local"]["host"], "10.254.50.26")
        self.assertEqual(second["station"]["local"]["peerPort"], 51800)
        self.assertEqual(first["station"]["raw"], second["station"]["raw"])
        self.assertEqual(first["device"], second["device"])
        self.assertEqual(original, self.config.read_bytes())

    def test_write_recovery_requires_proof_no_setter_was_attempted(self):
        self.call("configure", "--confirm-admin")
        self.backend.side_effect = [BackendError("LOCAL_CONNECT_FAILED", "connection failed", retry_safe=True), WRITE]
        self.assertEqual(self.call("motion", "off")[0], 0)
        self.assertEqual(self.backend.call_count, 2)
        self.probe.assert_called_once()

    def test_no_repeat_after_possible_write_or_ack_readback_or_bridge_failure(self):
        self.call("configure", "--confirm-admin")
        for code in ("LOCAL_TIMEOUT", "LOCAL_CONNECT_FAILED", "WRITE_ACK_TIMEOUT", "WRITE_READBACK_UNAVAILABLE",
                     "WRITE_READBACK_MISMATCH", "LOCAL_COMMAND_FAILED", "backend_timeout"):
            with self.subTest(code=code):
                self.backend.reset_mock()
                self.probe.reset_mock()
                self.backend.side_effect = BackendError(code, "check eufy status")
                exit_code, out, err = self.call("motion", "off")
                self.assertEqual((exit_code, out), (2, ""))
                self.assertIn("status", err)
                self.backend.assert_called_once()
                self.probe.assert_not_called()

    def test_missing_or_ambiguous_identity_never_gets_a_second_connection(self):
        match = self.probe.return_value["responders"][0]
        for rows in ([], [{**match, "matched_cached_device": False}],
                     [match, {**match, "host": "10.254.50.27"}]):
            with self.subTest(rows=rows):
                self.backend.reset_mock()
                self.backend.side_effect = BackendError("LOCAL_TIMEOUT", "connection timed out")
                self.probe.return_value = {"responders": rows}
                self.assertEqual(self.call("status")[0], 2)
                self.backend.assert_called_once()

    def test_forced_discovery_and_failed_recovery_do_not_loop(self):
        self.backend.side_effect = BackendError("LOCAL_TIMEOUT", "connection timed out", retry_safe=True)
        self.assertEqual(self.call("status", "--discover")[0], 2)
        self.backend.assert_called_once()
        self.probe.assert_called_once()
        self.backend.reset_mock()
        self.probe.reset_mock()
        self.assertEqual(self.call("status")[0], 2)
        self.assertEqual(self.backend.call_count, 2)
        self.probe.assert_called_once()

    def test_invalid_invocation_has_json_error_and_does_not_leak_arguments(self):
        code, out, err = self.call("motion", "private-invalid-value", "--json")
        self.assertEqual((code, err), (2, ""))
        self.assertEqual(json.loads(out)["error"]["code"], "invalid_arguments")
        self.assertNotIn("private-invalid-value", out)
        self.backend.assert_not_called()

    def test_unverified_backend_output_cannot_be_claimed_as_write_success(self):
        self.call("configure", "--confirm-admin")
        self.backend.return_value = {"ok": True, "result": {**WRITE["result"], "fresh": False}}
        code, out, err = self.call("motion", "off")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("verification is unavailable", err)
        self.backend.assert_called_once()


class BridgeRecoveryTests(unittest.TestCase):
    def test_backend_explicit_retry_proof_survives_bridge_without_private_message(self):
        for proof in (True, False, "true", None):
            with self.subTest(proof=proof):
                response = {"ok": False, "error": {"code": "LOCAL_TIMEOUT", "message": "private-station-details",
                                                     "details": {"retrySafe": proof}}}
                completed = subprocess.CompletedProcess([], 2, json.dumps(response).encode())
                with patch("eufy_cli.backend.shutil.which", return_value="node"), patch("eufy_cli.backend.subprocess.run", return_value=completed):
                    with self.assertRaises(BackendError) as error:
                        run_backend({})
                self.assertIs(error.exception.retry_safe, proof is True)
                self.assertNotIn("private-station-details", str(error.exception))
