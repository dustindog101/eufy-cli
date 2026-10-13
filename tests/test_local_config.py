from __future__ import annotations
import json
import os
import subprocess
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from eufy_cli import local_config
from eufy_cli.cache import AppCache
from eufy_cli.cli import CliError, _cache, run
from eufy_cli.backend import build_payload
from tests.archive_fixture import device_archive


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'app_BCDevicesMan').write_bytes(device_archive())
        self.device = AppCache.load(self.root).select(None)
        self.path = self.root / 'state' / 'local.json'

    def assert_private_permissions(self, path, *, directory):
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if directory else 0o600)
            return
        script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath ([Console]::In.ReadToEnd())
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
$valid = $acl.AreAccessRulesProtected -and ($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -eq $sid) -and ($rules.Count -eq 1) -and ($rules[0].IdentityReference.Value -eq $sid) -and ($rules[0].AccessControlType -eq 'Allow') -and (($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl)
if (-not $valid) { exit 3 }
"""
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], input=str(path.resolve()), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'Windows ACL must grant only the owner full control without inheritance')

    def test_failed_permissions_leave_existing_config_and_no_identifiers_in_temp(self):
        local_config.save_from_device(self.device, self.path)
        original = self.path.read_bytes()
        calls = []
        def refuse_file(path, *, directory):
            calls.append(directory)
            if not directory:
                self.assertEqual(path.read_bytes(), b'')
                raise local_config.LocalConfigError('permission failure')
        with patch.object(local_config, '_private_permissions', side_effect=refuse_file):
            with self.assertRaises(local_config.LocalConfigError):
                local_config.update_host('10.254.50.26', self.path)
        self.assertEqual(calls, [True, False])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('.local-*')), [])

    def test_import_is_minimal_private_and_preserves_runtime_identity(self):
        self.device.record['password'] = 'never-copy-password'
        self.device.station['token'] = 'never-copy-token'
        local_config.save_from_device(self.device, self.path)
        text = self.path.read_text()
        for secret in ('never-copy-password', 'never-copy-token', 'synthetic-dsk-key', 'synthetic-cloud-address'):
            self.assertNotIn(secret, text)
        self.assert_private_permissions(self.path, directory=False)
        self.assert_private_permissions(self.path.parent, directory=True)
        loaded = local_config.load(self.path).select(None)
        before = build_payload(self.device, 'motion.get')
        after = build_payload(loaded, 'motion.get')
        self.assertEqual(before['station']['raw']['station_sn'], after['station']['raw']['station_sn'])
        self.assertEqual(before['station']['raw']['p2p_did'], after['station']['raw']['p2p_did'])
        self.assertEqual(before['station']['raw']['member']['admin_user_id'], after['station']['raw']['member']['admin_user_id'])
        self.assertEqual(before['station']['local'], after['station']['local'])
        for key in ('device_sn', 'station_sn', 'device_type', 'device_channel'):
            self.assertEqual(before['device'][key], after['device'][key])
        public = loaded.public()
        self.assertEqual(public['cache']['source'], 'standalone_config')
        self.assertEqual(public['status']['source'], 'standalone_config')
        self.assertNotIn(loaded.serial, json.dumps(public))
        self.assertEqual(loaded.role, self.device.role)

    def test_commands_work_with_app_loader_disabled(self):
        local_config.save_from_device(self.device, self.path)
        with patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path), patch.object(AppCache, 'load', side_effect=AssertionError('No app file access')) as app_loader:
            devices = run(type('Args', (), {'command': 'devices'})())
            self.assertEqual(devices['source'], 'standalone_config')
            refresh = run(type('Args', (), {'command': 'refresh', 'device': None, 'cache_only': True})())
            self.assertEqual(refresh['refresh']['metadata_source'], 'standalone_config')
            app_loader.assert_not_called()

    def test_invalid_config_fails_closed_instead_of_reading_app(self):
        self.path.parent.mkdir()
        self.path.write_text('{')
        with patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path), patch.object(AppCache, 'load') as app_loader:
            with self.assertRaises(CliError) as error:
                _cache()
            self.assertEqual(error.exception.code, 'local_config_invalid')
            app_loader.assert_not_called()

    def test_extra_credentials_public_ip_and_identity_mismatch_are_rejected(self):
        local_config.save_from_device(self.device, self.path)
        original = json.loads(self.path.read_text())
        for change in ('token', 'public_ip', 'station_mismatch'):
            data = json.loads(json.dumps(original))
            if change == 'token': data['station']['token'] = 'must-not-accept'
            if change == 'public_ip': data['station']['ip_addr'] = '8.8.8.8'
            if change == 'station_mismatch': data['device']['station_sn'] = 'different-station'
            self.path.write_text(json.dumps(data))
            with self.assertRaises(local_config.LocalConfigError): local_config.load(self.path)

    def test_existing_config_is_preserved_until_explicit_replace(self):
        local_config.save_from_device(self.device, self.path)
        original = self.path.read_bytes()
        with self.assertRaises(local_config.LocalConfigError): local_config.save_from_device(self.device, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.device.station['member'] = {'_admin_user_id': ''}
        with self.assertRaises(local_config.LocalConfigError): local_config.save_from_device(self.device, self.path, replace=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_host_update_matches_station_without_reading_app(self):
        local_config.save_from_device(self.device, self.path)
        before = json.loads(self.path.read_text())
        host = '10.254.50.26'
        args = type('Args', (), {'command': 'configure', 'host': host, 'from_app': False, 'device': None, 'replace': False})()
        reply = {'responders': [{'host': host, 'matched_cached_device': True, 'device_alias': self.device.alias}]}
        with patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path), patch.object(AppCache, 'load', side_effect=AssertionError('No app reads')) as app, patch('eufy_cli.cli.discover', return_value=reply):
            result = run(args)
            app.assert_not_called()
        after = json.loads(self.path.read_text())
        self.assertEqual(after['station']['ip_addr'], host)
        self.assert_private_permissions(self.path, directory=False)
        after['station']['ip_addr'] = before['station']['ip_addr']
        self.assertEqual(after, before)
        self.assertFalse(result['app_required_for_normal_commands'])

    def test_unmatched_host_update_preserves_config(self):
        local_config.save_from_device(self.device, self.path)
        original = self.path.read_bytes()
        args = type('Args', (), {'command': 'configure', 'host': '10.254.50.26', 'from_app': False, 'device': None, 'replace': False})()
        with patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path), patch('eufy_cli.cli.discover', return_value={'responders': []}):
            with self.assertRaises(CliError) as error:
                run(args)
        self.assertEqual(error.exception.code, 'station_identity_not_verified')
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_config_never_falls_back_to_app(self):
        with patch.object(local_config, 'DEFAULT_CONFIG_PATH', self.path), patch.object(AppCache, 'load') as app:
            with self.assertRaises(CliError) as error:
                _cache()
            self.assertEqual(error.exception.code, 'local_config_missing')
            app.assert_not_called()
