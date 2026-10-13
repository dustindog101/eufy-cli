---
name: eufy-cli
description: Use the Eufy CLI to answer questions about a configured Eufy doorbell, check fresh motion detection status, enable or disable motion detection, transfer a private profile between trusted machines, and troubleshoot local setup or discovery. Applies to the CLI's supported T8210/T8010 pair; live video, event playback and other settings are not implemented.
---

# Eufy CLI

Use installed `eufy` for the user's requested supported action. It sends local P2P commands to a HomeBase using private standalone identifiers, with no runtime login, app-file reads or Eufy cloud API calls. Initial identifiers must already be configured. Treat the config as private even though it contains no passwords or login tokens.

## Find and inspect

- Check `command -v eufy`, then `eufy --version`, `eufy limits` and, when relevant, `eufy help COMMAND`.
- If missing, use the user's checkout or the installation instructions in the repository README: https://github.com/dustindog101/eufy-cli. Python3.11+, Node22+ and npm are required; keep launcher and backend together in the source checkout.
- On Windows, use `python bin/eufy COMMAND` or `bin\eufy.cmd COMMAND` from the source checkout; `Get-Command eufy` checks PATH. Help/tests/config ACLs, LAN discovery, and repeated fresh motion reads have been validated on Windows. Windows motion writes have not been live tested. For Windows live reads, use `status --live --discover` or `refresh --discover`. This performs one explicit identity-matched broadcast and uses the current response port only in memory, without rewriting the profile. The port can change between sessions; a saved port alone may not suffice. The backend stays pinned to the selected HomeBase IP. Do not infer account expiry or change firewall/routes incidentally. Use `session export FILE` and `session import FILE` to transfer a previously configured profile privately; Windows cannot perform the Mac app import. Do not transfer a profile to another machine without authorization.
- `eufy devices` shows aliases and models. Use `--device ALIAS` when the user names a device or selection is ambiguous.
- Normal results are JSON. Runtime errors exit2 with `ok:false`. Do not interpret an error or unavailable value as a device state.

## Status and requested motion changes

```sh
eufy status --live
eufy refresh
eufy motion off --dry-run
eufy motion off
eufy motion on
```

`device.live_motion.value` is the fresh boolean for status/refresh. Default `status` and dry-run have only saved metadata and can show no motion value. Do not report cached or unavailable state as fresh.

For a motion change, use the requested on/off state. The backend reads the original, correlates the setter acknowledgement, and performs independent fresh readback. Success returns `motion_detection`, `previous`, `changed` and `fresh`; already-matching settings are a no-op. Normal commands leave the requested state in place.

If role is Admin, no role assertion flag is needed. If its numeric saved role is unknown, use `--shared-admin` only when the user or trusted current/prior GUI evidence confirms Admin permission. Keep that confirmed context; do not demand the same confirmation again without a reason. If unconfirmed, ask whether the app says Admin permission before a write; reads can continue. Explicit Guest roles remain blocked. Never edit a role or substitute identifiers to bypass a refusal.

If the user requests a validation toggle, observe the original fresh state and restore it in a finally/recovery path, with fresh readback. Do not toggle merely to answer a status question. For acknowledgement/readback errors, report uncertainty and query `status --live`; do not blindly repeat writes.

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

For an IP/response-port change, `configure --host` (directed) or `configure --from-discovery` (one broadcast) requires a unique matching configured-station reply before updating the local endpoint. The latter also saves the UDP response port used to establish the outbound reply flow. It can change between connections; use the explicit `--discover` flag for each Windows live read. Reject `--discover` with cached-only status/refresh. It does not change network settings or access the app. Discovery alone cannot bootstrap all serial/account/channel identifiers needed for control. A local key has no known login-session expiry timer on the tested path, but future device/firmware/permission/network changes can break access.

## Portable session/profile transfer

```sh
eufy session export PRIVATE_PROFILE.json
eufy session import PRIVATE_PROFILE.json
```

Use these for an authorized transfer between the user's trusted machines. Transfer the file over a private channel such as SSH/SFTP; never paste or publish its contents. Export is unencrypted and contains private device/account identifiers, not login cookies or tokens. Import validates the versioned file offline and installs it with owner-only permissions. No app access, cloud/login calls or camera changes occur. Verify LAN access separately with `status --live --discover`.

Preserve existing profiles by default. Use import `--replace` only for an authorized replacement; use export `--overwrite` only when replacing that output is intended. Invalid imports and failed atomic saves preserve the previous profile. Preserve the transferred role/identifiers exactly; importing does not grant Admin access. Transfer to another host still needs authorization. On Windows use the checkout launcher described above.

## Limits and private diagnostics

- Live validated on T8210 + T8010: fresh motion status on macOS and Windows, and motion changes on macOS. Windows motion writes and other models/firmware are unverified.
- Event history is unresolved. Empty local output is not proof there are no events; logs are not a reliable substitute. No live video, encrypted clip playback/download, other settings, snooze or security modes.
- CLI traffic is pinned to one private IPv4 HomeBase address. This does not establish that the HomeBase itself never contacts cloud services.
- Never disclose the config, app archives, tokens/keys, serials, account/P2P identifiers, private addresses, personal paths or event contents in public artifacts. `discover` can display LAN addresses; aliases and setup paths can also be personal. Share only suitably redacted errors/booleans/model/version information.
- Do not add fresh cloud login, encrypted-video enrollment, resets, deletions or device/account/network changes as incidental troubleshooting. Explain the supported limitation and preserve the user's requested scope.
