"""Private, minimal LAN metadata. No passwords, tokens, cloud keys or app state."""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from pathlib import Path
import tempfile
from typing import Any

from .backend import build_payload
from .cache import AppCache, CachedDevice

DEFAULT_CONFIG_PATH = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'eufy-cli' / 'local.json'
NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
STATION_FIELDS = {'device_sn', 'device_model', 'device_type', 'main_sw_version', 'main_hw_version', 'p2p_did', 'ip_addr', 'member'}
OPTIONAL_STATION_FIELDS = {'p2p_port'}
DEVICE_FIELDS = {'device_sn', 'device_model', 'device_type', 'device_channel', 'station_sn', 'main_sw_version', 'main_hw_version'}


class LocalConfigError(ValueError):
    pass


def config_path(path: Path | None = None) -> Path:
    return path if path is not None else DEFAULT_CONFIG_PATH


def is_configured(path: Path | None = None) -> bool:
    return config_path(path).exists()


def _text(value: Any, *, optional: bool = False) -> str:
    if optional and value == '':
        return ''
    if not isinstance(value, str) or not value or len(value) > 256 or '\0' in value:
        raise LocalConfigError('Local config contains an invalid identifier or metadata field')
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise LocalConfigError('Local config contains an invalid type or channel')
    return value


def _peer_port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise LocalConfigError('Local config contains an invalid peer UDP port')
    return value


def _validate(data: Any) -> dict[str, Any]:
    required = {'version', 'alias', 'role', 'station', 'device'}
    if not isinstance(data, dict) or set(data) not in (required, required | {'permissions'}) or type(data.get('version')) is not int or data['version'] != 1:
        raise LocalConfigError('Local config schema is invalid')
    _text(data['alias'])
    if data['role'] not in ('admin', 'guest', 'unknown'):
        raise LocalConfigError('Local config role is invalid')
    if 'permissions' in data:
        permissions = data['permissions']
        if (not isinstance(permissions, dict) or set(permissions) != {'admin_confirmed'}
                or permissions['admin_confirmed'] is not True or data['role'] == 'guest'):
            raise LocalConfigError('Local config permission confirmation is invalid')
    station, device = data['station'], data['device']
    if not isinstance(station, dict) or set(station) not in (STATION_FIELDS, STATION_FIELDS | OPTIONAL_STATION_FIELDS) or not isinstance(device, dict) or set(device) != DEVICE_FIELDS:
        raise LocalConfigError('Local config metadata schema is invalid')
    if 'p2p_port' in station:
        _peer_port(station['p2p_port'])
    for obj in (station, device):
        for field in ('device_sn', 'device_model'):
            _text(obj[field])
        for field in ('main_sw_version', 'main_hw_version'):
            _text(obj[field], optional=True)
        _integer(obj['device_type'])
    _integer(device['device_channel'])
    _text(device['station_sn'])
    if device['station_sn'] != station['device_sn']:
        raise LocalConfigError('Local config device and station do not match')
    _text(station['p2p_did'])
    if not isinstance(station['ip_addr'], str):
        raise LocalConfigError('Local config requires a private IPv4 HomeBase address')
    try:
        ip = ipaddress.ip_address(station['ip_addr'])
    except ValueError:
        raise LocalConfigError('Local config requires a private IPv4 HomeBase address') from None
    if ip.version != 4 or not any(ip in n for n in NETWORKS):
        raise LocalConfigError('Local config requires a private IPv4 HomeBase address')
    if not isinstance(station['member'], dict) or set(station['member']) != {'admin_user_id'}:
        raise LocalConfigError('Local config member schema is invalid')
    _text(station['member']['admin_user_id'])
    return data


def save_from_device(device: CachedDevice, path: Path | None = None, *, replace: bool = False) -> Path:
    path = config_path(path)
    if path.exists() and not replace:
        raise LocalConfigError('Local config already exists; use --replace to reimport saved app metadata')
    payload = build_payload(device, 'motion.get')
    raw = payload['station']['raw']
    station = {
        'device_sn': raw['station_sn'], 'device_model': raw['station_model'], 'device_type': raw['device_type'],
        'main_sw_version': raw['main_sw_version'], 'main_hw_version': raw['main_hw_version'],
        'p2p_did': raw['p2p_did'], 'ip_addr': payload['station']['local']['host'],
        'member': {'admin_user_id': raw['member']['admin_user_id']},
    }
    target = {k: payload['device'][k] for k in DEVICE_FIELDS}
    data = _validate({'version': 1, 'alias': device.alias, 'role': device.role, 'station': station, 'device': target})
    return _save(data, path)



def _private_permissions(path: Path, *, directory: bool) -> None:
    """Restrict the config to its owner using the operating system's permissions."""
    if os.name != 'nt':
        path.chmod(0o700 if directory else 0o600)
        return
    # Windows chmod does not remove other users' access. Replace the DACL,
    # protecting it from inherited grants before any identifiers are written.
    script = r"""
$ErrorActionPreference = 'Stop'
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
if ($request.directory) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
} else {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
}
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
$acl.AddAccessRule($rule)
if ($request.directory) {
    [System.IO.Directory]::SetAccessControl($request.path, $acl)
} else {
    [System.IO.File]::SetAccessControl($request.path, $acl)
}
"""
    try:
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            input=json.dumps({'path': str(path.resolve()), 'directory': directory}),
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise LocalConfigError('Could not restrict local config permissions; no identifiers were written') from None


def _save(data: dict[str, Any], path: Path, *, overwrite: bool = True) -> Path:
    _validate(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_permissions(path.parent, directory=True)
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix='.local-', dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, 'w') as stream:
            _private_permissions(temp_path, directory=False)
            json.dump(data, stream, allow_nan=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp_path, path)
        else:
            # Linking a fully-written same-directory temp file publishes it
            # atomically only if no destination already exists.
            os.link(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                # Once published, cleanup failure must not turn success into an
                # error that could mislead the caller about the committed file.
                pass
    return path


def load(path: Path | None = None) -> AppCache:
    path = config_path(path)
    try:
        if path.stat().st_size > 65536:
            raise LocalConfigError('Local config exceeds its size limit')
        data = _validate(json.loads(path.read_text()))
        mtime = path.stat().st_mtime
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalConfigError('Local config is missing or invalid; no app-cache fallback was attempted') from None
    # Reuse inventory selection/alias functions, without loading any app file.
    cache = AppCache(group_container=path.parent)
    cache.metadata_source = 'standalone_config'
    cache.devices = [CachedDevice(data['device']['device_sn'], data['device'], data['station'], data['role'], mtime, data['alias'], 'standalone_config', data.get('permissions', {}).get('admin_confirmed', False))]
    return cache


def confirm_admin(*, confirmed: bool, path: Path | None = None) -> Path:
    """Record the user's app permission assertion for this exact private profile.

    The imported role and all device/account identities stay intact. Reimporting
    identifiers replaces the profile and clears this assertion.
    """
    path = config_path(path)
    try:
        data = _validate(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalConfigError('A valid local profile is required before confirming permission') from None
    if confirmed:
        if data['role'] == 'guest':
            raise LocalConfigError('Guest permission cannot change settings; ask the owner for Admin access')
        data['permissions'] = {'admin_confirmed': True}
    else:
        data.pop('permissions', None)
    return _save(data, path)


def update_host(host: str, path: Path | None = None) -> Path:
    """Use only after CLI discovery matched the configured station identity."""
    path = config_path(path)
    try:
        data = _validate(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalConfigError('Local config must exist before changing its HomeBase address') from None
    if data['station']['ip_addr'] != host:
        data['station'].pop('p2p_port', None)
    data['station']['ip_addr'] = host
    return _save(data, path)


def update_endpoint(host: str, port: int, path: Path | None = None) -> Path:
    """Persist a fresh identity-matched local endpoint without replacing identity or role."""
    port = _peer_port(port)
    path = config_path(path)
    try:
        data = _validate(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalConfigError('Local config must exist before updating its HomeBase endpoint') from None
    data['station']['ip_addr'] = host
    data['station']['p2p_port'] = port
    return _save(data, path)
