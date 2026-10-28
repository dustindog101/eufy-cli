"""Consumer commands for standalone local Eufy motion control."""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import replace
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


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                          help="output JSON for scripts")

    def error(self, message):
        # argparse can echo arbitrary argument values. Keep errors private and
        # useful, including when JSON was requested for an invalid invocation.
        raise CliError("invalid_arguments", "Command not recognized or incomplete. Run eufy help or eufy help COMMAND.")


def _parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="eufy", description="Control your Eufy doorbell over your local network.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Everyday use:
  eufy status                  Check fresh motion status
  eufy motion off              Turn motion detection off and verify
  eufy motion on               Turn motion detection on and verify

First use: eufy configure --from-app (Mac), or eufy session import FILE.
If the app shows Admin/Owner: eufy configure --confirm-admin (once).
Use --json for scripts. Use eufy help COMMAND for more options.
Supports T8210 + T8010 motion control; video and other settings unavailable.""",
    )
    parser.add_argument("--version", action="version", version=f"eufy {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    help_command = commands.add_parser("help", help="show help")
    help_command.add_argument("topic", nargs="?", choices=("devices", "status", "refresh", "discover", "events", "motion", "limits", "configure", "session"))
    commands.add_parser("devices", help="list configured devices (offline)")
    status = commands.add_parser("status", help="check fresh motion status",
        description="Read fresh motion status over LAN. Other metadata stays saved. A single configured doorbell is selected automatically.")
    refresh = commands.add_parser("refresh", help="check fresh status (same as status)")
    for command in (status, refresh):
        command.add_argument("--device", help="device alias, when more than one is available")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--live", action="store_true", default=True, help=argparse.SUPPRESS)
        mode.add_argument("--cache-only", action="store_true", help="show saved metadata without network access")
        command.add_argument("--discover", action="store_true", help="look up the identity-matched HomeBase before connecting")
    motion = commands.add_parser("motion", help="turn motion detection on or off",
        description="Change motion detection and verify fresh device readback. Leaves the requested state in place; already-matching state sends no setter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""If the app shows Admin/Owner, confirm once:
  eufy configure --confirm-admin
Guest access cannot change settings.
If acknowledgement/readback fails, check eufy status before another write.""")
    motion.add_argument("action", choices=("on", "off"))
    motion.add_argument("--device", help="device alias, when selection is ambiguous")
    motion.add_argument("--dry-run", action="store_true", help="preview permission and target without network access")
    motion.add_argument("--discover", action="store_true", help="look up the identity-matched HomeBase before connecting")
    motion.add_argument("--shared-admin", action="store_true", help="assert confirmed Admin access for this invocation only")
    configure = commands.add_parser("configure", help="one-time setup and permission confirmation",
        description="Use saved app identifiers on Mac, or maintain an existing private profile. No login, cloud calls or camera setting changes.")
    source = configure.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-app", action="store_true", help="import existing saved Mac app identifiers once")
    source.add_argument("--confirm-admin", action="store_true", help="remember that the app shows Admin/Owner for this profile; never overrides Guest")
    source.add_argument("--clear-admin", action="store_true", help="forget the saved Admin confirmation")
    source.add_argument("--host", help="save a private HomeBase address after matching its identity")
    source.add_argument("--from-discovery", action="store_true", help="save one uniquely identity-matched LAN endpoint")
    configure.add_argument("--device", help="device alias for app import or endpoint lookup")
    configure.add_argument("--replace", action="store_true", help="replace an existing profile during app import; clears prior confirmation")
    session = commands.add_parser("session", help="privately export or import a profile",
        description="Move a private profile between trusted machines without login or network access. The unencrypted file contains device/account identifiers; transfer it privately.")
    actions = session.add_subparsers(dest="session_action", required=True)
    export = actions.add_parser("export", help="save a private profile file")
    export.add_argument("file", help="private output file (never stdout)")
    export.add_argument("--overwrite", action="store_true", help="replace an existing export file")
    import_command = actions.add_parser("import", help="install a private profile file")
    import_command.add_argument("file", help="previously exported profile file")
    import_command.add_argument("--replace", action="store_true", help="replace an existing local profile")
    discovery = commands.add_parser("discover", help="look for HomeBases on your LAN")
    discovery.add_argument("--host", help="private IPv4 address for directed lookup")
    discovery.add_argument("--timeout", type=float, default=2.0, help="reply window, 0.1–10 seconds (default: 2)")
    commands.add_parser("limits", help="show supported features (offline)")
    events = commands.add_parser("events", help="experimental history (unresolved)",
        description="History is unresolved. Empty output does not prove no events. Video playback/download is unavailable.")
    events.add_argument("--device", help="device alias")
    events.add_argument("--limit", type=int, default=20, help="maximum events, 1–20")
    events.add_argument("--since", help="ISO date or timestamp, up to 30 days ago")
    events.add_argument("--source", choices=("local", "cache"), default="local")
    return parser


def _cache() -> AppCache:
    if not local_config.is_configured():
        raise CliError("local_config_missing", "Run eufy session import FILE, or configure --from-app once on macOS; normal commands never read app files")
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


def _connected_request(cache: AppCache, device: Any, operation: str, *,
                       force_discovery: bool = False, motion_value: bool | None = None,
                       admin_asserted: bool = False) -> dict[str, Any]:
    def lookup():
        try:
            probe = discover(timeout=2, cache=cache)
        except DiscoveryError as exc:
            raise CliError("discovery_failed", "HomeBase lookup failed. Check your LAN connection and try eufy status.") from exc
        host, port = _matched_endpoint(probe, device.alias)
        return replace(device, station={**device.station, "ip_addr": host, "p2p_port": port})

    def request(target):
        payload = build_payload(target, operation, motion_value=motion_value)
        if admin_asserted:
            payload["role"] = "admin"  # Assertion only; imported role/wire identities stay intact.
        return run_backend(payload)

    target = lookup() if force_discovery else device
    try:
        return request(target)
    except BackendError as exc:
        # Retry once only for a connection failure. For writes the backend must
        # explicitly prove the setter was never attempted. A bridge timeout,
        # ACK failure, rejection or readback failure never triggers a retry.
        if (force_discovery or exc.code not in {"LOCAL_TIMEOUT", "LOCAL_CONNECT_FAILED"}
                or (operation == "motion.set" and not exc.retry_safe)):
            raise
        return request(lookup())


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
        "network": {"cloud_calls": False, "fresh_login": False, "transport": "UDP to exact cached RFC1918 HomeBase IP", "same_homebase_lan_required": True, "endpoint_recovery": "one identity-matched broadcast after a safe connection failure; --discover forces lookup; profile unchanged", "wire_protocol": "proprietary Eufy P2P"},
        "budgets": {"operation_seconds": 13, "cleanup_seconds": 1.5, "bridge_timeout_seconds": 20, "connection_attempts_max": 2, "event_items_max": 20, "event_since_days_max": 30, "discovery_timeout_seconds": [0.1, 10], "live_discovery_seconds": 2},
        "writes": {"requires_admin": True, "unknown_shared_role_requires": "configure --confirm-admin once, or --shared-admin per invocation", "explicit_guest_blocked": True, "reads_original_and_result": True, "leaves_requested_state": True, "no_op_when_already_matching": True, "automatic_restore_for_normal_commands": False},
        "output": {"normal_commands": "human readable", "automation": "--json", "help": "human readable", "success_exit": 0, "errors_exit": 2},
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
    if args.command == "status" and discover_for_read and getattr(args, "cache_only", False):
        raise CliError("invalid_option_combination", "--discover cannot be used with --cache-only")
    if args.command == "refresh" and discover_for_read and getattr(args, "cache_only", False):
        raise CliError("invalid_option_combination", "refresh --discover cannot be used with --cache-only")
    if args.command == "configure":
        try:
            if args.replace and not args.from_app:
                raise CliError("invalid_option_combination", "--replace is only used with --from-app.")
            if getattr(args, "confirm_admin", False) or getattr(args, "clear_admin", False):
                cache = _cache()
                device = _selected(cache, args.device)
                confirmed = bool(args.confirm_admin)
                if confirmed and device.role == "guest":
                    raise CliError("guest_settings_forbidden", "Guest access cannot change settings. Ask the owner for Admin access.")
                path = local_config.confirm_admin(confirmed=confirmed)
                return {"ok": True, "operation": "permission_confirmation", "admin_confirmed": confirmed,
                        "role": device.role, "network_used": False}
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
        live = not getattr(args, "cache_only", False)
        output = {"ok": True, "device": public}
        if is_refresh:
            output["refresh"] = {"metadata_reloaded": True, "metadata_source": getattr(cache, "metadata_source", "app_cache"), "app_refresh_triggered": False, "app_cache_modified": False, "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(), "metadata_stale": True, "fresh_fields": ["motion_detection"] if live else []}
        if not live:
            return output
        try:
            response = _connected_request(cache, device, "motion.get", force_discovery=discover_for_read)
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
        shared_admin = role == "unknown" and (device.admin_confirmed or getattr(args, "shared_admin", False))
        if args.dry_run:
            allowed = role == "admin" or shared_admin
            reason = None
            if role == "guest":
                reason = "Guest access cannot change settings. Ask the owner for Admin access."
            elif role != "admin" and not shared_admin:
                reason = "Admin permission is unconfirmed. If the Eufy app shows Admin/Owner, run eufy configure --confirm-admin once, then retry."
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
            raise CliError("guest_settings_forbidden", "Guest access cannot change settings. Ask the owner for Admin access.")
        if role != "admin" and not shared_admin:
            raise CliError("role_unknown", "Admin permission is unconfirmed. If the Eufy app shows Admin/Owner, run eufy configure --confirm-admin once, then retry.")
        try:
            result = _connected_request(cache, device, "motion.set", motion_value=target,
                                        admin_asserted=shared_admin, force_discovery=discover_for_read)["result"]
            if (not isinstance(result, dict) or result.get("fresh") is not True
                    or result.get("source") != "local-p2p" or result.get("enabled") is not target
                    or type(result.get("previous")) is not bool or type(result.get("changed")) is not bool):
                raise BackendError("WRITE_READBACK_UNAVAILABLE", "Fresh verification is unavailable; check eufy status before another write.")
            return {"ok": True, "device": device.alias, "motion_detection": result["enabled"], "previous": result["previous"], "changed": result["changed"], "fresh": True, "source": "local_p2p"}
        except BackendError as exc:
            raise CliError(exc.code, str(exc)) from exc

    raise CliError("unknown_command", "unsupported command")


def main(argv: list[str] | None = None) -> int:
    from .presentation import emit
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    try:
        parser = _parser()
        args = parser.parse_args(argv)
        if args.command is None or args.command == "help":
            if getattr(args, "topic", None):
                parser.parse_args([args.topic, "--help"])
            else:
                parser.print_help()
            return 0
        return emit(run(args), json_output=json_output)
    except CliError as exc:
        return emit({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, json_output=json_output, code=2)
    except KeyboardInterrupt:
        return emit({"ok": False, "error": {"code": "interrupted", "message": "Interrupted. If changing motion, check eufy status before retrying."}}, json_output=json_output, code=130)
    except Exception:
        return emit({"ok": False, "error": {"code": "internal_error", "message": "Command could not be completed. Check eufy help and your local installation."}}, json_output=json_output, code=2)
