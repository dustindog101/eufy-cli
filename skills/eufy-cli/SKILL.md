---
name: eufy-cli
description: Use the Eufy CLI to check fresh motion status, enable or disable motion detection, read local recording times and HomeBase-reported activity dates, transfer a private profile between trusted machines, and troubleshoot local setup or discovery. Applies to T8210/T8010; complete event history, live video, playback and other settings are unavailable.
---

# Eufy CLI

Use installed `eufy` for the user's requested supported action. It sends local P2P commands to a HomeBase using private standalone identifiers, with no runtime login, app-file reads or Eufy cloud API calls. Initial identifiers must already be configured. Treat the config as private even though it contains no passwords or login tokens.

## Find and inspect

- Check `command -v eufy`, then `eufy --version`, `eufy limits` and, when relevant, `eufy help COMMAND`.
- If missing, use the user's checkout or the installation instructions in the repository README: https://github.com/dustindog101/eufy-cli. Python3.11+, Node22+ and npm are required; keep launcher and backend together in the source checkout.
- On Windows, use `python bin/eufy COMMAND` or `bin\eufy.cmd COMMAND` from the source checkout; `Get-Command eufy` checks PATH. Help/tests/config ACLs, LAN discovery, and repeated fresh motion reads have been validated on Windows. Windows motion writes have not been live tested. Use plain `status` for fresh motion; normal connection failures automatically trigger one identity-matched lookup and safe retry. `status --discover` or `refresh --discover` can look up the current endpoint first when ports change often. Discovered endpoints are used only in memory. Automatic recovery has synthetic regression coverage, not live Windows validation. The backend stays pinned to the selected HomeBase IP. Do not infer account expiry or change firewall/routes incidentally. Use `session export FILE` and `session import FILE` to transfer a previously configured profile privately; Windows cannot perform the Mac app import. Do not transfer a profile to another machine without authorization.
- `eufy devices` shows aliases and models. Use `--device ALIAS` when the user names a device or selection is ambiguous.
- Normal results are concise text; human errors go to stderr and exit 2. **Use `--json` for structured agent/script results**, including runtime errors with `ok:false`. The flag works before or after the command. Bare `eufy` shows offline help. Do not interpret errors or unavailable values as device states.

## Status and requested motion changes

```sh
eufy status --json
eufy refresh --json
eufy motion off --dry-run --json
eufy motion off --json
eufy motion on --json
```

`device.live_motion.value` is the fresh boolean in JSON status/refresh. Plain `status` is live by default; legacy `--live` is still accepted. Use `status --cache-only` or `refresh --cache-only` for offline saved metadata. Dry-run is offline and shows permission/target, not fresh motion. Do not report cached or unavailable state as fresh.

For a motion change, use the requested on/off state. The backend reads the original, correlates the setter acknowledgement, and performs independent fresh readback. Success returns `motion_detection`, `previous`, `changed` and `fresh`; already-matching settings are a no-op. Normal commands leave the requested state in place.

If role is Admin or the profile has a saved Admin confirmation, no assertion flag is needed. If the numeric saved role is unknown, ask for the specific permission label shown for this shared doorbell; being invited alone is insufficient. When the user or trusted GUI evidence confirms Admin/Owner, use `eufy configure --confirm-admin` once to persist that fact for the current profile. It preserves the imported role and device/account identities and never overrides explicit Guest. `configure --clear-admin` forgets it; app reimport also clears it. The older `--shared-admin` is a one-invocation assertion, only for confirmed Admin access. Keep confirmed context without asking repeatedly; reads and independent implementation can continue while a permission fact is missing. Never invent numeric role mappings, edit an imported role or substitute identities.

If the user requests a validation toggle, observe the original fresh state and restore it in a finally/recovery path, with fresh readback. Do not toggle merely to answer a status question. For acknowledgement/readback/bridge errors, report uncertainty and query `status --json`; do not blindly repeat writes. Automatic recovery retries only connection failures and, for writes, requires explicit backend proof that no setter was attempted. It never retries an uncertain write, rejection, acknowledgement failure or readback failure.

“Turn off the doorbell” is ambiguous: clarify whether they mean motion detection or something else. Camera power, chime, recording and security-mode controls are outside this CLI's implemented scope.

## Setup and IP maintenance

```sh
eufy configure --from-app
eufy configure --host PRIVATE_IP
eufy configure --from-discovery
eufy discover --timeout 2
eufy refresh --cache-only
```

Setup imports only the needed identifiers from existing saved Mac app metadata into an owner-private config outside the checkout. It does not log in. Do this for an authorized setup request; preserve an existing config unless replacement is requested. `--replace` explicitly reimports identifiers. Normal commands never fall back to the app if config is missing or invalid.

Live status and motion commands try the saved endpoint first. On a safe connection failure, they perform one bounded 2-second broadcast, require one endpoint matching the configured station identity, and retry once. They do not rewrite the profile. No or ambiguous identity matches fail closed. The Node backend remains pinned to the exact selected private HomeBase IP; cloud/TCP/HTTP/fetch guards remain active. This discovery identity check is not cryptographic authentication of broadcast replies. To persist an endpoint change, `configure --host` (directed) or `configure --from-discovery` requires a unique matching configured-station reply. For changing response ports, optional `--discover` looks up the current endpoint before status/refresh/motion. Reject it with cached-only status/refresh. It does not change network settings or access the app. Discovery alone cannot bootstrap all serial/account/channel identifiers needed for control. A local key has no known login-session expiry timer on the tested path, but future device/firmware/permission/network changes can break access.

## Recording times

```sh
eufy events yesterday --json
eufy events day-before-yesterday --json
eufy events YYYY-MM-DD --json
eufy events --since YYYY-MM-DD --json
```

For requests such as "all event times from the day before yesterday", use `events day-before-yesterday`. A bare `events` selects today. A date selects one day; `--since` selects a range through today and cannot be combined with a day selector. Relative dates use the computer's calendar date. Selection is automatic when unambiguous; `--device ALIAS` selects a configured device. This is read-only and works without Admin write confirmation.

The CLI automatically paginates local recording metadata in pages of 20, up to 200 records/12 pages within the normal operation budget. `--limit N` can lower the bound. It uses the validated local-storage flag and a descending cursor from the following midnight, with a one-second overlap deduplicated by internal record IDs. Do not replace this with the SDK's incorrect default storage/cursor request or substitute identities. Each page requires its own query ACK. Returned records must match the selected device/station and requested date range. Later-page connection/query failure, a bound or stalled cursor is explicitly partial; a first-page failure is an error. Never silently retry writes or change motion while troubleshooting history.

JSON provides `events` containing `timestamp`, `end_timestamp`, `type:recording` and `device_alias`, plus `count`, date bounds, pages, `truncated`, `partial_reason` and `query_exhausted`. Timestamps preserve the HomeBase wall clock with **no UTC/offset suffix**: `time_basis:homebase_wall_clock`, `timezone_verified:false`. Report them as HomeBase-reported times; do not silently interpret them as UTC or claim the user's local timezone. Text output shows chronological AM/PM times with seconds.

`query_exhausted:true` only means the database returned a short/empty page. `complete:false` and `authoritative_history:false` preserve the unverified agreement with the full app event list. Report the number of recordings returned, not a proven count of all motion/other alerts. Keep separate recordings at identical timestamps. If partial, mention the reason and that additional recordings may exist. Empty output does not prove zero events. Single-day and multi-page reads are live validated on macOS for the documented pair; Windows and other firmware/models remain unverified. No video access or app/cloud fallback is implemented.

## Activity dates and unresolved event totals

```sh
eufy events days --json
eufy events days --since YYYY-MM-DD --json
```

This read-only LAN query returns dates reported by the HomeBase. Default range: seven calendar days; explicit start: at most 30 days ago; maximum: 31 buckets. Dates preserve the reported calendar dates; the HomeBase timezone is not independently verified. Results cover the whole HomeBase, so `--device` and `--source cache` are refused.

The raw protocol field called `count` has not been reconciled with app events. A returned value of 1 must not be reported as one event; the app may show several on that day. Text output shows dates only. JSON retains `counts` entries with `date` and raw `count` for compatibility, plus `count_meaning:unverified`, `event_totals_available:false`, `scope:homebase` and `complete:false`. Never sum raw values as event totals, invent zeroes for missing dates, or infer per-doorbell completeness. `events counts` remains an alias with these same limits.

Use `events DAY` for timestamped recording metadata and returned-record counts. The separate device counter has no requested date range and cannot establish daily totals. Event queries use the saved endpoint without automatic recovery; maintain a moved endpoint with identity-matched `configure --from-discovery`. No cloud services, media, app files or device writes are used. Date retrieval is live verified on macOS for the documented pair; Windows remains unverified. Keep activity metadata private unless sharing is authorized.

## Portable session/profile transfer

```sh
eufy session export PRIVATE_PROFILE.eufy-profile.json
eufy session import PRIVATE_PROFILE.eufy-profile.json
```

Use these for an authorized transfer between the user's trusted machines. Transfer the file over a private channel such as SSH/SFTP; never paste or publish its contents. Export is unencrypted and contains private device/account identifiers, not login cookies or tokens. Choose an existing private directory for the export; the CLI restricts the file but does not change that directory's permissions. Import validates the versioned file offline and installs it with owner-only permissions. No app access, cloud/login calls or camera changes occur. Verify LAN access separately with `status --json`. Use `--discover` if you want identity-matched lookup first.

Preserve existing profiles by default. Use import `--replace` only for an authorized replacement; use export `--overwrite` only when replacing that output is intended. Invalid imports and failed atomic saves preserve the previous profile. Preserve transferred role/identifiers and optional permission confirmation exactly; importing does not grant Admin access. Older releases cannot import the new confirmation field. Transfer to another host still needs authorization. Mac-to-Windows import/export and fresh Windows status have been live validated. On Windows use the checkout launcher described above.

## Limits and private diagnostics

- Live validated on T8210 + T8010: fresh motion status on macOS and Windows, and motion changes on macOS. Windows motion writes and other models/firmware are unverified.
- Local recording timestamps and HomeBase-wide activity dates are available. Full app event coverage and the HomeBase clock timezone remain unverified; activity-date numeric values are not verified event totals. Empty local output is not proof there are no events; logs are not a reliable substitute. No live video, encrypted clip playback/download, other settings, snooze or security modes.
- The Node transport is pinned to one selected private IPv4 HomeBase address; separate bounded discovery can broadcast during setup, explicit lookup, or safe automatic connection recovery. This does not establish that the HomeBase itself never contacts cloud services.
- Never disclose the config, app archives, tokens/keys, serials, account/P2P identifiers, private addresses, personal paths or event contents in public artifacts. `discover` can display LAN addresses; aliases and setup paths can also be personal. Share only suitably redacted errors/booleans/model/version information.
- Do not add fresh cloud login, encrypted-video enrollment, resets, deletions or device/account/network changes as incidental troubleshooting. Explain the supported limitation and preserve the user's requested scope.
