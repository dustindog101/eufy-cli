# eufy-cli

Unofficial local control for an Eufy T8210 doorbell paired with a T8010 HomeBase 2. Check motion detection or turn it on/off from your terminal:

```sh
eufy status
eufy motion off
eufy motion on
```

Commands show short text results. `status` reads fresh motion from the doorbell; a successful change verifies fresh device readback. A single supported doorbell is selected automatically. Use `eufy devices` and `--device ALIAS` when selection is ambiguous.

After one-time setup, commands use a private standalone profile. No runtime login, app-file reads, Eufy cloud API calls, daemon or GUI dependency. This experimental project is unaffiliated with Eufy/Anker. Local recording times are available with pagination. Complete app event history, video, playback and other settings are not implemented.

## Install

Requires Python 3.11+, Node.js 22+, npm, and LAN access to the HomeBase. Keep the launcher and Node backend together in the source checkout.

```sh
git clone https://github.com/dustindog101/eufy-cli.git
cd eufy-cli
npm ci --ignore-scripts --no-audit --no-fund
./bin/eufy
```

Optionally add the launcher to PATH:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$(pwd)/bin/eufy" "$HOME/.local/bin/eufy"
```

Inspect any existing command named `eufy` before replacing it. Ensure `~/.local/bin` is on PATH. Running `eufy` alone shows help without contacting the device.

## One-time setup

On a Mac with existing saved Eufy app metadata:

```sh
eufy configure --from-app
eufy status
```

Use `--device ALIAS` if selection is ambiguous. Existing profiles are preserved unless `--replace` is supplied. Windows users import an existing profile as described below. Discovery alone cannot provision all identifiers required for control.

For an invited/shared account, check the permission label for this doorbell in the Eufy app. If it says **Admin** or **Owner**, confirm that once:

```sh
eufy configure --confirm-admin
eufy motion off
```

The CLI remembers your confirmation for the current profile. It preserves the imported role and all device/account identities; it does not invent a mapping for numeric roles or grant access. An explicit Guest role remains blocked. Being invited alone does not establish Admin permission. If the app says Guest, ask the owner for Admin access instead.

`eufy configure --clear-admin` forgets your confirmation. Reimporting identifiers with `configure --from-app --replace` also clears it. The older `--shared-admin` motion option is a one-invocation assertion for confirmed Admin access; routine commands do not need it once confirmation is saved.

The private profile contains the HomeBase's private IPv4 address, station serial/P2P identifier, optional UDP response port, protocol account identifier, doorbell serial/channel/model/firmware, imported role and optional permission confirmation. It contains no passwords, login tokens, cloud DSKs, private keys, events or app settings. Its location is `$XDG_CONFIG_HOME/eufy-cli/local.json`, or `~/.config/eufy-cli/local.json` when unset. Unix uses directory mode 700/file mode 600; Windows uses an owner-only protected ACL. Never publish or upload it.

Missing or invalid profiles fail with setup guidance. Ordinary runtime never falls back to app files. Physical termination of the app and complete HomeBase WAN isolation have not been validated.

## Everyday commands

```sh
eufy                         # Concise help, no network
eufy status                  # Fresh motion status
eufy motion off               # Leave motion detection off
eufy motion on                # Leave motion detection on
eufy motion off --dry-run     # Offline permission/target preview
eufy devices                 # Saved device list
eufy status --cache-only      # Saved metadata, no fresh device state
eufy limits                  # Support limits, no network
eufy events yesterday        # Recording times for yesterday
eufy events day-before-yesterday
eufy events YYYY-MM-DD       # Recording times for a specific day
eufy events days             # HomeBase-reported activity dates, last 7 days
eufy help motion
eufy help session import
eufy help setup
eufy help troubleshooting
eufy help agents
```

`refresh` is an alias workflow for fresh status; `refresh --cache-only` stays offline. Other metadata remains a saved snapshot. Profiles do not store motion or battery snapshots, so cached state can be unavailable. The older `status --live` syntax remains accepted.

Motion changes require a fresh original state, a correlated command acknowledgement and an independent fresh readback. Already-matching state returns without sending a setter. Normal commands leave the requested state; an explicitly requested validation must restore its original state. On acknowledgement/readback/bridge failure, state may be uncertain: check `eufy status` before another write. The CLI never blindly retries a possible write.

### Recording times

```sh
eufy events                         # Today
eufy events yesterday
eufy events day-before-yesterday
eufy events YYYY-MM-DD               # One specific calendar day
eufy events yesterday --json
eufy events --since YYYY-MM-DD       # Date range through today
```

Commands list recording start times in chronological order, including seconds, and the number of records returned. A single configured doorbell is selected automatically. Each result is fresh local recording metadata, not a video download or an inferred motion alert. JSON includes start/end timestamps and the device alias, with no record IDs, media paths or account/device identifiers.

The CLI retrieves pages of 20 automatically, up to 200 records and 12 pages within its normal 13-second operation budget. `--limit N` selects a smaller bound (1–200); if bounded, the newest records are returned. Adjacent pages overlap by one second and are deduplicated using internal record IDs, retaining distinct recordings at identical times. A limit, later-page timeout/rejection or stalled cursor produces explicitly partial results. A first-page failure is an error, not a zero-event result. Invalid records, wrong device/station identities and out-of-range timestamps fail closed.

**Times preserve the HomeBase-reported wall clock; its timezone is unverified.** JSON timestamps intentionally have no UTC/offset suffix, with `time_basis:homebase_wall_clock` and `timezone_verified:false`. Do not convert them as if they were verified UTC timestamps. Day selection also uses these reported calendar dates. Relative days use this computer's calendar date.

`query_exhausted:true` means the local database query reached a short/empty page. It does not establish agreement with every event shown by the app: `complete` and `authoritative_history` remain false. Alerts without recordings, deleted/unavailable recordings and app-only events may differ. Empty output does not prove there were no events. Date selection is limited to the last 30 days. `--source cache` retains the older experimental notification-log reader and cannot be combined with a day selector.

The validated T8210/T8010 path needs `storage_cloud:0` for local records and a descending time cursor beginning at the following midnight. The pinned SDK's defaults sent a different storage value and the beginning of the requested period as the cursor, returning empty lists. The adapter now builds this specific read-only request directly through the SDK's guarded transport, preserves configured identities, and requires the matching query acknowledgement before accepting a page. Read-only live validation retrieved multiple pages for one day and a separate single-page day. Full app-list agreement and other models/firmware remain unverified.

### HomeBase activity dates

```sh
eufy events days
eufy events days --since YYYY-MM-DD
eufy events days --json
```

This read-only query shows calendar dates returned by the HomeBase's database. The default range is seven calendar days; an explicit start date can be at most 30 days ago, with at most 31 date buckets. Results are HomeBase-wide, so `--device` and `--source cache` are refused. Dates preserve the HomeBase's reported calendar dates; its timezone has not been independently verified.

**These are not verified daily event totals.** The protocol names a numeric field `count`, but its meaning on the tested firmware has not been reconciled with the events visible in the app. It can report `1` on a date containing multiple app events. Text output therefore shows the dates only. Missing dates are unknown, not zero events.

`events counts` remains a compatibility alias for `events days`. JSON preserves the raw `counts` date/value entries for existing callers, together with `count_meaning:unverified`, `event_totals_available:false`, `scope:homebase` and `complete:false`. Do not sum those values as event totals.

Use `events DAY` for individual recording times and returned-record counts. The separate activity-date query and its raw numeric values do not establish event totals. A separate device counter also has no requested date range; it cannot establish daily or period totals.

### LAN connection handling

Stay on the HomeBase LAN. Live status and motion commands try the saved address first, keeping working connections quick. A connection timeout/failure triggers one bounded LAN lookup and one retry at a **unique endpoint matching the configured station identity**. For motion changes, recovery is allowed only when the backend explicitly confirms no setter was attempted. No match, ambiguous matches, command rejection, uncertain writes, or failed readback cannot trigger repeated writes. Event/count queries use the saved endpoint; if it has moved, refresh it with `configure --from-discovery`.

Recovery uses the matched IP/UDP response port in memory, preserving the private profile and exact device/account identities. To save a changed address explicitly:

```sh
eufy configure --from-discovery
# Or verify one particular private address before saving it:
eufy configure --host PRIVATE_IP
```

`--discover` is an optional shortcut on `status`, `refresh` or `motion` to look up the current matching endpoint before connecting. It is useful when an address/response port changes frequently. It cannot be combined with `--cache-only`. Discovery cannot grant permission or replace station identity.

The Node backend permits UDP only to the exact selected private IPv4 HomeBase address. Broadcast is confined to the separate bounded discovery path (explicit discovery/setup, `--discover`, or automatic connection recovery). TCP, HTTP, fetch and cloud access remain blocked. Matching discovery replies is an identity check, not cryptographic authentication of broadcast datagrams. No network or firewall configuration is changed.

Each backend attempt has a 13-second shared budget plus up to 1.5 seconds cleanup, with a 20-second bridge timeout. Connection recovery adds at most one 2-second lookup and one backend attempt. Successful saved-endpoint commands do not discover. `--discover` adds a single 2-second lookup and does not enter another recovery loop.

### Scripts and errors

Use `--json` for machine-readable results, before or after the command:

```sh
eufy status --json
eufy --json motion off
eufy limits --json
```

JSON keeps structured fields such as `device.live_motion.value` for fresh status and `motion_detection`, `previous`, `changed`, `fresh` for motion changes. Errors return `ok:false` with an error code/message and exit 2. Human errors go to stderr; normal text goes to stdout. Help remains text. Interrupted commands exit 130; check fresh status if interruption occurred during a change.

**Migration from 0.1.x:** add `--json` to scripts that parsed the old default JSON. Plain `status` now queries fresh motion; use `status --cache-only` for the old offline behavior.

## Windows and private profile transfer

From PowerShell, use the same checkout and dependencies:

```powershell
git clone https://github.com/dustindog101/eufy-cli.git
cd eufy-cli
npm.cmd ci --ignore-scripts --no-audit --no-fund
.\bin\eufy.cmd help
```

`python bin/eufy COMMAND` also works, locally or over an existing SSH connection. Windows cannot import Mac app files. Export the configured profile on the source machine, transfer it privately over SSH/SFTP, then import on the trusted destination:

```sh
eufy session export PRIVATE_PROFILE.eufy-profile.json
# Transfer privately; never commit or upload this file.
eufy session import PRIVATE_PROFILE.eufy-profile.json
```

On Windows:

```powershell
.\bin\eufy.cmd session import PRIVATE_PROFILE.eufy-profile.json
.\bin\eufy.cmd status
```

Exports are unencrypted and carry standalone identifiers, alias, imported role, permission confirmation if present, and saved endpoint. They include no login cookies/tokens, passwords, cloud keys or events. Choose an existing private directory outside the checkout; export protects the file but leaves the directory's permissions alone. `--overwrite` replaces an existing export. Import validates the versioned file offline and atomically installs it owner-private; existing profiles require `--replace`. Failed validation/saving preserves the previous profile. Import preserves permission facts; it does not grant access. Older CLI releases cannot import profiles containing the new permission-confirmation field.

Both transfer commands run offline, with no app/device access or network discovery. Windows PowerShell is required for private configuration writes.

Some stateful firewalls discard HomeBase replies because the UDP source port differs from the lookup port 32108. Identity-matched discovery obtains the current response port, allowing the backend to establish an outbound flow on its own socket before normal SDK lookup. If your Windows connection often needs recovery, use `status --discover` to look up the current port first. The port may change between sessions; saving it alone may not suffice. No firewall changes are made. See [Microsoft's UDP flow documentation](https://learn.microsoft.com/en-us/windows/win32/fwp/ale-stateful-filtering).

Mac-to-Windows private import/export and repeated fresh status with explicit discovery have been live validated. Automatic recovery is regression-tested with synthetic fixtures; Windows motion writes remain untested.

## Support and limits

| Feature | Status |
| --- | --- |
| Fresh motion status | Live validated on macOS and Windows for T8210 firmware 2.2.4.6 + T8010 firmware 3.4.2.6h |
| Motion on/off with readback | Live validated on macOS for that pair; Windows writes untested |
| App-file-independent runtime | Live validated with app-cache loading blocked |
| Local discovery; standalone endpoint maintenance | Implemented, identity-checked |
| Automatic connection recovery; consumer UX | Synthetic regression coverage; do not infer live recovery from fixtures |
| Offline profile export/import | Mac-to-Windows round-trip and fresh Windows status validated |
| Recording times | Read-only single-day and multi-page retrieval live validated on macOS for the documented pair; full app-list agreement and clock timezone unverified |
| HomeBase-reported activity dates | Live verified on macOS; numeric values are not verified event totals |
| Live video, recording playback/download, other settings, snooze, security modes | Not implemented |
| Fresh provisioning from discovery alone | Not implemented |

Other models/firmware are unverified. The local key derives from station serial and P2P DID, without a login-expiry timer; firmware, identity, permission or network changes can still break access. Compatibility is not guaranteed indefinitely. The CLI's network restrictions do not establish that the HomeBase itself avoids cloud traffic.

Recording queries allow at most 200 records, 12 pages and a start date at most 30 days ago. Partial results are explicitly marked; empty output does not prove no events. Complete app event coverage and encrypted recording access are unavailable.

## Help

`eufy`, `eufy help` and `eufy --help` show the same concise command overview. Each command's help includes examples. Use either `eufy help events` or `eufy events --help`; nested help works too, such as `eufy help session import`.

The offline guides `eufy help setup`, `eufy help troubleshooting` and `eufy help agents` cover first use, connection/permission recovery, and assistant integration. Help works without a profile, Node.js, app access or a device connection. Syntax errors show the relevant usage and help command without echoing arbitrary argument values. `--json` is for command results and errors; help stays readable text.

Legacy `--shared-admin` and experimental `events --source cache` remain accepted for compatibility and documented here, but are hidden from ordinary help. Everyday motion uses saved legitimate permission confirmation; ordinary events read the HomeBase.

## Agent and personal-assistant skill

The [Eufy CLI skill](skills/eufy-cli/SKILL.md) maps natural-language requests to commands and explains result verification, permissions, event pagination, clock interpretation and private-data handling. It supports requests such as:

- "Is motion detection on?"
- "Turn motion detection off."
- "Show recording times from the day before yesterday."
- "How many recordings came back for that day?"

Install the complete skill folder in your assistant's skill directory. For Codex:

```sh
mkdir -p "$HOME/.codex/skills"
cp -R skills/eufy-cli "$HOME/.codex/skills/"
```

Reload/restart the agent if needed. Other assistants with Markdown skill support can use the same `SKILL.md` with access to the installed CLI; no connector, daemon or API service is required. Follow that assistant's skill-discovery convention. The optional `agents/openai.yaml` provides Codex UI metadata and keeps automatic discovery enabled.

CLI dependencies and the private profile are installed separately; the skill contains no identifiers or credentials. Do not place the private profile inside the skill or repository. Agents should use `--json`, leave requested motion changes in place after fresh verification, and report partial recording results accurately. The skill does not implement scheduled monitoring or send event data to other services.

## Development

```sh
python3 -m unittest discover -s tests -q
npm test
git diff --check
./bin/eufy --version
```

The Node adapter pins `eufy-security-client` 4.0.0 and uses its private transport internals. Dependencies are unchanged for the consumer CLI. CI uses synthetic fixtures, never a live camera or private metadata. Before publishing diagnostics, remove local paths, aliases, LAN addresses, serials, account/P2P identifiers and event contents. Never submit a config, app archive or packet capture to an issue.

MIT licensed; see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md). P2P interoperability uses [bropat/eufy-security-client](https://github.com/bropat/eufy-security-client).
