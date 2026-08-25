# Security policy

Feature Fix Swarm installs agent skills and hooks, executes verification
commands, handles temporary Codex authentication state, and can participate in
push/deploy workflows. Security reports are taken seriously.

## Supported versions

| Version | Security fixes |
| --- | --- |
| Latest 5.x release | Supported |
| Earlier releases | Upgrade required |

Security fixes are released on the latest line. If a migration concern blocks
an upgrade, include that constraint in the private report.

## Report a vulnerability privately

Use GitHub's private vulnerability reporting flow:

<https://github.com/austinmao/feature-fix-swarm/security/advisories/new>

If that form is unavailable, email the maintainer address listed on
[@austinmao's GitHub profile](https://github.com/austinmao) with the subject
`Feature Fix Swarm security report`.

Please include:

- the affected version or commit;
- the component and required configuration;
- reproduction steps or a minimal proof of concept;
- the likely impact and any known mitigations; and
- whether you plan to request public credit.

Do not include live credentials, access tokens, or sensitive third-party data.
Use placeholders and coordinate a secure transfer only if it becomes necessary.

The maintainer will acknowledge a complete report as soon as practical,
coordinate validation and a fix, and agree on disclosure timing before making
the issue public. Please do not open a public issue for an undisclosed
vulnerability.

## Security boundaries

- FFS secures its installer, gates, skills, hooks, and runtime wrapper. It does
  not audit the consuming repository or guarantee the behavior of Claude,
  Codex, GSD, optional integrations, or other third-party agents.
- A preflight manifest is a trusted project file. Probe entries use direct
  structured argv execution without an implicit shell, but they still launch
  the named executable with the user's permissions.
- `network_mode: enabled` grants network access generally. `network_purpose`
  is audit metadata and does not enforce a destination allowlist.
- An explicit, run-bound `sandbox:danger-full-access` grant authorizes
  unsandboxed execution for that run. Review the plan and grant ledger first.
- FFS never needs credential values in bug reports, logs, or committed files.

See the [public-launch security review](docs/security-audit-2026-08-01.md) for
the latest documented audit scope and remaining repository-setting actions.
