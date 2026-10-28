"""Small text views; automation uses the explicit --json switch."""
from __future__ import annotations

import json
import sys
from typing import Any


def _state(value: Any) -> str:
    return "on" if value is True else "off" if value is False else "unavailable"


def _text(result: dict[str, Any]) -> str:
    if result.get("dry_run"):
        permission = "Permission confirmed." if result["write_available"] else result["reason"]
        return f"Preview: motion detection → {_state(result['target_value'])}. {permission} No device contacted."
    if "motion_detection" in result:
        action = "Already" if not result["changed"] else "Now"
        return f"{action} {_state(result['motion_detection'])}. Motion detection verified on the doorbell."
    if "devices" in result:
        return "\n".join(f"{d['alias']} ({d['model'] or 'unknown model'})" for d in result["devices"]) or "No configured devices. Run eufy help configure."
    if isinstance(result.get("device"), dict):
        device = result["device"]
        live = device.get("live_motion")
        if live:
            if not live["available"]:
                return "Fresh motion status unavailable. Check the HomeBase connection and run eufy status."
            return f"Motion detection is {_state(live['value'])}. Verified on the doorbell."
        return f"{device['alias']} ({device['model'] or 'unknown model'}). Saved metadata only; run eufy status for fresh motion."
    if result.get("operation") == "permission_confirmation":
        return ("Admin confirmation saved for this profile. You can now use eufy motion off or eufy motion on."
                if result["admin_confirmed"] else "Saved Admin confirmation cleared.")
    if "config_path" in result:
        return "Private profile saved. Run eufy status to check the doorbell."
    if result.get("operation", "").startswith("session_"):
        return ("Private profile exported. Transfer the unencrypted file privately."
                if result["operation"] == "session_export" else "Private profile imported. Run eufy status on the HomeBase LAN.")
    if "verified_pair" in result:
        return ("Supported: T8210 doorbell + T8010 HomeBase 2.\n"
                "Fresh motion status; motion on/off with verified readback.\n"
                "HomeBase-reported daily record counts: eufy events counts.\n"
                "History unresolved. Video, playback and other settings unavailable.\n"
                "Local LAN only; no runtime login or cloud access. Use --json for technical limits.")
    if "responders" in result:
        rows = result["responders"]
        matched = sum(row.get("matched_cached_device") is True for row in rows)
        return f"Found {len(rows)} HomeBase endpoint(s); {matched} match your profile."
    if "events" in result:
        return f"Experimental history: {len(result['events'])} item(s). History is unresolved; empty output does not prove no events. Use --json for records."
    if "counts" in result:
        lines = [f"HomeBase-reported daily records ({result['start_date']} to {result['end_date']}):"]
        lines.extend(f"  {row['date']}: {row['count']}" for row in result["counts"])
        if not result["counts"]:
            lines.append("  No daily counts returned.")
        lines.append("HomeBase-wide counts; complete doorbell history is unverified. Missing days are unknown, not zero.")
        return "\n".join(lines)
    return "Done."


def emit(result: dict[str, Any], *, json_output: bool = False, code: int = 0) -> int:
    if json_output:
        sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")
    elif result.get("ok") is False:
        error = result["error"]
        message = error["message"]
        if error["code"] == "backend_timeout":
            message += ". If changing motion, the setting may have changed; check eufy status before another write."
        if error["code"] in {"LOCAL_TIMEOUT", "LOCAL_CONNECT_FAILED", "backend_timeout"}:
            message += ". Check that this computer and the HomeBase are on the same LAN, then run eufy status."
        elif error["code"] in {"station_identity_not_verified", "station_endpoint_ambiguous", "station_endpoint_invalid"}:
            message += ". Check the HomeBase LAN connection and run eufy help configure."
        sys.stderr.write(f"eufy: {message}\n")
    else:
        sys.stdout.write(_text(result) + "\n")
    return code
