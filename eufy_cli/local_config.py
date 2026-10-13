"""Private, minimal LAN metadata. No passwords, tokens, cloud keys or app state."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .backend import build_payload
from .cache import AppCache, CachedDevice

DEFAULT_CONFIG_PATH = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'eufy-cli' / 'local.json'
NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
STATION_FIELDS = {'device_sn', 'device_model', 'device_type', 'main_sw_version', 'main_hw_version', 'p2p_did', 'ip_addr', 'member'}
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


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {'version', 'alias', 'role', 'station', 'device'} or data.get('version') != 1:
        raise LocalConfigError('Local config schema is invalid')
    _text(data['alias'])
    if data['role'] not in ('admin', 'guest', 'unknown'):
        raise LocalConfigError('Local config role is invalid')
    station, device = data['station'], data['device']
    if not isinstance(station, dict) or set(station) != STATION_FIELDS or not isinstance(device, dict) or set(device) != DEVICE_FIELDS:
        raise LocalConfigError('Local config metadata schema is invalid')
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


def _save(data: dict[str, Any], path: Path) -> Path:
    _validate(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix='.local-', dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, allow_nan=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
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
    cache.devices = [CachedDevice(data['device']['device_sn'], data['device'], data['station'], data['role'], mtime, data['alias'], 'standalone_config')]
    return cache


def update_host(host: str, path: Path | None = None) -> Path:
    """Use only after CLI discovery matched the configured station identity."""
    path = config_path(path)
    try:
        data = _validate(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalConfigError('Local config must exist before changing its HomeBase address') from None
    data['station']['ip_addr'] = host
    return _save(data, path)
