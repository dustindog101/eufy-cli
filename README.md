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

## One-time setup

On a Mac with existing saved Eufy app metadata:

```sh
eufy configure --from-app
```

Use `--device ALIAS` if selection is ambiguous. An existing config is preserved unless `--replace` is explicitly supplied.

The CLI stores only the HomeBase private IPv4 address, station serial/P2P identifier, protocol account identifier, and doorbell serial/channel/model/firmware metadata. It does not copy passwords, login tokens, cloud DSKs, private keys, events or app settings. Configuration is stored at `$XDG_CONFIG_HOME/eufy-cli/local.json`, or `~/.config/eufy-cli/local.json` when unset; the directory is owner-only700 and the file600. Never publish or upload this file.

Normal commands never fall back to the app if config is missing or invalid. Discovery alone cannot currently bootstrap all identifiers needed for control. A configured CLI has no GUI/IPC dependency; physical termination of the app and complete HomeBase WAN isolation have not been validated.

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
```

This sends one bounded LAN lookup and updates the address only after a reply matches the configured station identifier. It does not read the app or change network settings.

## Support and limits

| Feature | Status |
| --- | --- |
| Fresh motion status; motion on/off with readback | Live validated on T8210 firmware2.2.4.6 + T8010 firmware3.4.2.6h |
| App-file-independent runtime | Live validated with app-cache loading blocked |
| Local discovery; standalone IP maintenance | Implemented, identity-checked |
| Event history | Unresolved: local queries can return empty despite app events |
| Live video, recording playback/download, other settings, snooze, security modes | Not implemented |
| Fresh provisioning from discovery alone | Not implemented |

Other models/firmware and live control on other operating systems are not verified. The local key derives from station serial and P2P DID, without a login-expiry timer; firmware, identity, permission or network changes can still break access. No promise of indefinite compatibility is made.

An operation has a13-second shared budget plus up to1.5 seconds cleanup; the bridge timeout is20 seconds. Experimental history queries allow at most20 items and a start date at most30 days ago. Empty history output is not proof that no events exist. No complete pagination or encrypted-recording access is provided.

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
