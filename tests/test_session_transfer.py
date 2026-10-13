from __future__ import annotations

import json
import ipaddress
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eufy_cli import local_config
from eufy_cli.session_transfer import (
    FORMAT,
    MAX_TRANSFER_BYTES,
    SessionTransferError,
    export_session,
    import_session,
)


def profile(*, role: str = 'guest', peer_port: int | None = 51800) -> dict:
    station = {
        'device_sn': 'synthetic-homebase',
        'device_model': 'T8010',
        'device_type': 10002,
        'main_sw_version': '3.4.2.6h',
        'main_hw_version': 'fixture-hardware',
        'p2p_did': 'synthetic-local-did',
        'ip_addr': '10.254.50.25',
        'member': {'admin_user_id': 'synthetic-user-id'},
    }
    if peer_port is not None:
        station['p2p_port'] = peer_port
    return {
        'version': 1,
        'alias': 'Front Door',
        'role': role,
        'station': station,
        'device': {
            'device_sn': 'synthetic-doorbell',
            'device_model': 'T8210',
            'device_type': 7,
            'device_channel': 0,
            'station_sn': 'synthetic-homebase',
            'main_sw_version': '2.2.4.6',
            'main_hw_version': 'fixture-device-hardware',
        },
    }


def envelope(data: dict | None = None, *, version=1) -> bytes:
    return json.dumps(
        {'format': FORMAT, 'version': version, 'profile': profile() if data is None else data},
        allow_nan=False,
        separators=(',', ':'),
    ).encode('utf-8')


class SessionTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source' / 'local.json'
        self.source.parent.mkdir()
        local_config._save(profile(), self.source)
        self.export = self.root / 'transfer.json'
        self.destination = self.root / 'destination' / 'local.json'

    def write_envelope(self, data: bytes | None = None) -> Path:
        path = self.root / 'input.json'
        path.write_bytes(envelope() if data is None else data)
        return path

    def test_round_trip_preserves_identity_role_and_optional_endpoint(self) -> None:
        exported = export_session(self.export, source=self.source)
        self.assertEqual(exported, self.export)
        package = json.loads(self.export.read_text(encoding='utf-8'))
        self.assertEqual(set(package), {'format', 'version', 'profile'})
        self.assertEqual(package['format'], FORMAT)
        self.assertEqual(package['version'], 1)
        self.assertEqual(package['profile'], profile())
        for secret_field in ('password', 'token', 'cloud_key', 'dsk_key', 'app_archive'):
            self.assertNotIn(secret_field, self.export.read_text(encoding='utf-8'))

        imported = import_session(self.export, destination=self.destination)
        self.assertEqual(imported, self.destination)
        restored = local_config.load(self.destination).select(None)
        self.assertEqual(restored.role, 'guest')
        self.assertEqual(restored.station['device_sn'], 'synthetic-homebase')
        self.assertEqual(restored.station['p2p_did'], 'synthetic-local-did')
        self.assertEqual(restored.station['member']['admin_user_id'], 'synthetic-user-id')
        self.assertEqual(restored.station['p2p_port'], 51800)

    def test_legacy_profile_without_peer_port_round_trips(self) -> None:
        legacy = profile(peer_port=None)
        local_config._save(legacy, self.source)
        export_session(self.export, source=self.source)
        restored_path = import_session(self.export, destination=self.destination)
        restored = local_config.load(restored_path).select(None)
        self.assertNotIn('p2p_port', restored.station)
        self.assertEqual(restored.role, 'guest')

    def test_export_requires_different_destination_and_preserves_existing_file(self) -> None:
        original = self.source.read_bytes()
        with self.assertRaises(SessionTransferError):
            export_session(self.source, overwrite=True, source=self.source)
        self.assertEqual(self.source.read_bytes(), original)

        self.export.write_bytes(b'keep this')
        with self.assertRaises(SessionTransferError):
            export_session(self.export, source=self.source)
        self.assertEqual(self.export.read_bytes(), b'keep this')
        export_session(self.export, overwrite=True, source=self.source)
        self.assertEqual(json.loads(self.export.read_text(encoding='utf-8'))['profile'], profile())

    def test_export_does_not_change_selected_parent_permissions(self) -> None:
        output_dir = self.root / 'chosen-output'
        output_dir.mkdir(mode=0o755)
        before = stat.S_IMODE(output_dir.stat().st_mode)
        export_session(output_dir / 'profile.json', source=self.source)
        self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), before)

    def test_export_refuses_hardlink_alias_even_with_overwrite(self) -> None:
        alias = self.root / 'hardlink.json'
        try:
            os.link(self.source, alias)
        except OSError as error:
            self.skipTest(f'hard links unavailable: {error.__class__.__name__}')
        original = self.source.read_bytes()
        with self.assertRaises(SessionTransferError):
            export_session(alias, overwrite=True, source=self.source)
        self.assertEqual(alias.read_bytes(), original)

    def test_export_refuses_symlink_destination(self) -> None:
        link = self.root / 'link.json'
        try:
            link.symlink_to(self.root / 'not-created.json')
        except OSError as error:
            self.skipTest(f'symlinks unavailable: {error.__class__.__name__}')
        with self.assertRaises(SessionTransferError):
            export_session(link, overwrite=True, source=self.source)
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.root / 'not-created.json').exists())

    def test_export_file_is_restricted_before_identifiers_are_written(self) -> None:
        original = local_config._private_permissions
        calls = []

        def verify_before_write(path: Path, *, directory: bool) -> None:
            calls.append(directory)
            if not directory:
                self.assertEqual(path.read_bytes(), b'')
            original(path, directory=directory)

        with patch.object(local_config, '_private_permissions', side_effect=verify_before_write):
            export_session(self.export, source=self.source)
        self.assertEqual(calls, [False])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.export.stat().st_mode), 0o600)

    def test_import_rejects_existing_profile_unless_replace_and_preserves_bytes(self) -> None:
        self.destination.parent.mkdir()
        self.destination.write_bytes(b'original profile bytes')
        original = self.destination.read_bytes()
        with self.assertRaises(SessionTransferError):
            import_session(self.write_envelope(), destination=self.destination)
        self.assertEqual(self.destination.read_bytes(), original)

        import_session(self.write_envelope(), replace=True, destination=self.destination)
        self.assertEqual(local_config.load(self.destination).select(None).role, 'guest')

    def test_first_import_does_not_clobber_profile_created_at_commit(self) -> None:
        self.destination.parent.mkdir()
        concurrent_bytes = b'profile created by another process'

        def create_competing_destination(_temporary: Path, destination: Path) -> None:
            destination.write_bytes(concurrent_bytes)
            raise FileExistsError('synthetic commit race')

        with patch.object(local_config.os, 'link', side_effect=create_competing_destination):
            with self.assertRaises(SessionTransferError):
                import_session(self.write_envelope(), destination=self.destination)
        self.assertEqual(self.destination.read_bytes(), concurrent_bytes)
        self.assertEqual(list(self.destination.parent.glob('.local-*')), [])

    def test_no_replace_save_ignores_cleanup_failure_after_publication(self) -> None:
        destination = self.root / 'destination' / 'local.json'
        original_unlink = Path.unlink

        def fail_temp_cleanup(path: Path, *args, **kwargs) -> None:
            if path.name.startswith('.local-'):
                raise OSError('synthetic cleanup failure')
            original_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', new=fail_temp_cleanup):
            saved = local_config._save(profile(), destination, overwrite=False)
        self.assertEqual(saved, destination)
        self.assertEqual(local_config.load(destination).select(None).role, 'guest')
        for temp_path in destination.parent.glob('.local-*'):
            temp_path.unlink()

    def test_import_rejects_source_destination_alias_and_symlink_destination(self) -> None:
        same_profile = self.write_envelope()
        with self.assertRaises(SessionTransferError):
            import_session(same_profile, destination=same_profile)
        self.assertEqual(same_profile.read_bytes(), envelope())

        link = self.root / 'destination-link.json'
        try:
            link.symlink_to(self.destination)
        except OSError as error:
            self.skipTest(f'symlinks unavailable: {error.__class__.__name__}')
        with self.assertRaises(SessionTransferError):
            import_session(self.write_envelope(), destination=link)
        self.assertTrue(link.is_symlink())
        self.assertFalse(self.destination.exists())

    def test_invalid_profile_or_envelope_is_rejected_before_destination_mutation(self) -> None:
        bad_profiles = []
        boolean_profile_version = profile()
        boolean_profile_version['version'] = True
        bad_profiles.append(boolean_profile_version)
        extra_credential = profile()
        extra_credential['station']['token'] = 'synthetic-secret'
        bad_profiles.append(extra_credential)
        bad_port = profile()
        bad_port['station']['p2p_port'] = True
        bad_profiles.append(bad_port)
        numeric_ip = profile()
        numeric_ip['station']['ip_addr'] = int(ipaddress.ip_address('10.254.50.25'))
        bad_profiles.append(numeric_ip)

        invalid_inputs = [envelope(item) for item in bad_profiles]
        invalid_inputs.append(envelope(version=True))
        invalid_inputs.append(json.dumps({'format': FORMAT, 'version': 1, 'profile': profile(), 'extra': 'x'}).encode())
        for bad in invalid_inputs:
            with self.subTest(bad=bad[:40]):
                input_path = self.write_envelope(bad)
                with self.assertRaises(SessionTransferError):
                    import_session(input_path, destination=self.destination)
                self.assertFalse(self.destination.exists())

    def test_duplicate_keys_nonfinite_numbers_and_deep_json_are_rejected(self) -> None:
        invalid = [
            b'{"format":"eufy-cli-profile","format":"eufy-cli-profile","version":1,"profile":{}}',
            envelope().replace(b'"version":1,"alias"', b'"version":1,"version":1,"alias"'),
            b'{"format":"eufy-cli-profile","version":1,"profile":{"version":NaN}}',
            b'{"format":"eufy-cli-profile","version":1,"profile":{"version":1e999}}',
            ('[' * 2000 + '0' + ']' * 2000).encode('ascii'),
        ]
        for bad in invalid:
            with self.subTest(prefix=bad[:40]):
                with self.assertRaises(SessionTransferError):
                    import_session(self.write_envelope(bad), destination=self.destination)
                self.assertFalse(self.destination.exists())
                self.assertFalse(self.destination.parent.exists())

    def test_actual_read_is_bounded_to_64_kib(self) -> None:
        oversized = self.root / 'oversized.json'
        oversized.write_bytes(b' ' * (MAX_TRANSFER_BYTES + 1))
        with self.assertRaises(SessionTransferError):
            import_session(oversized, destination=self.destination)
        self.assertFalse(self.destination.exists())

    def test_import_save_permission_and_replace_failures_preserve_existing_bytes(self) -> None:
        self.destination.parent.mkdir()
        self.destination.write_bytes(b'original profile bytes')
        original = self.destination.read_bytes()
        valid_input = self.write_envelope()

        with patch.object(local_config, '_private_permissions', side_effect=local_config.LocalConfigError('synthetic failure')):
            with self.assertRaises(SessionTransferError):
                import_session(valid_input, replace=True, destination=self.destination)
        self.assertEqual(self.destination.read_bytes(), original)

        with patch.object(local_config.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(SessionTransferError):
                import_session(valid_input, replace=True, destination=self.destination)
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(list(self.destination.parent.glob('.local-*')), [])


if __name__ == '__main__':
    unittest.main()
