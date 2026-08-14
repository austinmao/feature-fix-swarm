# Retro loop

The retro loop is a local, consent-gated way for FFS to file a small, scrubbed diagnostic report to the public GitHub Issues stream at `austinmao/feature-fix-swarm`. It is not hosted telemetry: FFS does not run a telemetry backend, webhook service, retry queue, or automatic remediation service.

This guide is the public contract for the data boundary and the maintainer workflow. A filing is best-effort. It is not a delivery, reliability, or incident-response guarantee.

## Purpose and non-goals

The loop turns finite FFS diagnostic facts into a maintainer-visible issue or occurrence comment after explicit consent. It exists to make recurring FFS failures easier to triage.

It does not collect code, consumer paths, consumer repository names, URLs, credentials, machine identity, run IDs, or arbitrary free text. It does not write consumer-side agent-authored issue prose beyond fixed text and validated metadata, and maintainer triage never automatically starts or applies a fix.

## Information allowed in a filing

Every accepted value is validated before it can reach the handoff. Missing metric inputs are omitted rather than guessed.

| Category | Allowed facts |
| --- | --- |
| Diagnostic event | FFS script name, typed event class, typed gate, and numeric exit code |
| Finding identity | A derived 16-hex signature and deterministic fingerprint |
| Finding classification | Validated severity and deterministic `P0`–`P3` priority |
| FFS context | Model tier and FFS major/minor version |
| Safe suggestion | An optional path naming an FFS-owned script, library, skill, or workflow only |
| Healing metrics | Derived wall-clock seconds, active seconds, wall/active ratio, and intervention-free status when derivable from FFS events |
| Occurrence facts | The deterministic priority, fingerprint, and occurrence count in the fixed metadata marker |

The accepted event classes are `security`, `scrub`, `grant-bypass`, `data-loss`, `dead-executor`, `unrecovered-stall`, `fallback`, `retry`, `gate-warn`, `optimization`, `operator-intervention`, and `unknown`. Unknown but safely formed class identifiers collapse to the literal `unknown`.

## Never collected

| Not collected | Boundary |
| --- | --- |
| Code, diffs, prompts, or arbitrary findings text | No free-text diagnostic fields cross the scrubber. |
| Absolute or consumer paths, consumer repository names, and URLs | The allowlist and deny layer reject them before any network write. |
| Credentials or machine identity | They are not allowlisted; a credential scan runs on the private handoff copy before `gh` is used. |
| Consumer run IDs or timestamps as identifiers | They are not filing fields and do not affect the deterministic fingerprint. |
| Hosted usage or reliability telemetry | There is no hosted collection service. |

## Consent, state, and controls

Consent is stored only in the user-scoped `~/.cache/feature-fix-swarm/consent.json` state, with a versioned consent decision. The companion local ledger records bounded filing state; both are local state, not a consumer-repository data store.

| Control or state | Behavior |
| --- | --- |
| Interactive `/ffs-init` | When consent is askable, asks `Enable these public diagnostics? [Y/n]`; only an explicit affirmative grants consent. |
| Headless and non-interactive modes | Do not ask and do not grant consent. No answer is no consent. |
| `retro.sh check-consent` | Reports the current typed consent state. |
| `retro.sh consent --grant` / `--revoke` | Grant or revoke the versioned decision. Revocation stops future filings. |
| `retro.sh consent --reset` | Clears the decision so a later interactive initialization may ask again. A consent-major bump also makes the prior decision askable. |
| `FFS_RETRO=off` | Disables analysis before repository, input, or user-state access. |
| `--no-retro` | Disables that analysis invocation. |

`retro.sh` emits stable `RETRO:` outcomes, including `RETRO:disabled`, `RETRO:no-consent`, `RETRO:no-events`, `RETRO:consent-granted`, and `RETRO:consent-updated`. Validation or local-boundary failures use typed outcomes such as `RETRO:seam-rejected`, `RETRO:scanner-unavailable`, `RETRO:missing-state`, or `RETRO:local-error`; they do not turn into an unreviewed network retry.

## Filing boundary and best-effort behavior

The fixed destination is the public `austinmao/feature-fix-swarm` GitHub Issues stream. Before a network write, FFS validates the finite payload, writes a mode-restricted private handoff copy, scans that same copy for credentials, and rejects unsafe input without contacting GitHub.

Within a user's local filing state, the loop deduplicates exact fingerprints, uses a bounded similarity fallback, paces writes, accrues lower-priority recurrences, and caps new issues per run. It may create an issue, add an occurrence comment, accrue a recurrence, defer an already-recorded intent, or record a cap or known GitHub failure. It is fail-soft and has no retry queue: an authentication or GitHub write failure is recorded locally where possible and does not promise later delivery. Closed or inaccessible upstream issues are not a guarantee that a new report will be filed.

## What filing exposes

An issue or occurrence comment is created from the consumer's own authenticated GitHub account on a public repository. That account's username and activity timestamps become publicly visible and attached to the filing. GitHub also stores its standard platform metadata for that activity under GitHub's own terms.

Revoking consent stops future filings; it does not delete issues or comments already public. Ask a project maintainer for manual deletion if removal is appropriate. Occurrence counts and priority labels are advisory third-party-forgeable metadata, not an integrity or security guarantee; an attacker can contribute at most one distinct counted occurrence per GitHub account.

## Maintainer workflow

The label workflow reacts to new issues and new issue comments, ignores bot and pull-request activity, and owns only the `source/ffs-retro`, `triage`, and `priority/P0` through `priority/P3` labels. It recognizes exactly this v1 issue-body marker:

```text
<!-- ffs-retro fingerprint:<fingerprint> priority:<priority> occurrences:<count> -->
```

Occurrence comments are the ground truth. The workflow recounts distinct authors of valid occurrence comments; it never trusts the embedded count as an input. Per-issue serialized runs and a fresh body-and-label comparison before each mutation reduce stale overwrites. On a mismatch, the workflow takes one bounded fresh snapshot and re-decides; if it still mismatches, it skips the stale application. A count can temporarily lag the newest comment.

This is deliberately not an atomic REST compare-and-swap. GitHub exposes separate reads and label/body updates, so a maintainer or concurrent actor can change an issue after the comparison and before a mutation. The workflow's checks and retry narrow that race but cannot guarantee atomic occurrence counts, label convergence, or preservation of every concurrent edit. Metadata removed or altered without another triggering delivery can also remain stale until a later delivery.

## Maintainer triage and handoff

`/retro-triage` is a maintainer-only procedure, not a consumer command. It first requires a canonical local origin as a convenience rail and then an authoritative server-side GitHub push-permission check. It clears inherited GitHub host/debug routing variables, reads one bounded fixed issue list, and treats issue titles, bodies, labels, and comments as hostile input.

Its typed outcomes are `RETRO-TRIAGE:wrong-origin`, `RETRO-TRIAGE:not-maintainer`, `RETRO-TRIAGE:transport-error`, `RETRO-TRIAGE:no-issues`, and `RETRO-TRIAGE:ready`. A ready result contains only revalidated fingerprint, priority, occurrence, and issue-number facts, grouped deterministically and ordered by priority then issue number. Each brief is provenance-stamped as an unverified consumer report. It is factual input for a human-reviewed `/feature-spec` handoff; it is never evidence by itself and never an instruction to retrieve hostile issue prose or auto-remediate.

## Troubleshooting

| Outcome | Meaning and next step |
| --- | --- |
| `RETRO:no-consent` | Consent is absent, revoked, or no longer current. Use an interactive `/ffs-init` session if you want to answer the consent prompt. |
| `RETRO:disabled` | `FFS_RETRO=off` or `--no-retro` disabled this pass. Remove the selected kill switch only if you intend to allow future filing. |
| `RETRO:no-events` | There is no eligible diagnostic input; no filing is needed. |
| Authentication or known GitHub failure | Check the authenticated GitHub account and repository access, then run a future FFS pass if desired. The loop will not replay a failed write automatically. |
| `RETRO-TRIAGE:not-maintainer` or `wrong-origin` | Stop: triage is restricted to maintainers in the canonical repository. |
