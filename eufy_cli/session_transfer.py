"""Offline transfer for the minimal standalone local profile."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from . import local_config

FORMAT = 'eufy-cli-profile'
VERSION = 1
MAX_TRANSFER_BYTES = 64 * 1024


class SessionTransferError(ValueError):
    """A fixed, safe error for profile transfer failures."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError('non-finite JSON number')


def _decode_json(data: bytes) -> Any:
    try:
        return json.loads(
            data.decode('utf-8'),
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise SessionTransferError('Profile file contains invalid JSON.') from None


def _read_bytes(path: Path) -> bytes:
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_TRANSFER_BYTES + 1)
    except OSError:
        raise SessionTransferError('Cannot read profile file.') from None
    if len(data) > MAX_TRANSFER_BYTES:
        raise SessionTransferError('Profile file exceeds the 64 KiB limit.')
    return data


def _validated_profile(value: Any) -> dict[str, Any]:
    required = {'version', 'alias', 'role', 'station', 'device'}
    if (not isinstance(value, dict) or set(value) not in (required, required | {'permissions'})
            or type(value.get('version')) is not int or value['version'] != VERSION):
        raise SessionTransferError('Profile schema is unsupported or invalid.')
    try:
        return local_config._validate(value)
    except (local_config.LocalConfigError, TypeError, ValueError, KeyError, RecursionError):
        raise SessionTransferError('Profile schema is unsupported or invalid.') from None


def _profile_from_bytes(data: bytes) -> dict[str, Any]:
    return _validated_profile(_decode_json(data))


def _transfer_from_bytes(data: bytes) -> dict[str, Any]:
    envelope = _decode_json(data)
    if (not isinstance(envelope, dict) or set(envelope) != {'format', 'version', 'profile'}
            or envelope.get('format') != FORMAT or type(envelope.get('version')) is not int
            or envelope['version'] != VERSION):
        raise SessionTransferError('Profile transfer format is unsupported or invalid.')
    return _validated_profile(envelope['profile'])


def _serialize(profile: dict[str, Any]) -> bytes:
    envelope = {'format': FORMAT, 'version': VERSION, 'profile': profile}
    try:
        data = (json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SessionTransferError('Profile could not be serialized safely.') from None
    if len(data) > MAX_TRANSFER_BYTES:
        raise SessionTransferError('Profile file exceeds the 64 KiB limit.')
    return data


def _same_file_or_path(left: Path, right: Path) -> bool:
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
        if left.exists() and right.exists() and os.path.samefile(left, right):
            return True
    except (OSError, RuntimeError):
        raise SessionTransferError('Cannot safely compare profile paths.') from None
    return False


def _ensure_not_destination_symlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise SessionTransferError('Refusing to replace a profile symlink.')
    except OSError:
        raise SessionTransferError('Cannot safely inspect the profile destination.') from None


def _atomic_export(path: Path, data: bytes, *, overwrite: bool) -> None:
    temporary: Path | None = None
    try:
        if not path.parent.is_dir():
            raise SessionTransferError('Export directory must already exist.')
        fd, name = tempfile.mkstemp(prefix='.eufy-profile-', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as stream:
            # Restrict the temporary file before writing any profile identifiers.
            local_config._private_permissions(temporary, directory=False)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        _ensure_not_destination_symlink(path)
        if overwrite:
            os.replace(temporary, path)
        else:
            # A hard-link creation is atomic and fails if another process creates
            # the destination after the initial existence check.
            os.link(temporary, path)
    except SessionTransferError:
        raise
    except FileExistsError:
        raise SessionTransferError('Export file already exists; use --overwrite to replace it.') from None
    except (OSError, local_config.LocalConfigError):
        raise SessionTransferError('Cannot safely write profile export.') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def export_session(filename: str | Path, *, overwrite: bool = False, source: Path | None = None) -> Path:
    """Export the configured standalone profile without touching app or network state."""
    try:
        source_path = Path(local_config.config_path(source)).expanduser()
        destination = Path(filename).expanduser()
    except (TypeError, ValueError, OSError):
        raise SessionTransferError('Invalid profile path.') from None
    if type(overwrite) is not bool:
        raise SessionTransferError('Invalid export option.')
    _ensure_not_destination_symlink(destination)
    if _same_file_or_path(source_path, destination):
        raise SessionTransferError('Export must use a different file from the configured profile.')
    if destination.exists() and not overwrite:
        raise SessionTransferError('Export file already exists; use --overwrite to replace it.')
    profile = _profile_from_bytes(_read_bytes(source_path))
    _atomic_export(destination, _serialize(profile), overwrite=overwrite)
    return destination


def import_session(filename: str | Path, *, replace: bool = False, destination: Path | None = None) -> Path:
    """Validate and atomically install a standalone profile without app fallback."""
    try:
        source_path = Path(filename).expanduser()
        destination_path = Path(local_config.config_path(destination)).expanduser()
    except (TypeError, ValueError, OSError):
        raise SessionTransferError('Invalid profile path.') from None
    if type(replace) is not bool:
        raise SessionTransferError('Invalid import option.')
    _ensure_not_destination_symlink(destination_path)
    if _same_file_or_path(source_path, destination_path):
        raise SessionTransferError('Import source must differ from the configured profile path.')
    profile = _transfer_from_bytes(_read_bytes(source_path))
    if destination_path.exists() and not replace:
        raise SessionTransferError('A standalone profile already exists; use --replace to replace it.')
    try:
        # _save validates again, protects the configured private directory and
        # temp file, then atomically publishes without clobbering a profile that
        # appeared after the existence check unless replacement was requested.
        return local_config._save(profile, destination_path, overwrite=replace)
    except (local_config.LocalConfigError, OSError, TypeError, ValueError):
        raise SessionTransferError('Cannot safely install profile; existing profile was preserved.') from None
