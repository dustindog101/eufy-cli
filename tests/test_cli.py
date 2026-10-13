from __future__ import annotations

import datetime as dt
import json
import io
from contextlib import redirect_stdout
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eufy_cli import local_config
from eufy_cli.archive import KeyedArchiveDecoder, load_archive
from eufy_cli.backend import build_payload
from eufy_cli.cache import AppCache
from eufy_cli.cli import CliError, main, run
from eufy_cli.discovery import DiscoveryError, LOOKUP_PACKET, parse_reply
from eufy_cli.events import EventError, normalize_records, read_cache_events
from tests.archive_fixture import Builder, device_archive, userinfo_archive


class ArchiveTests(unittest.TestCase):
    def test_decodes_foundation_collections_and_null(self) -> None:
        b = Builder()
        nested = b.dictionary({"motionDetection": True, "nothing": plistlib.UID(0)})
        root = b.custom("BCDevicesMan", {"payload": nested, "list": b.array(["one", "two"])})
        decoded = KeyedArchiveDecoder(plistlib.loads(b.plist(root))).root()
        self.assertEqual(decoded["__class__"], "BCDevicesMan")
        self.assertEqual(decoded["payload"], {"motionDetection": True, "nothing": None})
        self.assertEqual(decoded["list"], ["one", "two"])

    def test_loads_synthetic_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.archive"
            path.write_bytes(device_archive())
            root = load_archive(path)
        self.assertEqual(root["__class__"], "BCDevicesMan")


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / "cache_BCDevicesMan").write_bytes(device_archive())

    def test_device_projection_is_redacted_and_stale(self) -> None:
        cache = AppCache.load(self.path)
        self.assertEqual(len(cache.devices), 1)
        device = cache.devices[0]
        public = device.public()
        encoded = json.dumps(public)
        for private in (device.serial, "synthetic-dsk-key", "10.254.50.25", "synthetic-cloud-address", "synthetic-local-did"):
            self.assertNotIn(private, encoded)
        self.assertEqual(public["model"], "T8210")
        self.assertEqual(public["firmware"], "2.2.4.6")
        self.assertEqual(public["role"], "unknown")
        self.assertEqual(public["status"]["motion_detection"], {"value": True, "stale": True})

    def test_defaults_to_one_t8210(self) -> None:
        cache = AppCache.load(self.path)
        self.assertEqual(cache.select(None).model, "T8210")

    def test_numeric_member_type_is_unknown_without_a_verified_mapping(self) -> None:
        cache = AppCache.load(self.path)
        self.assertEqual(cache.select(None).public()["role"], "unknown")

    def test_account_privilege_is_not_used_as_per_device_role(self) -> None:
        (self.path / "userinfo.archive").write_bytes(userinfo_archive("guest"))
        cache = AppCache.load(self.path)
        public = cache.select(None).public()
        self.assertEqual(public["role"], "unknown")
        self.assertNotIn("synthetic-token", json.dumps(public))
        self.assertNotIn("user@example.invalid", json.dumps(public))

    def test_explicit_role_string_is_preserved(self) -> None:
        (self.path / "cache_BCDevicesMan").write_bytes(device_archive(role="guest"))
        cache = AppCache.load(self.path)
        self.assertEqual(cache.select(None).public()["role"], "guest")

    def test_unknown_numeric_member_type_refuses_motion_write_before_backend(self) -> None:
        cache = AppCache.load(self.path)
        args = type("Args", (), {"command": "motion", "action": "off", "device": None, "dry_run": False})()
        with patch("eufy_cli.cli._cache", return_value=cache), patch("eufy_cli.backend.run_backend") as backend:
            with self.assertRaises(CliError) as raised:
                run(args)
        self.assertEqual(raised.exception.code, "role_unknown")
        backend.assert_not_called()

    def test_guest_motion_write_is_refused_before_backend(self) -> None:
        (self.path / "cache_BCDevicesMan").write_bytes(device_archive(role="guest"))
        cache = AppCache.load(self.path)
        args = type("Args", (), {"command": "motion", "action": "off", "device": None, "dry_run": False})()
        with patch("eufy_cli.cli._cache", return_value=cache), patch("eufy_cli.backend.run_backend") as backend:
            with self.assertRaises(CliError) as raised:
                run(args)
        self.assertEqual(raised.exception.code, "guest_settings_forbidden")
        backend.assert_not_called()

    def test_live_status_uses_only_normalized_motion_result(self) -> None:
        cache = AppCache.load(self.path)
        args = type("Args", (), {"command": "status", "device": None, "live": True})()
        response = {"ok": True, "result": {"enabled": False, "fresh": True, "source": "local-p2p", "raw": "private-backend-content"}}
        with patch("eufy_cli.cli._cache", return_value=cache), patch("eufy_cli.cli.run_backend", return_value=response):
            result = run(args)
        live = result["device"]["live_motion"]
        self.assertEqual(live, {"source": "local_p2p", "available": True, "value": False, "stale": False})
        self.assertNotIn("private-backend-content", json.dumps(result))

    def test_invalid_since_has_clear_error(self) -> None:
        cache = AppCache.load(self.path)
        args = type("Args", (), {"command": "events", "limit": 10, "since": "not-a-date", "source": "local", "device": None})()
        with patch("eufy_cli.cli._cache", return_value=cache):
            with self.assertRaises(CliError) as raised:
                run(args)
        self.assertEqual(raised.exception.code, "invalid_since")

    def test_bridge_payload_forces_empty_cloud_address(self) -> None:
        cache = AppCache.load(self.path)
        payload = build_payload(cache.select(None), "events.local", query={"startDate": "2026-10-01", "endDate": "2026-10-12", "maxItems": 10})
        self.assertEqual(payload["station"]["raw"]["app_conn"], "")
        self.assertEqual(payload["station"]["raw"]["station_model"], "T8010")
        self.assertEqual(payload["station"]["raw"]["device_type"], 10002)
        self.assertEqual(payload["station"]["local"]["host"], "10.254.50.25")
        self.assertEqual(payload["device"]["device_model"], "T8210")
        self.assertNotIn("synthetic-dsk-key", json.dumps(payload))
        self.assertEqual(payload["station"]["local"]["dskKey"], "")
        self.assertIsNone(payload["station"]["local"]["dskExpirationMs"])

    def test_lan_payload_needs_no_cloud_key_or_expiry(self) -> None:
        device = AppCache.load(self.path).select(None)
        for expiration in (1, None):
            device.station["dsk_expiration"] = expiration
            device.station.pop("dsk_key", None)
            device.record["main_sw_version"] = "2.2.4.6"
            device.record["main_hw_version"] = "fixture-hardware"
            payload = build_payload(device, "motion.get")
            self.assertEqual(payload["station"]["local"]["dskKey"], "")
            self.assertIsNone(payload["station"]["local"]["dskExpirationMs"])
            self.assertEqual(payload["device"]["main_sw_version"], "2.2.4.6")
            self.assertEqual(payload["device"]["main_hw_version"], "fixture-hardware")

    def test_shared_admin_assertion_preserves_wire_identity(self) -> None:
        device = AppCache.load(self.path).select(None)
        device.role = "unknown"
        cache = AppCache(self.path)
        cache.devices = [device]
        args = type("Args", (), {"command": "motion", "action": "off", "device": None, "dry_run": False, "shared_admin": True})()
        response = {"ok": True, "result": {"enabled": False, "previous": True, "changed": True}}
        with patch("eufy_cli.cli._cache", return_value=cache), patch("eufy_cli.cli.run_backend", return_value=response) as backend:
            result = run(args)
        payload = backend.call_args.args[0]
        expected = build_payload(device, "motion.set", motion_value=False)
        self.assertEqual(payload["station"], expected["station"])
        self.assertEqual(payload["role"], "admin")
        self.assertFalse(result["motion_detection"])

    def test_shared_admin_assertion_does_not_override_explicit_guest(self) -> None:
        cache = AppCache.load(self.path)
        cache.select(None).role = "guest"
        args = type("Args", (), {"command": "motion", "action": "off", "device": None, "dry_run": False, "shared_admin": True})()
        with patch("eufy_cli.cli._cache", return_value=cache), patch("eufy_cli.cli.run_backend") as backend:
            with self.assertRaises(CliError) as raised:
                run(args)
        self.assertEqual(raised.exception.code, "guest_settings_forbidden")
        backend.assert_not_called()

    def test_refresh_reloads_archive_and_does_not_modify_it(self) -> None:
        args = type("Args", (), {"command": "refresh", "device": None, "cache_only": True})()
        archive = self.path / "cache_BCDevicesMan"
        original_loader = AppCache.load
        with patch("eufy_cli.cli._cache", side_effect=lambda: original_loader(self.path)), patch("eufy_cli.cli.run_backend") as backend:
            first_bytes = archive.read_bytes()
            first = run(args)
            self.assertEqual(archive.read_bytes(), first_bytes)
            archive.write_bytes(device_archive(role="admin"))
            second_bytes = archive.read_bytes()
            second = run(args)
            self.assertEqual(archive.read_bytes(), second_bytes)
        self.assertEqual(first["device"]["role"], "unknown")
        self.assertEqual(second["device"]["role"], "admin")
        self.assertTrue(second["refresh"]["metadata_reloaded"])
        self.assertFalse(second["refresh"]["app_cache_modified"])
        self.assertTrue(second["refresh"]["metadata_stale"])
        self.assertEqual(second["refresh"]["fresh_fields"], [])
        backend.assert_not_called()

    def test_refresh_fresh_motion_does_not_relabel_cached_fields(self) -> None:
        cache = AppCache.load(self.path)
        args = type("Args", (), {"command": "refresh", "device": None, "cache_only": False})()
        response = {"ok": True, "result": {"enabled": False, "fresh": True, "source": "local-p2p"}}
        with patch("eufy_cli.cli._cache", return_value=cache), patch("eufy_cli.cli.run_backend", return_value=response):
            result = run(args)
        self.assertFalse(result["device"]["live_motion"]["value"])
        self.assertFalse(result["device"]["live_motion"]["stale"])
        self.assertTrue(result["device"]["cache"]["stale"])
        self.assertTrue(result["refresh"]["metadata_stale"])
        self.assertFalse(result["refresh"]["app_refresh_triggered"])


class HelpTests(unittest.TestCase):
    def test_help_and_limits_work_without_cache_or_backend(self) -> None:
        with patch("eufy_cli.cli._cache") as cache, patch("eufy_cli.cli.run_backend") as backend:
            for topic in (None, "devices", "status", "refresh", "discover", "events", "motion", "limits", "configure"):
                output = io.StringIO()
                with redirect_stdout(output):
                    try:
                        code = main(["help", topic] if topic else ["help"])
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(code, 0)
                self.assertIn("usage: eufy", output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["limits"]), 0)
            limits = json.loads(output.getvalue())
            self.assertFalse(limits["metadata"]["app_cache_updated_by_cli"])
            self.assertFalse(limits["network"]["cloud_calls"])
            self.assertIn("event_history", limits["unresolved"])
            cache.assert_not_called()
            backend.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def test_exact_lookup_packet(self) -> None:
        self.assertEqual(LOOKUP_PACKET, bytes.fromhex("f13000020000"))

    def test_parses_reply(self) -> None:
        body = b"station\0" + (42).to_bytes(4, "big") + b"suffix\0\0"
        packet = b"\xf1\x41" + len(body).to_bytes(2, "big") + body
        self.assertEqual(parse_reply(packet), "station-000042-suffix")

    def test_rejects_wrong_length_and_header(self) -> None:
        with self.assertRaises(DiscoveryError):
            parse_reply(b"\xf1\x41\x00\x14" + b"x" * 20 + b"extra")
        with self.assertRaises(DiscoveryError):
            parse_reply(b"\x00\x00" + b"x" * 22)

    def test_rejects_non_lan_targets_and_nonfinite_timeout(self) -> None:
        from eufy_cli.discovery import discover
        with self.assertRaises(DiscoveryError):
            discover(host="0.0.0.0")
        with self.assertRaises(DiscoveryError):
            discover(timeout=float("nan"))


class ConfigureEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app_root = self.root / "app"
        self.app_root.mkdir()
        (self.app_root / "cache_BCDevicesMan").write_bytes(device_archive())
        self.config_path = self.root / "config" / "local.json"
        device = AppCache.load(self.app_root).select(None)
        device.role = "admin"
        self.alias = device.alias
        local_config.save_from_device(device, self.config_path)
        self.original = self.config_path.read_bytes()

    def _run_discovery(self, responders):
        output = io.StringIO()
        with patch.object(local_config, "DEFAULT_CONFIG_PATH", self.config_path), \
                patch.object(AppCache, "load", side_effect=AssertionError("No app fallback")) as app_loader, \
                patch("eufy_cli.cli.discover", return_value={"responders": responders}) as probe, \
                redirect_stdout(output):
            code = main(["configure", "--from-discovery"])
        app_loader.assert_not_called()
        self.assertEqual(probe.call_args.kwargs["timeout"], 2)
        self.assertIsNotNone(probe.call_args.kwargs["cache"])
        self.assertEqual(probe.call_args.kwargs["cache"].metadata_source, "standalone_config")
        return code, json.loads(output.getvalue())

    def test_from_discovery_saves_one_unique_matched_endpoint_and_preserves_profile(self) -> None:
        host, port = "10.254.50.26", 51800
        matched = {"host": host, "port": port, "matched_cached_device": True, "device_alias": self.alias}
        code, result = self._run_discovery([
            matched,
            dict(matched),
            {"host": "10.254.50.27", "port": 51801, "matched_cached_device": False, "device_alias": None},
        ])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        before = json.loads(self.original)
        after = json.loads(self.config_path.read_text())
        self.assertEqual(after["alias"], before["alias"])
        self.assertEqual(after["role"], before["role"])
        self.assertEqual(after["device"], before["device"])
        self.assertEqual(after["station"]["device_sn"], before["station"]["device_sn"])
        self.assertEqual(after["station"]["p2p_did"], before["station"]["p2p_did"])
        self.assertEqual(after["station"]["member"], before["station"]["member"])
        self.assertEqual(after["station"]["ip_addr"], host)
        self.assertEqual(after["station"]["p2p_port"], port)

    def test_from_discovery_without_identity_match_preserves_config(self) -> None:
        code, result = self._run_discovery([
            {"host": "10.254.50.26", "port": 51800, "matched_cached_device": False, "device_alias": None},
        ])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "station_identity_not_verified")
        self.assertEqual(self.config_path.read_bytes(), self.original)

    def test_from_discovery_with_multiple_unique_endpoints_preserves_config(self) -> None:
        code, result = self._run_discovery([
            {"host": "10.254.50.26", "port": 51800, "matched_cached_device": True, "device_alias": self.alias},
            {"host": "10.254.50.27", "port": 51801, "matched_cached_device": True, "device_alias": self.alias},
        ])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "station_endpoint_ambiguous")
        self.assertEqual(self.config_path.read_bytes(), self.original)

    def test_invalid_profile_peer_port_fails_before_backend(self) -> None:
        data = json.loads(self.config_path.read_text())
        data["station"]["p2p_port"] = True
        self.config_path.write_text(json.dumps(data))
        args = type("Args", (), {"command": "status", "device": None, "live": True})()
        with patch.object(local_config, "DEFAULT_CONFIG_PATH", self.config_path), patch("eufy_cli.cli.run_backend") as backend:
            with self.assertRaises(CliError) as raised:
                run(args)
        self.assertEqual(raised.exception.code, "local_config_invalid")
        backend.assert_not_called()

    def test_invalid_profile_peer_port_fails_before_discovery_or_write(self) -> None:
        data = json.loads(self.config_path.read_text())
        data["station"]["p2p_port"] = 0
        self.config_path.write_text(json.dumps(data))
        original = self.config_path.read_bytes()
        output = io.StringIO()
        with patch.object(local_config, "DEFAULT_CONFIG_PATH", self.config_path), \
                patch.object(AppCache, "load", side_effect=AssertionError("No app fallback")) as app_loader, \
                patch("eufy_cli.cli.discover") as probe, redirect_stdout(output):
            code = main(["configure", "--from-discovery"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "local_config_import_failed")
        app_loader.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), original)


class LiveDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app_root = self.root / "app"
        self.app_root.mkdir()
        (self.app_root / "cache_BCDevicesMan").write_bytes(device_archive())
        self.config_path = self.root / "config" / "local.json"
        device = AppCache.load(self.app_root).select(None)
        device.role = "admin"
        self.alias = device.alias
        local_config.save_from_device(device, self.config_path)
        self.original = self.config_path.read_bytes()

    def _run_live_read(self, argv, responders):
        output = io.StringIO()
        response = {"ok": True, "result": {"enabled": True, "fresh": True, "source": "local-p2p"}}
        with patch.object(local_config, "DEFAULT_CONFIG_PATH", self.config_path), \
                patch.object(AppCache, "load", side_effect=AssertionError("No app fallback")) as app_loader, \
                patch("eufy_cli.cli.discover", return_value={"responders": responders}) as probe, \
                patch("eufy_cli.cli.run_backend", return_value=response) as backend, \
                redirect_stdout(output):
            code = main(argv)
        app_loader.assert_not_called()
        return code, json.loads(output.getvalue()), probe, backend

    def test_live_status_and_refresh_use_unique_discovered_endpoint_in_memory(self) -> None:
        host, port = "10.254.50.26", 51800
        responders = [
            {"host": host, "port": port, "matched_cached_device": True, "device_alias": self.alias},
            {"host": host, "port": port, "matched_cached_device": True, "device_alias": self.alias},
            {"host": "10.254.50.27", "port": 51801, "matched_cached_device": False, "device_alias": None},
        ]
        for argv in (["status", "--live", "--discover"], ["refresh", "--discover"]):
            with self.subTest(command=argv[0]):
                before = self.config_path.read_bytes()
                code, result, probe, backend = self._run_live_read(argv, responders)
                self.assertEqual(code, 0)
                self.assertTrue(result["ok"])
                probe.assert_called_once()
                self.assertEqual(probe.call_args.kwargs["timeout"], 2)
                self.assertEqual(probe.call_args.kwargs["cache"].metadata_source, "standalone_config")
                payload = backend.call_args.args[0]
                self.assertEqual(payload["station"]["local"]["host"], host)
                self.assertEqual(payload["station"]["local"]["peerPort"], port)
                self.assertEqual(payload["station"]["raw"]["station_sn"], "synthetic-station-serial")
                self.assertEqual(payload["station"]["raw"]["p2p_did"], "synthetic-local-did")
                self.assertEqual(payload["device"]["device_sn"], "synthetic-doorbell-serial")
                self.assertEqual(payload["role"], "admin")
                backend.assert_called_once()
                self.assertEqual(self.config_path.read_bytes(), before)
                self.assertEqual(before, self.original)

    def test_live_discovery_no_or_ambiguous_match_fails_before_backend(self) -> None:
        cases = (
            ([], "station_identity_not_verified"),
            ([
                {"host": "10.254.50.26", "port": 51800, "matched_cached_device": True, "device_alias": self.alias},
                {"host": "10.254.50.27", "port": 51801, "matched_cached_device": True, "device_alias": self.alias},
            ], "station_endpoint_ambiguous"),
        )
        for responders, expected_code in cases:
            with self.subTest(error=expected_code):
                before = self.config_path.read_bytes()
                output = io.StringIO()
                with patch.object(local_config, "DEFAULT_CONFIG_PATH", self.config_path), \
                        patch.object(AppCache, "load", side_effect=AssertionError("No app fallback")) as app_loader, \
                        patch("eufy_cli.cli.discover", return_value={"responders": responders}) as probe, \
                        patch("eufy_cli.cli.run_backend") as backend, redirect_stdout(output):
                    code = main(["status", "--live", "--discover"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], expected_code)
                app_loader.assert_not_called()
                probe.assert_called_once()
                backend.assert_not_called()
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_invalid_discover_option_combinations_fail_before_io(self) -> None:
        for argv in (["status", "--discover"], ["refresh", "--cache-only", "--discover"]):
            with self.subTest(argv=argv):
                output = io.StringIO()
                with patch.object(local_config, "load") as load_config, \
                        patch("eufy_cli.cli.discover") as probe, \
                        patch("eufy_cli.cli.run_backend") as backend, redirect_stdout(output):
                    code = main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_option_combination")
                load_config.assert_not_called()
                probe.assert_not_called()
                backend.assert_not_called()


class EventTests(unittest.TestCase):
    def test_normalization_omits_private_content_and_urls(self) -> None:
        records = [{
            "timestamp": "2026-10-11T18:20:00Z",
            "event_type": "motion_detected",
            "device_sn": "synthetic-doorbell-serial",
            "title": "private event contents",
            "image_url": "https://example.invalid/secret-image",
            "token": "secret-token",
        }]
        event = normalize_records(records, {"synthetic-doorbell-serial": "Front Door"})[0]
        self.assertEqual(event, {"timestamp": "2026-10-11T18:20:00+00:00", "type": "motion_detected", "device_alias": "Front Door"})
        self.assertNotIn("private event", json.dumps(event))
        self.assertNotIn("https://", json.dumps(event))
        self.assertNotIn("secret-token", json.dumps(event))

    def test_cache_logs_fail_truthfully_when_unrecognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "log"
            logs.mkdir()
            (logs / "notification_extension_test.log").write_bytes(b"opaque\x00data\n")
            cache = AppCache(root)
            with self.assertRaises(EventError) as raised:
                read_cache_events(cache)
        self.assertEqual(raised.exception.code, "event_cache_unrecognized")

    def test_normalizes_numeric_timestamp(self) -> None:
        when = int(dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc).timestamp() * 1000)
        event = normalize_records({"items": [{"time": when, "type": 12}]}, {})[0]
        self.assertEqual(event["type"], "code_12")
        self.assertEqual(event["timestamp"], "2026-10-10T00:00:00+00:00")

    def test_normalizes_sanitized_station_record_shape(self) -> None:
        records = {"events": [{"startTime": "2026-10-10T01:00:00Z", "endTime": "2026-10-10T01:00:09Z", "frameCount": 12}]}
        event = normalize_records(records, {}, fallback_type="recording", fallback_alias="Front Door")[0]
        self.assertEqual(event, {
            "timestamp": "2026-10-10T01:00:00+00:00",
            "type": "recording",
            "device_alias": "Front Door",
            "end_timestamp": "2026-10-10T01:00:09+00:00",
            "frame_count": 12,
        })


if __name__ == "__main__":
    unittest.main()
