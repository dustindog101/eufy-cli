"""Offline help examples and focused guides; no configuration or device access."""

EXAMPLES = {
    'devices': 'eufy devices\neufy status --device "Front Door"\n\nOne configured doorbell is selected automatically.',
    'status': 'eufy status\neufy status --json\neufy status --cache-only\n\nFresh motion is read over LAN. Saved metadata is not fresh device state.\nConnection problems: eufy help troubleshooting',
    'refresh': 'eufy refresh\neufy refresh --cache-only\n\nSame fresh motion check as status; no routine login or manual refresh is needed.',
    'motion': 'eufy motion off\neufy motion on\neufy motion off --dry-run\n\nIf the app shows Admin/Owner, confirm once:\n  eufy configure --confirm-admin\nGuest access cannot change settings.\nIf acknowledgement/readback fails, check eufy status before another write.',
    'configure': 'eufy configure --from-app          # First setup on Mac\neufy configure --confirm-admin     # Only if the app says Admin/Owner\neufy configure --from-discovery    # Save a changed matching LAN endpoint\n\nWindows: import a profile from a trusted machine with eufy session import FILE.\nExisting profiles are preserved; --replace explicitly reimports app metadata.\nSetup walkthrough: eufy help setup',
    'session': 'eufy session export PRIVATE_PROFILE.eufy-profile.json\neufy session import PRIVATE_PROFILE.eufy-profile.json\n\nTransfer the unencrypted file privately between your trusted machines.\nMore help: eufy help session export / eufy help session import',
    'session export': 'eufy session export PRIVATE_PROFILE.eufy-profile.json\n\nChoose an existing private directory outside your source checkout.\nThe file contains device/account identifiers. Never commit or publish it.',
    'session import': 'eufy session import PRIVATE_PROFILE.eufy-profile.json\neufy status\n\nImport validates and installs the profile offline. Status then checks LAN access.\nUse --replace only when replacing your existing profile is intended.',
    'discover': 'eufy discover\neufy discover --timeout 2\n\nRead-only LAN lookup; it does not save a profile or grant permissions.\nFor a configured HomeBase that moved: eufy configure --from-discovery',
    'events': 'eufy events                          # Today\neufy events yesterday\neufy events day-before-yesterday\neufy events YYYY-MM-DD                # A specific day\neufy events yesterday --json\neufy events --since YYYY-MM-DD        # Through today\neufy events days                     # HomeBase activity dates, not totals\n\nUp to 200 recordings from the last 30 days; pagination is automatic.\nPartial results are marked. Empty results do not prove there were no events.\nTimes preserve the HomeBase clock; timezone/full app-list coverage unverified.\nUse a day or --since, not both. Activity dates do not accept --device.',
    'limits': 'eufy limits\neufy limits --json\n\nOffline support information. No device connection or profile required.',
}

GUIDES = {
    'setup': '''Eufy setup

1. Install Python 3.11+, Node.js 22+ and the CLI dependencies (see README).
2. On a Mac with saved Eufy app metadata:
     eufy configure --from-app
   On Windows, import a profile transferred privately from a trusted machine:
     eufy session import PRIVATE_PROFILE.eufy-profile.json
3. Stay on the HomeBase LAN and check:
     eufy status
4. If this shared doorbell's app permission says Admin/Owner, confirm once:
     eufy configure --confirm-admin
   Then request a change with eufy motion off or eufy motion on.

Guest permission cannot change settings. Discovery alone cannot provision a profile.
Normal use needs no cloud login, running app, daemon or routine re-login.
More: eufy help configure / eufy help session import
''',
    'troubleshooting': '''Eufy troubleshooting

Missing profile:
  eufy help setup

Cannot connect:
  Stay on the same LAN as the HomeBase, then run eufy status.
  Status and motion try one identity-matched recovery after a safe connection failure.
  If event queries cannot reach a moved HomeBase, verify and save its endpoint:
    eufy configure --from-discovery

Permission denied:
  Check the app's permission label for this doorbell.
  For confirmed Admin/Owner: eufy configure --confirm-admin (once).
  For Guest: ask the owner for Admin access.

Motion acknowledgement/readback failed:
  The setting may have changed. Run eufy status before another write.

Missing recording times or partial results:
  Use one day: eufy events yesterday or eufy events YYYY-MM-DD.
  Results are recording metadata, capped at 200; partial output states the reason.
  Empty output is not proof of zero events. Activity-date values are not event totals.
  Clock timezone and agreement with every app event remain unverified.

For structured errors: add --json. Support limits: eufy limits.
Do not publish private profiles, identifiers, addresses or event contents in issues.
''',
    'agents': '''Eufy for agents and personal assistants

Install the eufy-cli skill folder from this repository in your assistant's skill directory.
Codex example (from the repository root):
  mkdir -p "$HOME/.codex/skills"
  cp -R skills/eufy-cli "$HOME/.codex/skills/"
Reload the assistant if needed. CLI dependencies and the private profile are separate.
Other assistants can use skills/eufy-cli/SKILL.md as their tool instructions.

Use --json to read results and errors:
  eufy status --json
  eufy motion off --json
  eufy events day-before-yesterday --json

A motion request leaves the requested state, verified by fresh readback.
History is recording metadata; report partial output and unverified clock timezone.
Never infer event totals from activity-date values or retry an uncertain write.
The skill contains no device identifiers or credentials.

Skill: https://github.com/dustindog101/eufy-cli/tree/main/skills/eufy-cli
''',
}
