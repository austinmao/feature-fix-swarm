# Public-launch security review — 2026-08-01

This review covered the `austinmao/feature-fix-swarm` repository at
`d8ff516` (`v5.0.5`) plus the public-launch changes described below. It is a
maintainer security review, not an independent certification or penetration
test.

## Scope

- All reachable Git history and tags for exposed credentials and sensitive
  filenames
- Current Python and shell execution surfaces
- npm dependency graph and lockfile
- GitHub Actions permissions and dependency pinning
- GitHub repository security settings and community health files
- Committed planning, spike, evaluation, and fixture artifacts

## Results

| Check | Result | Notes |
| --- | --- | --- |
| Full-history Gitleaks 8.30.1 scan | Pass | 225 commits scanned; no findings |
| GitHub secret-scanning alerts | Pass | Zero alerts returned; scanning and push protection are enabled |
| Sensitive filename search across history | Pass | No tracked `.env`, private-key, keystore, or credential files found |
| Large-object review | Pass | Largest historical objects are text files; no large binary or archive payloads found |
| npm audit before remediation | Fail | One high and two moderate transitive advisories |
| npm audit after lockfile remediation | Pass | `fast-uri` 3.1.5, `@hono/node-server` 2.0.12, and `@modelcontextprotocol/sdk` 1.30.0; zero reported vulnerabilities |
| Bandit production scan | Pass after change | Removed the sole high finding by replacing shell-interpreted probe strings with direct structured argv execution |
| ShellCheck | Pass | Executable shell surface passes at warning severity |
| Branch protection | Open setting | `main` was unprotected at review time |
| Dependabot security updates | Open setting | Disabled at review time; configuration is now committed but the repository feature must be enabled |
| Code scanning | Pending merge | No prior analysis existed; CodeQL and OpenSSF Scorecard workflows are now included |
| Commit/tag signatures | Advisory | Most commits and several release tags are unsigned or lightweight |

## Changes made for public launch

- Remediated the vulnerable transitive npm lockfile entries without changing
  the exact GSD 1.9.1 direct pin.
- Replaced preflight probe command strings with non-empty JSON `argv` arrays.
  Probes execute without an implicit shell, expand environment placeholders
  without storing resolved values, and fail closed on invalid shapes.
- Added least-privilege workflow permissions, timeouts, immutable action SHAs,
  Dependabot configuration, CodeQL, and OpenSSF Scorecard.
- Added a private vulnerability-reporting policy and standard community files.
- Removed stale internal `.planning` state and obsolete Ruflo spike evidence.
  Reproducible GPT-5.6 evaluation inputs/results and test-referenced specs were
  retained deliberately.

## Repository settings still required

These are GitHub-side controls and do not take effect merely because a file is
committed:

1. Protect `main` (or create a ruleset) with required pull requests, required
   CI and CodeQL checks, conversation resolution, and no force pushes or branch
   deletion.
2. Enable Dependabot alerts and security updates.
3. Enable private vulnerability reporting.
4. Enable secret-scanning non-provider patterns and validity checks if they are
   available for the repository.
5. Require full-length SHA pins for Actions at the repository or organization
   policy level.
6. Prefer signed annotated release tags and signed release commits going
   forward. Rewriting old commits solely to add signatures is not recommended.

## Limitations

Automated scanning cannot prove the absence of every secret or vulnerability.
This review did not exercise live Claude/Codex provider infrastructure, audit
the internals of pinned upstream dependencies, or test Windows (which is out
of scope). Runtime behavior still depends on the permissions granted to the
host CLI and on the consuming repository's own code and workflows.

Report a suspected vulnerability privately through [the security
policy](../SECURITY.md).
