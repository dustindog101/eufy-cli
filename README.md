# eufy-cli

Unofficial local CLI for an Eufy T8210 doorbell paired with a T8010 HomeBase 2. It reads fresh motion status and changes motion detection over LAN, with command acknowledgement and independent readback.

After a one-time identifier import, normal commands use a private standalone configuration. They do not read app files, log in, or call Eufy cloud APIs. The transport is Eufy's proprietary P2P protocol. This is an experimental project, unaffiliated with Eufy/Anker.

## Install

Requires Python 3.11+, Node.js 22+, npm, and LAN access to the HomeBase. Use the source checkout so the Python launcher and Node backend stay together.

```sh
git clone https://github.com/dustindog101/eufy-cli.git
cd eufy-cli
npm ci --ignore-scripts --no-audit --no-fund
./bin/eufy help
```

Optionally add the launcher to PATH:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$(pwd)/bin/eufy" "$HOME/.local/bin/eufy"
```

If a command named `eufy` already exists, inspect it before replacing it. Ensure `~/.local/bin` is on PATH.

## Windows and SSH

From PowerShell, install the same source checkout:

```powershell
git clone https://github.com/dustindog101/eufy-cli.git
cd eufy-cli
npm.cmd ci --ignore-scripts --no-audit --no-fund
.\bin\eufy.cmd help
.\bin\eufy.cmd discover --timeout 2
```

`python bin/eufy COMMAND` also works. Run locally on Windows or through an existing SSH connection; the Windows process sends UDP to the HomeBase, rather than forwarding camera traffic through the SSH client. The Windows host needs a working route to the HomeBase and permission for UDP replies. No firewall changes are made by this CLI.

Windows cannot import the Mac app cache. On the configured source machine, run `eufy session export PRIVATE_PROFILE.eufy-profile.json`, transfer the file privately, then run `.\bin\eufy.cmd session import PRIVATE_PROFILE.eufy-profile.json` on Windows. Keep the transfer file outside the checkout in a directory restricted to your Windows user. Import protects the installed profile with an owner-only Windows ACL; Unix uses directory700/file600. Windows PowerShell is required for private configuration writes. See [portable session transfer](#portable-session-exportimport) below.

After transferring a profile, use an identity-matched lookup before each live read:

```powershell
.\bin\eufy.cmd status --live --discover
.\bin\eufy.cmd refresh --discover
```

The explicit `--discover` option broadcasts once, requires one endpoint matching the configured station identity, then uses that IP/UDP response port in memory for the live read. It preserves the profile, device/account identifiers and permissions, and refuses missing or ambiguous matches. It cannot provision a blank config. `configure --from-discovery` is available separately to persist a matched endpoint.

HomeBase lookup replies can use a different UDP source port than the request to32108. Some stateful firewalls discard those replies. The matched response port lets the CLI establish an outbound flow on its own socket before the SDK's normal lookup, without changing firewall settings. The backend still sends only to the selected HomeBase IP; broadcast occurs only in explicit discovery/setup or an opted-in live `--discover` read. See [Microsoft's UDP flow documentation](https://learn.microsoft.com/en-us/windows/win32/fwp/ale-stateful-filtering).

The response port can change between connections, so a saved port alone is insufficient on some stateful firewalls. Use `--discover` for repeated Windows live reads. Windows launcher/config permissions, synthetic tests, and repeated fresh motion reads with `--discover` are live verified. Windows motion writes have not been tested.

## One-time setup

On a Mac with existing saved Eufy app metadata:

```sh
eufy configure --from-app
```

Use `--device ALIAS` if selection is ambiguous. An existing config is preserved unless `--replace` is explicitly supplied.

The CLI stores only the HomeBase private IPv4 address, station serial/P2P identifier, optional discovered UDP response port, protocol account identifier, and doorbell serial/channel/model/firmware metadata. It does not copy passwords, login tokens, cloud DSKs, private keys, events or app settings. Configuration is stored at `$XDG_CONFIG_HOME/eufy-cli/local.json`, or `~/.config/eufy-cli/local.json` when unset; Unix uses directory700/file600; Windows config writes apply an owner-only protected ACL. Never publish or upload this file.

Normal commands never fall back to the app if config is missing or invalid. Discovery alone cannot currently bootstrap all identifiers needed for control. A configured CLI has no GUI/IPC dependency; physical termination of the app and complete HomeBase WAN isolation have not been validated.

## Portable session export/import

Move the configured local profile between your trusted machines:

```sh
eufy session export PRIVATE_PROFILE.eufy-profile.json
# Transfer the file privately, for example over SSH/SFTP.
eufy session import PRIVATE_PROFILE.eufy-profile.json
```

Windows uses `python bin/eufy session import PRIVATE_PROFILE.eufy-profile.json` or `bin\eufy.cmd session import PRIVATE_PROFILE.eufy-profile.json` from the source checkout. The export carries the standalone device/account identifiers, alias, recorded role and saved LAN endpoint in a versioned JSON envelope. It includes no app archives, passwords, login cookies/tokens, cloud keys or events. The file is unencrypted and private; never commit or upload it publicly.

Export protects the output file with owner-only permissions and preserves its source. The chosen output directory must already exist; its permissions are left alone. `--overwrite` explicitly replaces an existing export file. Import rejects malformed, oversized, unsupported or extra-credential data before changing the local profile, preserves identifiers/role, and uses an atomic owner-private save. An existing destination requires `--replace`; failed validation or saving preserves it. Imports do not grant permissions.

Both commands run offline and do not access the app, discover devices, change camera settings or refresh metadata. Verify the imported profile separately on the HomeBase LAN:

```sh
eufy status --live --discover
```

Mac export → Windows import/export and fresh LAN status have been verified with a temporary profile; the active Windows profile remained unchanged. The receiver needs the source checkout, Python/Node dependencies, and LAN access. Peer ports/IPs can change; `--discover` finds the current identity-matched endpoint for a live read.

## Commands

```sh
eufy devices
eufy status --live
eufy refresh
eufy refresh --cache-only
eufy motion off --shared-admin --dry-run
eufy motion off --shared-admin
eufy motion on --shared-admin
eufy discover --timeout 2
eufy limits
eufy help motion
```

`status --live` and `refresh` query fresh motion; other metadata remains a saved snapshot. `--cache-only` does not send a network request. Standalone config stores identifiers, not battery or motion snapshots, so cached state can be unavailable.

Motion changes require a fresh original state, matching command acknowledgement, and fresh readback. A matching state returns without sending a setter. Normal commands leave the requested state in place; validation tests restore their original state. On acknowledgement/readback failure, the setting may be uncertain—check fresh status rather than retrying a write blindly.

`--shared-admin` asserts Admin permission shown in the app when its numeric saved role cannot be mapped reliably. It does not grant permission, replace identity, or override an explicit Guest role. Do not use it unless Admin access is confirmed.

If the HomeBase IP changes:

```sh
eufy configure --host PRIVATE_IP
# Or match the existing station via one broadcast and refresh its IP/UDP port:
eufy configure --from-discovery
```

Both options send one bounded LAN lookup and update the endpoint only after a reply matches the configured station identifier. `--from-discovery` uses broadcast; `--host` uses the supplied private address. It does not read the app or change network settings.

## Support and limits

| Feature | Status |
| --- | --- |
| Fresh motion status | Live validated on macOS and Windows for T8210 firmware2.2.4.6 + T8010 firmware3.4.2.6h |
| Motion on/off with readback | Live validated on macOS for that pair; Windows writes not tested |
| App-file-independent runtime | Live validated with app-cache loading blocked |
| Local discovery; standalone IP maintenance | Implemented, identity-checked |
| Offline profile export/import | Mac-to-Windows round-trip and fresh Windows status validated |
| Event history | Unresolved: local queries can return empty despite app events |
| Live video, recording playback/download, other settings, snooze, security modes | Not implemented |
| Fresh provisioning from discovery alone | Not implemented |

Other models/firmware are not verified. Motion status has been live validated on macOS and Windows; motion changes have been live validated on macOS, with Windows writes unverified. The local key derives from station serial and P2P DID, without a login-expiry timer; firmware, identity, permission or network changes can still break access. No promise of indefinite compatibility is made.

An operation has a13-second shared budget plus up to1.5 seconds cleanup; opted-in live discovery adds one bounded2-second lookup; the bridge timeout is20 seconds. Experimental history queries allow at most20 items and a start date at most30 days ago. Empty history output is not proof that no events exist. No complete pagination or encrypted-recording access is provided.

Normal results are JSON; runtime failures return an `ok:false` error and exit2. Help is human-readable. `eufy limits` lists current capabilities and budgets without network access.

The Node adapter pins `eufy-security-client4.0.0`, uses its private transport internals, clears cloud addresses, blocks TCP/HTTP/fetch, and permits UDP only to the exact configured RFC1918 HomeBase address. These limits concern the CLI, not all traffic the HomeBase itself generates.

## Agent skill

The [eufy-cli skill](skills/eufy-cli/SKILL.md) lets compatible agents select the CLI for Eufy status, motion-control and setup requests. Install it in a discoverable skill directory; for Codex:

```sh
mkdir -p "$HOME/.codex/skills"
cp -R skills/eufy-cli "$HOME/.codex/skills/"
```

Reload/restart the agent if its skill catalog was already loaded. Ask, for example, “Use the Eufy CLI to check motion detection” or “Disable motion detection on my doorbell.” The CLI and local configuration must be installed separately; the skill includes no identifiers or credentials. Other SKILL.md-compatible agents can use their own skill directory.

## Development

```sh
python3 -m unittest discover -s tests -v
npm test
./bin/eufy --version
```

CI uses synthetic fixtures, never a live camera or private metadata. Before publishing diagnostics, remove local paths, aliases, LAN addresses, serials, account/P2P identifiers and event contents. Never submit a config, app archive or packet capture to an issue.

MIT licensed; see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md). P2P interoperability uses [bropat/eufy-security-client](https://github.com/bropat/eufy-security-client).
