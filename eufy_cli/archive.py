"""Read-only decoder for Apple's NSKeyedArchive property-list format.

This module intentionally knows nothing about Eufy credentials. Callers must
select an explicit safe field set before serializing or displaying records.
"""

from __future__ import annotations

import datetime as dt
import plistlib
from pathlib import Path
from typing import Any


class ArchiveError(ValueError):
    """Raised when a cached archive cannot be decoded safely."""


class KeyedArchiveDecoder:
    """Decode common Foundation objects and custom keyed records."""

    def __init__(self, archive: dict[str, Any], *, max_objects: int = 200_000):
        objects = archive.get("$objects")
        top = archive.get("$top")
        if not isinstance(objects, list) or not isinstance(top, dict):
            raise ArchiveError("not an NSKeyedArchive")
        if len(objects) > max_objects:
            raise ArchiveError("archive object limit exceeded")
        self.objects = objects
        self.top = top
        self._memo: dict[int, Any] = {}

    @staticmethod
    def _uid_index(value: Any) -> int | None:
        if isinstance(value, plistlib.UID):
            return value.data
        return None

    def _class_name(self, raw: Any) -> str | None:
        if not isinstance(raw, dict):
            return None
        index = self._uid_index(raw.get("$class"))
        if index is None or index < 0 or index >= len(self.objects):
            return None
        cls = self.objects[index]
        if not isinstance(cls, dict):
            return None
        name = cls.get("$classname")
        return name if isinstance(name, str) else None

    def decode(self, value: Any) -> Any:
        index = self._uid_index(value)
        if index is not None:
            if index < 0 or index >= len(self.objects):
                raise ArchiveError("archive contains an invalid object reference")
            return self._decode_index(index)
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        if isinstance(value, tuple):
            return [self.decode(item) for item in value]
        if isinstance(value, dict):
            return {key: self.decode(item) for key, item in value.items() if not key.startswith("$")}
        return value

    def _decode_index(self, index: int) -> Any:
        if index in self._memo:
            return self._memo[index]
        raw = self.objects[index]
        if raw == "$null":
            self._memo[index] = None
            return None
        if not isinstance(raw, dict):
            value = self._decode_primitive(raw)
            self._memo[index] = value
            return value

        class_name = self._class_name(raw)
        if class_name in {"NSArray", "NSMutableArray", "NSSet", "NSMutableSet", "NSOrderedSet"}:
            result: list[Any] = []
            self._memo[index] = result
            members = raw.get("NS.objects", [])
            if isinstance(members, list):
                result.extend(self.decode(item) for item in members)
            return result
        if class_name in {"NSDictionary", "NSMutableDictionary"}:
            result_map: dict[str, Any] = {}
            self._memo[index] = result_map
            keys = raw.get("NS.keys", [])
            values = raw.get("NS.objects", [])
            if isinstance(keys, list) and isinstance(values, list):
                for key, item in zip(keys, values):
                    decoded_key = self.decode(key)
                    if isinstance(decoded_key, (str, int, float, bool)):
                        result_map[str(decoded_key)] = self.decode(item)
            return result_map
        if class_name in {"NSString", "NSMutableString"}:
            value = self.decode(raw.get("NS.string", ""))
            self._memo[index] = value
            return value
        if class_name in {"NSNumber", "__NSCFBoolean"}:
            for key in ("NS.boolval", "NS.intval", "NS.integer", "NS.real"):
                if key in raw:
                    value = self.decode(raw[key])
                    self._memo[index] = value
                    return value
        if class_name in {"NSData", "NSMutableData"}:
            value = raw.get("NS.data", b"")
            self._memo[index] = value
            return value

        # Install a placeholder before descending so shared references and
        # cycles in custom model objects do not recurse indefinitely.
        result = {"__class__": class_name} if class_name else {}
        self._memo[index] = result
        for key, item in raw.items():
            if key in {"$class", "$classname", "$classes"} or key.startswith("$"):
                continue
            result[key] = self.decode(item)
        return result

    @staticmethod
    def _decode_primitive(value: Any) -> Any:
        if isinstance(value, (dt.datetime, dt.date)):
            return value
        return value

    def root(self) -> Any:
        if "root" not in self.top:
            raise ArchiveError("archive root missing")
        return self.decode(self.top["root"])

    def root_field(self, field: str) -> Any:
        """Decode one root property without traversing unrelated secrets."""
        if "root" not in self.top:
            raise ArchiveError("archive root missing")
        index = self._uid_index(self.top["root"])
        if index is None or index < 0 or index >= len(self.objects):
            raise ArchiveError("archive root reference is invalid")
        raw = self.objects[index]
        if not isinstance(raw, dict) or field not in raw:
            return None
        return self.decode(raw[field])


def load_archive(path: Path, *, max_bytes: int = 32 * 1024 * 1024) -> Any:
    """Read one property-list archive without writing or mutating it."""

    try:
        if path.stat().st_size > max_bytes:
            raise ArchiveError("archive byte limit exceeded")
        archive = plistlib.loads(path.read_bytes())
    except ArchiveError:
        raise
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ArchiveError("cached archive could not be read") from exc
    if not isinstance(archive, dict):
        raise ArchiveError("cached archive has an unexpected format")
    return KeyedArchiveDecoder(archive).root()


def load_archive_field(path: Path, field: str, *, max_bytes: int = 32 * 1024 * 1024) -> Any:
    """Read and decode a single root field, leaving unrelated fields untouched."""
    try:
        if path.stat().st_size > max_bytes:
            raise ArchiveError("archive byte limit exceeded")
        archive = plistlib.loads(path.read_bytes())
    except ArchiveError:
        raise
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ArchiveError("cached archive could not be read") from exc
    if not isinstance(archive, dict):
        raise ArchiveError("cached archive has an unexpected format")
    return KeyedArchiveDecoder(archive).root_field(field)


def json_safe(value: Any) -> Any:
    """Convert already-approved values into JSON-compatible primitives."""

    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None
