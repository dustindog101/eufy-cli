"""Small local subprocess bridge to the sibling Node protocol backend.

Secrets required by the backend are carried only on stdin. They are never put
in argv, CLI output, or error messages.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .cache import CachedDevice


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = PROJECT_ROOT / "src" / "local-backend.mjs"
MAX_BACKEND_OUTPUT = 4 * 1024 * 1024
RFC1918_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _member(station: dict[str, Any]) -> dict[str, Any]:
    member = station.get("member")
    if isinstance(member, list):
        member = next((item for item in member if isinstance(item, dict)), {})
    if not isinstance(member, dict):
        member = {}
    return member


def _safe_host(value: Any) -> str:
    if not isinstance(value, str):
        raise BackendError("local_endpoint_unavailable", "cached local station address is unavailable")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise BackendError("local_endpoint_invalid", "cached local station address is invalid") from exc
    if address.version != 4 or not (address.is_loopback or address.is_link_local or any(address in network for network in RFC1918_NETWORKS)):
        raise BackendError("local_endpoint_invalid", "cached station address is not a private IPv4 address")
    return str(address)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def build_payload(device: CachedDevice, operation: str, *, query: dict[str, Any] | None = None, motion_value: bool | None = None) -> dict[str, Any]:
    """Create the small, explicit bridge schema. This object can contain secrets."""
    if operation not in {"events.local", "motion.get", "motion.set"}:
        raise BackendError("unsupported_operation", "requested local operation is unavailable")
    station = device.station
    if not isinstance(station, dict):
        raise BackendError("station_metadata_unavailable", "cached station metadata is unavailable")
    member = _member(station)
    if not member:
        member = _member(device.record)
    local_host = _safe_host(station.get("ip_addr"))
    # DSK protects cloud rendezvous; directed LAN lookup does not use it.
    # Do not forward cached cloud lookup keys into the strictly local backend.
    key = ""
    expiration = None

    station_sn = _str(station.get("device_sn")) or _str(station.get("station_sn"))
    device_sn = device.serial
    if not station_sn:
        raise BackendError("station_metadata_unavailable", "cached station identity is unavailable")
    target = {
        "device_sn": device_sn,
        "device_name": _str(device.record.get("device_name")) or device.alias,
        "device_model": _str(device.record.get("device_model")) or "",
        "device_type": device.record.get("device_type", 0),
        "device_channel": device.record.get("device_channel", 0),
        "station_sn": station_sn,
        "main_sw_version": _str(device.record.get("main_sw_version")) or "",
        "main_hw_version": _str(device.record.get("main_hw_version")) or "",
        "params": [],
    }
    station_target = {
        "device_sn": device_sn,
        "device_type": target["device_type"],
        "device_channel": target["device_channel"],
    }
    station_raw = {
        "station_sn": station_sn,
        "device_type": station.get("device_type", 0),
        "station_model": _str(station.get("station_model")) or _str(station.get("device_model")) or "",
        "main_sw_version": _str(station.get("main_sw_version")) or "",
        "main_hw_version": _str(station.get("main_hw_version")) or "",
        "p2p_did": _str(station.get("p2p_did")) or "",
        "app_conn": "",  # Explicitly disable any cloud lookup address.
        "params": [],
        "devices": [station_target],
        "member": {
            "admin_user_id": member.get("_admin_user_id", member.get("admin_user_id", "")),
            "member_user_id": member.get("_member_user_id", member.get("member_user_id", "")),
            "member_type": member.get("_member_type", member.get("member_type", -1)),
        },
    }
    payload: dict[str, Any] = {
        "version": 1,
        "operation": operation,
        "role": device.role,
        "station": {
            "raw": station_raw,
            "local": {"host": local_host, "dskKey": key, "dskExpirationMs": expiration},
        },
        "device": target,
    }
    if operation == "motion.set":
        if not isinstance(motion_value, bool):
            raise BackendError("invalid_motion_value", "motion value must be boolean")
        payload["value"] = motion_value
    if query is not None:
        payload["query"] = query
    return payload


def run_backend(payload: dict[str, Any], *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Invoke one local backend request with its input delivered through stdin."""
    node = shutil.which("node")
    if not node:
        raise BackendError("node_unavailable", "Node.js is required for local station queries")
    if not BACKEND_PATH.is_file():
        raise BackendError("backend_unavailable", "local protocol backend is not installed")
    try:
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BackendError("bridge_payload_invalid", "cached station metadata could not be prepared") from exc
    try:
        completed = subprocess.run(
            [node, str(BACKEND_PATH)],
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=PROJECT_ROOT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendError("backend_timeout", "local station query timed out") from exc
    except OSError as exc:
        raise BackendError("backend_start_failed", "local protocol backend could not start") from exc
    output = completed.stdout or b""
    if len(output) > MAX_BACKEND_OUTPUT:
        raise BackendError("backend_output_too_large", "local station returned too much data")
    try:
        response = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackendError("backend_response_invalid", "local protocol backend returned an invalid response") from exc
    if not isinstance(response, dict):
        raise BackendError("backend_response_invalid", "local protocol backend returned an invalid response")
    if response.get("ok") is not True:
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = "local_query_failed"
        # Never echo backend message text, which could accidentally contain a secret.
        details = error.get("details", {}) if isinstance(error, dict) else {}
        return_code = details.get("returnCode") if isinstance(details, dict) else None
        message = {
            "PROPERTY_UNAVAILABLE": "station returned no fresh motion state",
            "WRITE_ACK_TIMEOUT": "motion acknowledgement timed out; setting may have changed, check status --live",
            "WRITE_READBACK_UNAVAILABLE": "motion was acknowledged but readback failed; check status --live",
            "WRITE_READBACK_MISMATCH": "fresh motion state did not match the requested setting",
            "LOCAL_TIMEOUT": "local station query timed out",
            "LOCAL_CONNECT_FAILED": "could not establish a local station connection",
            "CACHED_SESSION_EXPIRED": "cached local station key has expired",
            "NETWORK_BLOCKED": "a network destination outside the pinned local station was blocked",
        }.get(code, "local station query failed")
        if isinstance(return_code, int) and not isinstance(return_code, bool):
            message = f"local station rejected the command (code {return_code})"
        raise BackendError(code, message)
    return response
