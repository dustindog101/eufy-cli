"""Command-line interface for local, cache-backed Eufy information."""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import replace
import json
import sys
from typing import Any

from . import __version__
from .archive import ArchiveError
from .backend import BackendError, build_payload, run_backend
from .cache import AppCache
from .discovery import DiscoveryError, discover
from .events import EventError, parse_since, read_cache_events, read_local_events
from . import local_config, session_transfer


class CliError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eufy", description="Local Eufy doorbell control using private local metadata. No new login or cloud calls.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  eufy status --live                         Fresh LAN motion status
  eufy configure --from-app                 One-time private identifier import
  eufy session export PRIVATE_FILE.json      Export a portable private profile
  eufy session import PRIVATE_FILE.json      Import without app access or login
  eufy refresh                               Reload local identifiers + fresh motion
  eufy motion off --shared-admin --dry-run   Preview only
  eufy motion off --shared-admin             Turn motion off and verify
  eufy motion on --shared-admin              Turn motion on and verify
  eufy limits                                Capabilities and limits (JSON)

Motion changes leave the requested state in place. --shared-admin asserts
Admin permission shown in the app when the numeric cache role is unknown.
Event history is experimental: empty output here does not prove no events.
Configured commands do not read the app. Refresh does not update app files.
JSON for command results; human-readable help via eufy help COMMAND.""",
    )
    parser.add_argument("--version", action="version", version=f"eufy {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    help_command = commands.add_parser("help", help="show help without accessing the app cache")
    help_command.add_argument("topic", nargs="?", choices=("devices", "status", "refresh", "discover", "events", "motion", "limits", "configure", "session"))
    session = commands.add_parser("session", help="export/import private standalone identifiers; no login or network",
        description="Move the configured local profile between trusted machines. The JSON file contains private device/account identifiers, not login cookies, passwords or tokens. Transfer it privately. Import validates locally without contacting the camera or app.")
    session_actions = session.add_subparsers(dest="session_action", required=True)
    export = session_actions.add_parser("export", help="save an owner-private portable profile file")
    export.add_argument("file", help="private output JSON file (never stdout)")
    export.add_argument("--overwrite", action="store_true", help="replace an existing export file; never the active profile")
    import_command = session_actions.add_parser("import", help="validate and atomically install a portable profile")
    import_command.add_argument("file", help="previously exported profile JSON file")
    import_command.add_argument("--replace", action="store_true", help="explicitly replace an existing local profile after validation")
    configure = commands.add_parser("configure", help="import identifiers or refresh a verified LAN endpoint",
        description="Copy only needed identifiers from existing saved app metadata, or refresh an existing standalone profile's verified LAN endpoint. No login, cloud calls or device changes.")
    config_source = configure.add_mutually_exclusive_group(required=True)
    config_source.add_argument("--from-app", action="store_true", help="explicit one-time import of existing saved app metadata")
    config_source.add_argument("--host", help="update standalone HomeBase IP and peer port after matching LAN discovery; no app access")
    config_source.add_argument("--from-discovery", action="store_true", help="refresh the standalone HomeBase endpoint from one unique identity-matched broadcast reply")
    configure.add_argument("--device", help="cached device alias")
    configure.add_argument("--replace", action="store_true", help="replace existing config with a fresh one-time import")
    commands.add_parser("limits", help="show verified features, requirements and limits (no network)")
    refresh = commands.add_parser("refresh", help="reload local identifiers and read fresh LAN motion",
        description="Reload the CLI private config, then query fresh motion over LAN. App files are never read by this command. This does not update the app's own cache.",
        epilog="With standalone config, change IP via eufy configure --host PRIVATE_IP (identity checked over LAN). Reimport identifiers only if needed using configure --from-app --replace. Refresh does not contact Eufy services.")
    refresh.add_argument("--device", help="cached device alias")
    refresh.add_argument("--cache-only", action="store_true", help="only reload saved local identifiers; no network, metadata stays stale")
    refresh.add_argument("--discover", action="store_true", help="discover the current peer endpoint before a live LAN read")

    commands.add_parser("devices", help="list redacted devices from the app cache")

    status = commands.add_parser("status", help="show cached state, or fresh motion with --live", description="Without --live all values come from saved app metadata and are stale. --live queries fresh motion only; other fields remain cached.")
    status.add_argument("--device", help="cached device alias")
    status.add_argument("--live", action="store_true", help="read motion state from the local station over LAN")
    status.add_argument("--discover", action="store_true", help="discover the current peer endpoint before a live LAN read")

    discovery = commands.add_parser("discover", help="send one local UDP discovery lookup")
    discovery.add_argument("--host", help="private IPv4 address for directed lookup")
    discovery.add_argument("--timeout", type=float, default=3.0, help="reply window, 0.1–10 seconds (default: 3)")

    events = commands.add_parser("events", help="experimental history query (currently empty despite app events)", description="History is unresolved on this installation. Empty local output must not be interpreted as proof of no events. App logs are currently opaque.", epilog="Limits: 1–20 items, start date up to 30 days ago; ISO date/timestamp. No video playback/download.")
    events.add_argument("--device", help="cached device alias")
    events.add_argument("--limit", type=int, default=20, help="maximum events, 1–20")
    events.add_argument("--since", help="ISO date or timestamp")
    events.add_argument("--source", choices=("local", "cache"), default="local", help="local station history (default) or app notification logs")

    motion = commands.add_parser("motion", help="change motion detection with fresh readback", description="T8210 + T8010 only. Requires a fresh original state, command acknowledgement, and fresh readback. Actual commands leave the requested state in place.", epilog="Use --shared-admin only when the app reports Admin permission. Explicit Guest roles are blocked. --dry-run uses stale cached state and sends no network request.")
    motion.add_argument("action", choices=("on", "off"))
    motion.add_argument("--device", help="cached device alias")
    motion.add_argument("--shared-admin", action="store_true", help="assert the app reports Admin permission when the numeric cached role is unknown")
    motion.add_argument("--dry-run", action="store_true", help="show a preview without contacting the device")
    return parser


def _emit(payload: dict[str, Any], code: int = 0) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")
    return code


def _fail(code: str, message: str, exit_code: int = 2) -> int:
    return _emit({"ok": False, "error": {"code": code, "message": message}}, exit_code)


def _cache() -> AppCache:
    if not local_config.is_configured():
        raise CliError("local_config_missing", "Run eufy configure --from-app once, or restore your private local config; normal commands never read app files")
    try:
        return local_config.load()
    except local_config.LocalConfigError as exc:
        raise CliError("local_config_invalid", str(exc)) from exc


def _selected(cache: AppCache, alias: str | None):
    try:
        return cache.select(alias)
    except ArchiveError as exc:
        raise CliError("device_selection_required", str(exc)) from exc


def _matched_endpoint(probe: dict[str, Any], alias: str, *, host: str | None = None) -> tuple[str, int]:
    responders = probe.get("responders")
    if not isinstance(responders, list):
        responders = []
    endpoints: set[tuple[str, int]] = set()
    for row in responders:
        if not isinstance(row, dict) or row.get("matched_cached_device") is not True or row.get("device_alias") != alias:
            continue
        row_host, port = row.get("host"), row.get("port")
        if host is not None and row_host != host:
            continue
        if not isinstance(row_host, str) or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise CliError("station_endpoint_invalid", "A matching discovery reply contained an invalid endpoint; endpoint was not used")
        endpoints.add((row_host, port))
    if not endpoints:
        raise CliError("station_identity_not_verified", "No discovery reply matched the configured station; endpoint was not refreshed")
    if len(endpoints) != 1:
        raise CliError("station_endpoint_ambiguous", "Multiple matching station endpoints were found; endpoint was not refreshed")
    return next(iter(endpoints))


def _motion_value(device: Any) -> bool | None:
    status = device.public().get("status", {})
    motion = status.get("motion_detection", {})
    return motion.get("value") if isinstance(motion, dict) else None


def _fresh_motion(result: Any) -> bool | None:
    if not isinstance(result, dict):
        return None
    for key in ("motion_detection", "motionDetection", "motion_detection_enabled", "enabled", "value"):
        value = result.get(key)
        if isinstance(value, dict):
            value = value.get("value", value.get("enabled"))
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
    return None


def _limits() -> dict[str, Any]:
    return {
        "ok": True,
        "verified_pair": {"doorbell": "T8210", "doorbell_firmware": "2.2.4.6", "homebase": "T8010", "homebase_firmware": "3.4.2.6h"},
        "working": ["saved_metadata", "lan_discovery", "fresh_motion_status", "motion_on_off_with_readback"],
        "unresolved": {"event_history": "Local queries return empty despite app events; completeness unverified", "cached_logs": "App notification logs are opaque"},
        "unsupported": ["live_video", "recording_playback", "recording_download", "other_settings", "snooze", "security_modes", "discovery_only_control_bootstrap"],
        "metadata": {"standalone_configured": local_config.is_configured(), "app_cache_read_in_standalone_mode": False, "reloaded_each_command": True, "app_cache_updated_by_cli": False, "refresh_live_fields": ["motion_detection"], "app_must_be_running": False, "saved_app_cache_required": False, "one_time_import_uses_saved_app_metadata": True, "standalone_config_required": True, "cloud_dsk_required": False, "known_metadata_expiry_limit": None, "expiry_note": "No DSK expiry dependency; continued access after permission, identity, firmware or network changes is not guaranteed"},
        "network": {"cloud_calls": False, "fresh_login": False, "transport": "UDP to exact cached RFC1918 HomeBase IP", "same_homebase_lan_required": True, "explicit_live_discovery": "status --live --discover or refresh --discover adds one identity-matched broadcast; profile unchanged", "wire_protocol": "proprietary Eufy P2P"},
        "budgets": {"operation_seconds": 13, "cleanup_seconds": 1.5, "bridge_timeout_seconds": 20, "event_items_max": 20, "event_since_days_max": 30, "discovery_timeout_seconds": [0.1, 10], "live_discovery_seconds": 2},
        "writes": {"requires_admin": True, "unknown_shared_role_requires": "--shared-admin", "explicit_guest_blocked": True, "reads_original_and_result": True, "leaves_requested_state": True, "no_op_when_already_matching": True, "automatic_restore_for_normal_commands": False},
        "output": {"normal_commands": "JSON", "help": "human readable", "success_exit": 0, "errors_exit": 2},
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "session":
        try:
            if args.session_action == "export":
                path = session_transfer.export_session(args.file, overwrite=args.overwrite)
            else:
                path = session_transfer.import_session(args.file, replace=args.replace)
            return {"ok": True, "operation": "session_" + args.session_action, "file": str(path),
                    "contents": "standalone_profile", "network_used": False, "login_performed": False,
                    "login_tokens_included": False, "encrypted": False}
        except session_transfer.SessionTransferError as exc:
            raise CliError("session_transfer_failed", str(exc)) from exc
    discover_for_read = getattr(args, "discover", False)
    if args.command == "status" and discover_for_read and not getattr(args, "live", False):
        raise CliError("invalid_option_combination", "status --discover requires --live")
    if args.command == "refresh" and discover_for_read and getattr(args, "cache_only", False):
        raise CliError("invalid_option_combination", "refresh --discover cannot be used with --cache-only")
    if args.command == "configure":
        try:
            if args.host is not None:
                cache = local_config.load()
                device = _selected(cache, args.device)
                probe = discover(host=args.host, timeout=2, cache=cache)
                endpoint_host, endpoint_port = _matched_endpoint(probe, device.alias, host=args.host)
                path = local_config.update_endpoint(endpoint_host, endpoint_port)
            elif getattr(args, "from_discovery", False):
                cache = local_config.load()
                device = _selected(cache, args.device)
                probe = discover(timeout=2, cache=cache)
                endpoint_host, endpoint_port = _matched_endpoint(probe, device.alias)
                path = local_config.update_endpoint(endpoint_host, endpoint_port)
            else:
                device = _selected(AppCache.load(), args.device)
                path = local_config.save_from_device(device, replace=args.replace)
            return {"ok": True, "source": "standalone_config", "config_path": str(path), "device_alias": device.alias, "app_required_for_normal_commands": False, "login_performed": False, "tokens_or_cloud_keys_copied": False}
        except (ArchiveError, local_config.LocalConfigError, BackendError, DiscoveryError, OSError) as exc:
            if isinstance(exc, OSError):
                raise CliError("local_config_write_failed", "private local config could not be saved") from exc
            raise CliError("local_config_import_failed", str(exc)) from exc
    if args.command == "limits":
        return _limits()
    if args.command == "discover":
        try:
            try:
                cache = _cache()
            except CliError:
                cache = None
            result = discover(host=args.host, timeout=args.timeout, cache=cache)
            return {"ok": True, **result}
        except DiscoveryError as exc:
            raise CliError("discovery_failed", str(exc)) from exc

    cache = _cache()
    if args.command == "devices":
        return {"ok": True, "source": getattr(cache, "metadata_source", "app_cache"), "stale": True, "devices": [device.public() for device in cache.devices]}

    if args.command in ("status", "refresh"):
        device = _selected(cache, args.device)
        public = device.public()
        is_refresh = args.command == "refresh"
        live = not getattr(args, "cache_only", False) if is_refresh else getattr(args, "live", False)
        output = {"ok": True, "device": public}
        if is_refresh:
            output["refresh"] = {"metadata_reloaded": True, "metadata_source": getattr(cache, "metadata_source", "app_cache"), "app_refresh_triggered": False, "app_cache_modified": False, "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(), "metadata_stale": True, "fresh_fields": ["motion_detection"] if live else []}
        if not live:
            return output
        try:
            live_device = device
            if discover_for_read:
                try:
                    probe = discover(timeout=2, cache=cache)
                except DiscoveryError as exc:
                    raise CliError("discovery_failed", str(exc)) from exc
                endpoint_host, endpoint_port = _matched_endpoint(probe, device.alias)
                live_device = replace(device, station={**device.station, "ip_addr": endpoint_host, "p2p_port": endpoint_port})
            response = run_backend(build_payload(live_device, "motion.get"))
        except BackendError as exc:
            raise CliError(exc.code, str(exc)) from exc
        result = response.get("result")
        fresh = isinstance(result, dict) and result.get("fresh") is True and result.get("source") == "local-p2p"
        if not fresh:
            current = None
        else:
            current = _fresh_motion(result)
        public["live_motion"] = {
            "source": "local_p2p" if fresh else "unavailable",
            "available": current is not None,
            "value": current,
            "stale": current is None,
        }
        return output

    if args.command == "events":
        if not 1 <= args.limit <= 20:
            raise CliError("invalid_limit", "--limit must be between 1 and 20")
        try:
            since = parse_since(args.since)
        except EventError as exc:
            raise CliError(exc.code, str(exc)) from exc
        if args.source == "cache":
            try:
                return {"ok": True, **read_cache_events(cache, since=since, limit=args.limit)}
            except EventError as exc:
                raise CliError(exc.code, str(exc)) from exc
        device = _selected(cache, args.device)
        try:
            return {"ok": True, **read_local_events(cache, device, since=since, limit=args.limit)}
        except EventError as exc:
            raise CliError(exc.code, str(exc)) from exc

    if args.command == "motion":
        device = _selected(cache, args.device)
        target = args.action == "on"
        role = device.role
        shared_admin = role == "unknown" and getattr(args, "shared_admin", False)
        if args.dry_run:
            allowed = role == "admin" or shared_admin
            reason = None
            if role == "guest":
                reason = "cached Guest role cannot change device settings"
            elif role != "admin" and not shared_admin:
                reason = "cached role is unknown; refusing device settings changes"
            return {
                "ok": True,
                "dry_run": True,
                "operation": "set_motion_detection",
                "device_alias": device.alias,
                "current_cached_value": _motion_value(device),
                "target_value": target,
                "cached_role": role,
                "write_available": allowed,
                "write_permitted_by_cached_role": role == "admin",
                "admin_asserted": shared_admin,
                "reason": reason,
            }
        if role == "guest":
            raise CliError("guest_settings_forbidden", "cached Guest role cannot change device settings")
        if role != "admin" and not shared_admin:
            raise CliError("role_unknown", "cached role is unknown; refusing device settings changes")
        try:
            payload = build_payload(device, "motion.set", motion_value=target)
            if shared_admin:
                payload["role"] = "admin"  # User assertion only; cached wire identity stays intact.
            result = run_backend(payload)["result"]
            return {"ok": True, "device": device.alias, "motion_detection": result["enabled"], "previous": result["previous"], "changed": result["changed"], "fresh": True, "source": "local_p2p"}
        except BackendError as exc:
            raise CliError(exc.code, str(exc)) from exc

    raise CliError("unknown_command", "unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        if args.topic:
            parser.parse_args([args.topic, "--help"])
        else:
            parser.print_help()
        return 0
    try:
        return _emit(run(args))
    except CliError as exc:
        return _fail(exc.code, str(exc))
    except Exception:
        # Keep unexpected parser/backend details (which can contain private
        # values) out of CLI output.
        return _fail("internal_error", "command could not be completed")
