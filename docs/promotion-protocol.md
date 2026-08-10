# Promotion Protocol

Twelve rules for moving a change from development through staging into production
without rebuilding it, guessing at drift, or shipping unproven artifacts. Each rule
below is one sentence, followed by the mechanism that enforces it today.

1. Build the artifact once; promote that same artifact through every environment, never rebuild it per environment (Enforcement: the image-build workflow's build-and-push job).
2. Pin artifact identity by content digest, never by a mutable tag (Enforcement: the digest resolver, consumed by the rollout script's digest-pin flag).
3. Keep environment configuration scoped per environment and outside the artifact itself (Enforcement: the environment registry — config/environments.yaml, checked by bash scripts/gsd/env-registry.sh check).
4. Reference secrets by name from the secret store; never bake a secret value into the artifact or the repo (Enforcement: per-environment secret-store configs, referenced from the environment registry — config/environments.yaml; bash scripts/gsd/env-registry.sh check exits 2 on any secret-value leak finding).
5. Declare every environment in the environment registry with a verified date (Enforcement: the environment registry's per-host rows in config/environments.yaml; bash scripts/gsd/env-registry.sh check prints a stale-verified advisory — advisory only, never a gate).
6. Staging mirrors production topology at reduced scale, using seeded data only (Enforcement: the parity manifest and its read-only audit lever).
7. Nothing reaches production without recorded proof that the exact artifact already passed the previous environment (Enforcement: the evidence ledger's promotion-evidence type and its pre-grant precondition check).
8. Promotion proof is bound to the exact artifact identity and expires after a bounded window (Enforcement: the evidence ledger's promotion-record and promotion-check logic, artifact-matching plus TTL).
9. Fleet-wide promotion goes canary first, then the rest of the fleet, with a recorded revert lever (Enforcement: the fleet promotion lever's canary-required guard and revert command).
10. Environment mutation happens only through a re-runnable CLI lever, never a manual console clickpath (Enforcement: lever scripts and their accompanying test suites).
11. Environment drift is machine-detected on a recurring basis, not tracked from memory (Enforcement: the parity manifest and its read-only audit lever).
12. An emergency bypass exists, is restricted to an operator, and is loudly and durably recorded (Enforcement: the evidence ledger's operator-only emergency escape, grant- and reason-gated).

## Plan-wall pass policy: diminishing returns + wall-the-diff (operator decision, 2026-08-08)

The plan wall (`scripts/gsd/plan-wall.sh`) no longer requires zero unresolved
HIGH findings on plan prose — three consecutive specs (006/007/008) capped at
their wall round limits with every finding real, narrow, and non-repeating,
because adversarial judgment-tier review of a security-adjacent DESIGN always
finds another legitimate contract-completeness gap in text. Two policies
replace the 0-HIGH criterion: **(b) diminishing returns** — an unresolved
CRITICAL always blocks; a HIGH-only round passes iff it reported strictly
fewer new HIGH/CRITICAL findings than the previous round (durable per-round
counts via `gates.py loop-round --note-count`; missing history is strict:
blocked). Residual HIGHs ride into execution as pinned executor assumptions —
left unresolved in the findings-queue and listed in the phase's
`WALL-RESIDUALS.md`. **(c) wall the diff** — those residuals are closed at
the executed-diff review: `review-gate-command.sh` feeds every
`WALL-RESIDUALS.md` to the ship reviewer as review focus, where each finding
is falsifiable against real code instead of prose. The findings-queue remains
the authoritative residual record; the manifest is a convenience surface.

## Accepted risk: loop credential scope (operator decision, 2026-08-07)

The autonomous loop's GitHub credential retains `repo` (including repo
administration) and `workflow` scopes. That means branch protection on `main`
and the workflow definitions the required checks come from are editable from
inside the loop — the one control an agent's shell cannot otherwise reach.
The recommended mitigation (a fine-grained PAT without `administration`/
`workflow` for autonomous runs, keeping the broad token interactive-only) was
explicitly declined by the operator; the risk is accepted as-is. Compensating
controls: the `tamper` CI job scans every PR diff for gate-weakening moves,
and GitHub's audit log records `protected_branch.*` mutations post-hoc.

## Maintenance obligations

The set of action-name prefixes that this protocol treats as production-targeting
(and therefore subject to Rule 7's promotion-proof precondition) is a fixed list
maintained in the enforcement code. That list fails OPEN: any new kind of
production-mutating action whose name does not match an existing prefix is *not*
automatically covered by the precondition. Introducing a new production-mutating
action type is therefore not just a feature change — it carries a standing
obligation to extend the prefix list in the same change, or the new action type
silently bypasses every rule in this document that the prefix list gates.
