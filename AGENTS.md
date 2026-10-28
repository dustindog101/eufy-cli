# Agent development instructions

Read README.md and `skills/eufy-cli/SKILL.md` for supported behavior. Preserve the normal runtime's strict standalone-config/no-app-fallback contract and exact configured-host UDP filter. Keep the offline API and cloud/TCP/HTTP/fetch guards. Do not invent numeric account-role mappings or substitute device/account identities.

Motion writes must retain fresh-original-state reads, acknowledgement correlation and independent fresh readback. Normal requested writes leave the chosen state; an explicitly requested validation should restore its original state. Do not perform device writes merely to test unrelated code. No incidental event deletion, reset, storage maintenance, account/enrollment or network changes.

Public tests and documentation must contain only synthetic identifiers and addresses. Never commit configs, tokens, keys, serials, app archives, packet captures, event contents or personal absolute paths. Keep development notes about behavior/source/test outcomes free of personal session details. Do not commit node_modules or generated skill archives.

Run `python3 -m unittest discover -s tests -q`, `npm test`, and `git diff --check` after relevant changes. Fixtures do not prove live device support. Preserve honest support limits: motion and local recording-time retrieval tested on the documented pair; full app event coverage and HomeBase timezone unverified; video and other settings absent.
