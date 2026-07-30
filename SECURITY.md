# Security Policy

3xui-watchdog holds credentials (panel username/password or API token,
optionally Telegram bot tokens/webhook URLs) and has the ability to disable
proxy clients and, in the opt-in "nuclear" fallback mode, restart Xray-core.
Treat any vulnerability here as security-relevant, not just a bug.

## Scope

In scope:
- The watchdog daemon itself (`src/xui_watchdog/`)
- The Docker image and systemd unit as shipped in this repo
- The CI/release pipeline (supply-chain concerns: dependency pinning,
  artifact signing, etc.)

Out of scope:
- Vulnerabilities in 3x-ui or Xray-core themselves — report those upstream at
  https://github.com/MHSanaei/3x-ui and https://github.com/XTLS/Xray-core
- Misconfiguration on your own server (e.g. exposing the panel or the Xray
  gRPC API port to the public internet — see the README's networking notes)

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security reports.

Instead, use GitHub's private vulnerability reporting for this repo
(Security tab → "Report a vulnerability"), or email the address listed in
the repo's GitHub profile if private reporting isn't enabled yet.

Please include:
- Affected version/commit
- Steps to reproduce, or a PoC if applicable
- Impact (e.g. credential exposure, unauthorized client removal, privilege
  escalation, DoS via forced restarts)

## Response targets

- Acknowledgement: within 5 business days
- Initial assessment/triage: within 10 business days
- Fix or mitigation timeline: communicated once triaged, prioritized by
  severity (credential exposure and remote code execution are treated as
  highest priority)

## Non-negotiable scope reminder

This tool is built to enforce quota/expiry policy on servers **the operator
already controls and has credentials for**. It is not designed or intended
to interact with, probe, or affect any 3x-ui/Xray instance you do not
administer. Reports involving misuse outside that scope will be treated as
out of scope for this project's security process.
