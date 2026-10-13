"""Safe views over read-only Eufy app caches."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .archive import ArchiveError, load_archive


DEFAULT_GROUP_CONTAINER = Path.home() / "Library/Group Containers/group.security-batterycam"
STATION_CLASS_HINTS = ("homebase", "station")
MOTION_KEYS = {
    "motiondetection",
    "motiondetectionenabled",
    "motionenable",
    "motionenabled",
    "motiondetect",
    "motiondetectenabled",
    "pirenabled",
    "pirdetection",
    "devicemotiondetection",
}
PARAM_PROPERTY_IDS = {1011: "motion_detection", 1101: "battery_percent"}
SAFE_STATUS_KEYS = {
    "online", "isonline", "deviceonline", "battery", "batterylevel",
    "batterypercent", "charging", "ischarging", "temperature", "recording",
    "storage", "storagestatus", "sdcardstatus", "connectionstatus",
}
SAFE_STATUS_WORDS = {
    "online", "offline", "connected", "disconnected", "charging", "charged",
    "normal", "low", "full", "empty", "recording", "idle", "unknown",
}


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _walk(value: Any, *, max_depth: int = 10) -> Iterator[dict[str, Any]]:
    seen: set[int] = set()

    def visit(node: Any, depth: int) -> Iterator[dict[str, Any]]:
        if depth > max_depth or not isinstance(node, (dict, list, tuple)):
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(node, dict):
            yield node
            for child in node.values():
                yield from visit(child, depth + 1)
        else:
            for child in node:
                yield from visit(child, depth + 1)

    yield from visit(value, 0)


def _text(value: Any, *, limit: int = 100) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > limit or "://" in value:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value


def _model(value: Any) -> str | None:
    text = _text(value, limit=64)
    if not text or not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        return None
    return text


def _alias_name(value: Any, serial: str | None = None) -> str | None:
    name = _text(value, limit=80)
    if not name or name.casefold() == (serial or "").casefold():
        return None
    if re.search(r"(?:token|password|secret|authorization|bearer|https?://)", name, re.IGNORECASE):
        return None
    if re.fullmatch(r"[0-9a-fA-F:.-]{12,}", name) or re.fullmatch(r"[A-Za-z0-9_=-]{24,}", name):
        return None
    try:
        ipaddress.ip_address(name)
        return None
    except ValueError:
        return name


def _safe_scalar(value: Any) -> bool | int | float | str | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) <= 1000:
        return value
    if isinstance(value, float) and abs(value) <= 1000:
        return value
    if isinstance(value, str) and value.lower() in SAFE_STATUS_WORDS:
        return value.lower()
    return None


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "on", "enabled", "1"}:
            return True
        if text in {"false", "no", "off", "disabled", "0"}:
            return False
    return None


def _model_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Return a small allowlisted projection for CLI display."""
    fields: dict[str, Any] = {}
    direct = {
        "model": ("device_model", "model"),
        "firmware": ("main_sw_version", "firmware_version", "software_version"),
        "hardware": ("main_hw_version", "hardware_version"),
        "category": ("category", "categoryType"),
    }
    for output, candidates in direct.items():
        for candidate in candidates:
            value = record.get(candidate)
            if output == "model":
                safe = _model(value)
            elif output in {"firmware", "hardware"}:
                safe = _text(value, limit=64)
            else:
                safe = _safe_scalar(value)
            if safe is not None:
                fields[output] = safe
                break

    status: dict[str, Any] = {}
    motion_state: bool | None = None
    motion_supported = False
    for node in _walk(record):
        property_id = node.get("_param_type", node.get("param_type"))
        if isinstance(property_id, str) and property_id.isdecimal():
            property_id = int(property_id)
        if property_id == 1011:
            motion_supported = True
            parsed_motion = _bool_value(node.get("_param_value", node.get("param_value")))
            if parsed_motion is not None:
                motion_state = parsed_motion
        elif property_id == 1101:
            battery = _safe_scalar(node.get("_param_value", node.get("param_value")))
            if battery is not None and 0 <= battery <= 100:
                status["battery_percent"] = battery
        for key, value in node.items():
            normalized = _key(key)
            if normalized in MOTION_KEYS:
                motion_supported = True
                parsed = _bool_value(value)
                if parsed is not None:
                    motion_state = parsed
            if normalized in SAFE_STATUS_KEYS:
                parsed_status = _safe_scalar(value)
                if parsed_status is not None:
                    status[normalized] = parsed_status
    fields["capabilities"] = {"motion_detection": "cached" if motion_supported else "unknown"}
    fields["status"] = {
        "source": "app_cache",
        "stale": True,
        "motion_detection": {"value": motion_state, "stale": True},
        "values": status,
    }
    return fields


def _member_records(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    member = record.get("member")
    if isinstance(member, dict):
        yield member
    elif isinstance(member, list):
        for item in member:
            if isinstance(item, dict):
                yield item


def _role(*records: dict[str, Any] | None) -> str:
    for record in records:
        if not isinstance(record, dict):
            continue
        members = list(_member_records(record))
        if not members and ("_member_type" in record or "member_type" in record):
            members = [record]
        for member in members:
            role = member.get("_member_type", member.get("member_type"))
            if isinstance(role, str) and role.strip().lower() in {"guest", "admin", "owner"}:
                return "guest" if role.strip().lower() == "guest" else "admin"
    return "unknown"


def _serial(record: dict[str, Any]) -> str | None:
    value = record.get("device_sn") or record.get("station_sn")
    return value if isinstance(value, str) and value else None


def _is_station(record: dict[str, Any]) -> bool:
    class_name = str(record.get("__class__", "")).lower()
    # Some doorbell records also carry a p2p_did. Prefer the app model class
    # and the HomeBase device-type code over the mere presence of that field.
    device_type = record.get("device_type")
    return any(hint in class_name for hint in STATION_CLASS_HINTS) or device_type == 10002


@dataclass
class CachedDevice:
    """Internal device record; `record` and `station` may contain secrets."""

    serial: str
    record: dict[str, Any]
    station: dict[str, Any] | None
    role: str
    cache_mtime: float
    alias: str = ""
    metadata_source: str = "app_cache"

    @property
    def model(self) -> str | None:
        return _model(self.record.get("device_model"))

    def public(self) -> dict[str, Any]:
        fields = _model_fields(self.record)
        fields["status"]["source"] = self.metadata_source
        if self.metadata_source == "standalone_config" and self.model == "T8210" and self.station and self.station.get("device_model") == "T8010":
            fields["capabilities"]["motion_detection"] = "local_p2p"
        return {
            "alias": self.alias,
            "model": fields.get("model"),
            "firmware": fields.get("firmware"),
            "hardware": fields.get("hardware"),
            "category": fields.get("category"),
            "role": self.role,
            "capabilities": fields["capabilities"],
            "status": fields["status"],
            "cache": {
                "source": self.metadata_source,
                "stale": True,
                "cached_at": dt.datetime.fromtimestamp(self.cache_mtime, dt.timezone.utc).isoformat(),
            },
        }


class AppCache:
    """Read the app's saved archives and expose redacted views."""

    def __init__(self, group_container: Path = DEFAULT_GROUP_CONTAINER):
        self.group_container = group_container
        self.devices: list[CachedDevice] = []
        self.archive_count = 0

    @classmethod
    def load(cls, group_container: Path = DEFAULT_GROUP_CONTAINER) -> "AppCache":
        cache = cls(group_container)
        cache._load()
        return cache

    def _load(self) -> None:
        archives = sorted(self.group_container.glob("*_BCDevicesMan"), key=lambda p: p.stat().st_mtime)
        if not archives:
            raise ArchiveError("Eufy app device cache not found")
        selected: dict[str, tuple[dict[str, Any], dict[str, Any] | None, float, str]] = {}
        for path in archives:
            try:
                root = load_archive(path)
            except ArchiveError:
                continue
            self.archive_count += 1
            if not isinstance(root, dict):
                continue
            records: list[dict[str, Any]] = []
            for node in _walk(root):
                serial = _serial(node)
                if serial and ("device_model" in node or _is_station(node)):
                    records.append(node)
            stations = [node for node in records if _is_station(node)]
            for record in records:
                serial = _serial(record)
                if not serial or _is_station(record):
                    continue
                parent = record.get("parent_sn") or record.get("station_sn")
                station = next((x for x in stations if _serial(x) == parent), None)
                if station is None:
                    station = next((x for x in stations if any(_serial(n) == serial for n in _walk(x.get("devices", [])))), None)
                if station is None:
                    station = stations[0] if len(stations) == 1 else None
                mtime = path.stat().st_mtime
                old = selected.get(serial)
                score = self._record_score(record, station)
                old_score = self._record_score(old[0], old[1]) if old else -1
                if old is None or score > old_score or (score == old_score and mtime > old[2]):
                    selected[serial] = (record, station, mtime, path.name)

        devices: list[CachedDevice] = []
        for serial, (record, station, mtime, _source) in selected.items():
            # Suppress bridge/station entries from the selectable camera list.
            model = _model(record.get("device_model"))
            class_name = str(record.get("__class__", "")).lower()
            if any(hint in class_name for hint in STATION_CLASS_HINTS):
                continue
            role = _role(record, station)
            devices.append(CachedDevice(serial, record, station, role, mtime))
        devices.sort(key=lambda item: (item.model or "", item.alias))
        self._assign_aliases(devices)
        self.devices = devices
        if not devices:
            raise ArchiveError("no cached camera device records found")

    @staticmethod
    def _record_score(record: dict[str, Any], station: dict[str, Any] | None) -> int:
        score = sum(value not in (None, "", [], {}) for value in record.values())
        if station:
            score += sum(station.get(key) not in (None, "", [], {}) for key in ("p2p_did", "app_conn", "dsk_key", "member", "device_channel"))
        return score

    @staticmethod
    def _assign_aliases(devices: list[CachedDevice]) -> None:
        used: dict[str, int] = {}
        for index, device in enumerate(devices, start=1):
            name = _alias_name(device.record.get("device_name"), device.serial)
            if name:
                alias_base = name
            elif device.model:
                alias_base = device.model
            else:
                alias_base = f"device-{index}"
            count = used.get(alias_base.casefold(), 0) + 1
            used[alias_base.casefold()] = count
            device.alias = alias_base if count == 1 else f"{alias_base} #{count}"

    def select(self, alias: str | None) -> CachedDevice:
        if alias:
            matches = [device for device in self.devices if device.alias.casefold() == alias.casefold()]
            if len(matches) == 1:
                return matches[0]
            available = ", ".join(device.alias for device in self.devices)
            raise ArchiveError(f"device selection did not match; choose one of: {available}")
        t8210 = [device for device in self.devices if device.model == "T8210"]
        if len(t8210) == 1:
            return t8210[0]
        if len(self.devices) == 1:
            return self.devices[0]
        available = ", ".join(device.alias for device in self.devices)
        raise ArchiveError(f"choose a device with --device; available aliases: {available}")

    def match_station_did(self, did: str) -> CachedDevice | None:
        if not did:
            return None
        for device in self.devices:
            if isinstance(device.station, dict) and device.station.get("p2p_did") == did:
                return device
        return None

    def events_aliases(self) -> dict[str, str]:
        """Return an internal-only serial-to-display-alias lookup."""
        return {device.serial: device.alias for device in self.devices}
