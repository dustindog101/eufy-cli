"""Normalize local Eufy event records without exposing content or media links."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .backend import BackendError, build_payload, run_backend
from .cache import AppCache, CachedDevice


class EventError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


TIME_KEYS = ("timestamp", "time", "created_at", "create_time", "event_time", "date", "record_time", "startTime", "start_time")
END_TIME_KEYS = ("endTime", "end_time")
TYPE_KEYS = ("event_type", "eventType", "type", "alarm_type", "alarmType", "detection_type", "detectionType")
DEVICE_KEYS = ("device_sn", "deviceSN", "device_id", "deviceId", "serial", "station_sn")
CONTAINER_KEYS = ("events", "records", "items", "history", "data", "result")
SAFE_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")


def parse_calendar_since(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    try:
        date = dt.date.fromisoformat(value)
        if date.isoformat() != value:
            raise ValueError
    except ValueError:
        raise EventError("invalid_since", "Local history selects calendar days; use YYYY-MM-DD.") from None
    if date > dt.date.today():
        raise EventError("invalid_since", "The selected day cannot be in the future.")
    return dt.datetime.combine(date, dt.time.min)


def parse_since(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            parsed = dt.datetime.combine(dt.date.fromisoformat(value), dt.time.min, tzinfo=dt.timezone.utc)
        else:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            parsed = parsed.astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise EventError("invalid_since", "--since must be an ISO date or timestamp") from exc
    if parsed > dt.datetime.now(dt.timezone.utc):
        raise EventError("invalid_since", "--since cannot be in the future")
    return parsed


def _timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        # Tolerate either Unix seconds or milliseconds.
        if number > 10**12:
            number /= 1000
        try:
            return dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
            return _timestamp(number)
        except ValueError:
            pass
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            try:
                date = dt.date.fromisoformat(text[:10])
                return dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc)
            except ValueError:
                return None
    return None


def _first(record: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _records(value: Any, *, max_depth: int = 8, include_time_only: bool = False) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth or not isinstance(node, (dict, list, tuple)):
            return
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, dict):
            if _first(node, TIME_KEYS) is not None and (
                _first(node, TYPE_KEYS) is not None or include_time_only
            ):
                found.append(node)
            for key in CONTAINER_KEYS:
                if key in node:
                    walk(node[key], depth + 1)
            # Also descend into lists nested under unknown wrappers, but do not
            # crawl arbitrary dictionaries where private content may live.
            for child in node.values():
                if isinstance(child, (list, tuple)):
                    walk(child, depth + 1)
        else:
            for child in node:
                walk(child, depth + 1)

    walk(value, 0)
    return found


def normalize_records(
    value: Any,
    aliases: dict[str, str],
    *,
    since: dt.datetime | None = None,
    limit: int = 20,
    fallback_type: str | None = None,
    fallback_alias: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only a timestamp, an enum-like event type, and a cached alias."""
    normalized: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for record in _records(value, include_time_only=fallback_type is not None):
        stamp = _timestamp(_first(record, TIME_KEYS))
        event_type = _first(record, TYPE_KEYS)
        if event_type is None and fallback_type is not None:
            event_type = fallback_type
        if stamp is None or not isinstance(event_type, (str, int)) or isinstance(event_type, bool):
            continue
        if isinstance(event_type, int):
            event_type = f"code_{event_type}"
        event_type = event_type.strip()
        if not SAFE_EVENT_TYPE.fullmatch(event_type):
            continue
        if since is not None and stamp < since:
            continue
        device_key = _first(record, DEVICE_KEYS)
        alias = aliases.get(device_key, fallback_alias) if isinstance(device_key, str) else fallback_alias
        iso = stamp.isoformat()
        event = {"timestamp": iso, "type": event_type, "device_alias": alias}
        end_stamp = _timestamp(_first(record, END_TIME_KEYS))
        if end_stamp is not None:
            event["end_timestamp"] = end_stamp.isoformat()
        frame_count = record.get("frameCount", record.get("frame_count"))
        if isinstance(frame_count, int) and not isinstance(frame_count, bool) and 0 <= frame_count <= 1_000_000:
            event["frame_count"] = frame_count
        normalized[(iso, event_type, alias)] = event
    return sorted(normalized.values(), key=lambda item: item["timestamp"], reverse=True)[:limit]


def _cache_log_available(group_container: Path) -> bool:
    log_dir = group_container / "log"
    return any(log_dir.glob("notification_extension_*.log")) if log_dir.is_dir() else False


def read_cache_events(cache: AppCache, *, since: dt.datetime | None = None, limit: int = 20) -> dict[str, Any]:
    """Parse only recognized structured records from notification logs.

    Current app logs can be opaque/binary. No free-text or URL extraction is
    attempted, and unrecognized data is reported as unavailable.
    """
    log_dir = cache.group_container / "log"
    paths = sorted(log_dir.glob("notification_extension_*.log"), key=lambda path: path.stat().st_mtime, reverse=True) if log_dir.is_dir() else []
    if not paths:
        raise EventError("event_cache_missing", "no app notification log cache was found")
    records: list[Any] = []
    total_bytes = 0
    for path in paths:
        try:
            size = path.stat().st_size
            if size <= 0 or size > 4 * 1024 * 1024 or total_bytes + size > 24 * 1024 * 1024:
                continue
            total_bytes += size
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate.startswith(("{", "[")):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            records.extend(_records(parsed))
    events = normalize_records(records, cache.events_aliases(), since=since, limit=limit)
    if not events:
        raise EventError("event_cache_unrecognized", "notification cache has no supported event records")
    return {
        "source": "app_notification_log_cache",
        "cached": True,
        "authoritative_history": False,
        "complete": False,
        "events": events,
    }


def read_local_events(
    cache: AppCache,
    device: CachedDevice,
    *,
    since: dt.datetime | None = None,
    limit: int = 200,
    counts_only: bool = False,
    end_date: dt.date | None = None,
) -> dict[str, Any]:
    now = dt.datetime.now().astimezone()
    start = since or now
    if (now.date() - start.date()).days > 30:
        raise EventError("date_range_too_large", "local station history is limited to a 30-day date range")
    query = {
        "startDate": start.date().isoformat(),
        "endDate": (end_date or now.date()).isoformat(),
        "maxItems": min(limit, 20 if counts_only else 200),
    }
    try:
        payload = build_payload(device, "events.counts" if counts_only else "events.local", query=query)
        response = run_backend(payload)
    except BackendError as exc:
        raise EventError(exc.code, str(exc)) from exc
    result = response.get("result", response)
    if counts_only:
        if (not isinstance(result, dict) or not isinstance(result.get("counts"), list)
                or len(result["counts"]) > 31 or result.get("scope") != "homebase"
                or result.get("source") != "local-p2p"):
            raise EventError("invalid_counts_response", "HomeBase daily counts were unavailable")
        counts = []
        seen = set()
        for row in result["counts"]:
            try:
                date = dt.date.fromisoformat(row["date"]).isoformat()
                count = row["count"]
                if (date != row["date"] or date in seen or not query["startDate"] <= date <= query["endDate"]
                        or type(count) is not int or not 0 <= count <= 2**53 - 1):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise EventError("invalid_counts_response", "HomeBase daily counts were invalid") from None
            seen.add(date)
            counts.append({"date": date, "count": count})
        return {
            "source": "local_station_daily_counts", "scope": "homebase", "complete": False,
            "count_meaning": "unverified", "event_totals_available": False,
            "counts": sorted(counts, key=lambda row: row["date"], reverse=True),
            "start_date": query["startDate"], "end_date": query["endDate"],
            "note": "HomeBase-reported dates and raw numeric values. Numeric values are not verified event totals and may differ from the app. Missing days are unknown, not zero.",
        }
    if (not isinstance(result, dict) or result.get("source") != "local-p2p"
            or result.get("time_basis") != "homebase_wall_clock" or result.get("timezone_verified") is not False
            or type(result.get("query_exhausted")) is not bool or type(result.get("truncated")) is not bool
            or type(result.get("pages")) is not int or not 1 <= result["pages"] <= 12
            or result.get("partial_reason") not in (None, "limit", "timeout", "query_failed", "cursor_stalled", "page_budget")
            or (not result["truncated"] and not result["query_exhausted"])
            or (result["truncated"] != (result["partial_reason"] is not None))
            or not isinstance(result.get("events"), list) or len(result["events"]) > limit):
        raise EventError("invalid_history_response", "HomeBase recording history was invalid")
    events = []
    for row in result["events"]:
        try:
            stamp = dt.datetime.fromisoformat(row["timestamp"])
            end = dt.datetime.fromisoformat(row["end_timestamp"])
            if (stamp.tzinfo or end.tzinfo or stamp.isoformat() != row["timestamp"]
                    or end.isoformat() != row["end_timestamp"] or end < stamp or row["type"] != "recording"
                    or not query["startDate"] <= stamp.date().isoformat() <= query["endDate"]):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise EventError("invalid_history_response", "HomeBase recording timestamps were invalid") from None
        events.append({"timestamp": row["timestamp"], "end_timestamp": row["end_timestamp"],
                       "type": "recording", "device_alias": device.alias})
    return {
        "source": "local_station_history",
        "cached": False,
        "authoritative_history": False,
        "complete": False,
        "device_alias": device.alias,
        "events": sorted(events, key=lambda row: row["timestamp"]),
        "count": len(events), "start_date": query["startDate"], "end_date": query["endDate"],
        "time_basis": "homebase_wall_clock", "timezone_verified": False,
        "query_exhausted": result["query_exhausted"], "truncated": result["truncated"],
        "partial_reason": result["partial_reason"], "pages": result["pages"],
        "note": "Local recording metadata only. HomeBase clock timezone and agreement with the full app event list are unverified.",
    }
