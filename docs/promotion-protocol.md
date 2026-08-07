# Promotion Protocol

Twelve rules for moving a change from development through staging into production
without rebuilding it, guessing at drift, or shipping unproven artifacts. Each rule
below is one sentence, followed by the mechanism that enforces it today.

1. Build the artifact once; promote that same artifact through every environment, never rebuild it per environment (Enforcement: the image-build workflow's build-and-push job).
2. Pin artifact identity by content digest, never by a mutable tag (Enforcement: the digest resolver, consumed by the rollout script's digest-pin flag).
3. Keep environment configuration scoped per environment and outside the artifact itself (Enforcement: the environment registry).
4. Reference secrets by name from the secret store; never bake a secret value into the artifact or the repo (Enforcement: per-environment secret-store configs, referenced from the environment registry).
5. Declare every environment in the environment registry with a verified date (Enforcement: the environment registry's per-host rows).
6. Staging mirrors production topology at reduced scale, using seeded data only (Enforcement: the parity manifest and its read-only audit lever).
7. Nothing reaches production without recorded proof that the exact artifact already passed the previous environment (Enforcement: the evidence ledger's promotion-evidence type and its pre-grant precondition check).
8. Promotion proof is bound to the exact artifact identity and expires after a bounded window (Enforcement: the evidence ledger's promotion-record and promotion-check logic, artifact-matching plus TTL).
9. Fleet-wide promotion goes canary first, then the rest of the fleet, with a recorded revert lever (Enforcement: the fleet promotion lever's canary-required guard and revert command).
10. Environment mutation happens only through a re-runnable CLI lever, never a manual console clickpath (Enforcement: lever scripts and their accompanying test suites).
11. Environment drift is machine-detected on a recurring basis, not tracked from memory (Enforcement: the parity manifest and its read-only audit lever).
12. An emergency bypass exists, is restricted to an operator, and is loudly and durably recorded (Enforcement: the evidence ledger's operator-only emergency escape, grant- and reason-gated).

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
