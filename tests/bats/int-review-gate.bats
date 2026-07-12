#!/usr/bin/env bats
# int-review-gate.bats — grep-pins that review-gate SKILL.md actually wires
# the two Phase-1 levers it owns: review-tier.sh (INT-001, tier-scoped
# passes) and lib/gates.py findings-queue (INT-004, persisted findings +
# resolved-sig dedup). Mirrors setup-install.bats / int-consult-levers.bats's
# static-assertion pattern — pins exact command lines + section ORDER via
# `grep -n` line-number comparisons, not bare substring presence, so a
# reworded-but-inert prose edit can't pass a stale wiring.
#
# NOTE: INT-002 (gates.py cross-subcommand same-store isolation) is a pytest
# test, not a grep pin — it lives in the Python test suite, not here.

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

# ── INT-001: tier-selection preamble ─────────────────────────────────────────

@test "INT-001: review-tier.sh is invoked" {
  grep -F 'review-tier.sh' skills/review-gate/SKILL.md
}

@test "INT-001: --all tier call pins REVIEW_TIER_BASE=main on review-tier.sh --all" {
  grep -F 'REVIEW_TIER_BASE=main scripts/gsd/review-tier.sh --all' skills/review-gate/SKILL.md
}

@test "INT-001: LIGHT tier imperatively SKIPs Pass 2 and Pass 3" {
  grep -F 'SKIP Pass 2' skills/review-gate/SKILL.md
  grep -F 'SKIP Pass 3' skills/review-gate/SKILL.md
}

@test "INT-001: FULL tier names adversary-host.sh for the extra cross-model adversary" {
  grep -F 'adversary-host.sh' skills/review-gate/SKILL.md
}

@test "INT-001: FULL-tier adversary_invoke passes a real model, not the review bin" {
  grep -F 'adversary_invoke "$_adv_kind" 480 "$_adv_model" "$_adv_effort"' skills/review-gate/SKILL.md
  ! grep -F 'adversary_invoke "$_adv_kind" 480 "$REVIEW_BIN"' skills/review-gate/SKILL.md
}

@test "INT-001: FULL-tier adversary warns visibly when it cannot run" {
  grep -F 'FULL-tier adversary unavailable' skills/review-gate/SKILL.md
}

@test "INT-001: tier selection is fail-safe to standard on lever failure" {
  grep -F 'falling back to standard' skills/review-gate/SKILL.md
}

@test "INT-001: --file parsing is fixed (git diff HEAD -- <path>)" {
  grep -F 'git diff HEAD -- ' skills/review-gate/SKILL.md
}

@test "INT-001: gate header prints the tier + reason" {
  grep -F 'Tier: <tier>' skills/review-gate/SKILL.md
}

@test "INT-001: tier selection never suppresses the honest-verifier pass" {
  grep -F 'never suppresses an otherwise-eligible honest-verifier' skills/review-gate/SKILL.md
}

@test "INT-001: version bumped to 1.6.0" {
  grep -F 'version: "1.6.0"' skills/review-gate/SKILL.md
}

@test "spec 005: pass-1 finding format carries CAUSE/PROVENANCE/PROOF" {
  grep -F 'CAUSE: <root cause' skills/review-gate/SKILL.md
  grep -F 'confidence: clear|likely|unknown' skills/review-gate/SKILL.md
  grep -F 'PROOF: <how to verify' skills/review-gate/SKILL.md
}

@test "spec 005: adversarial prompt requests provenance fields" {
  grep -F 'SEVERITY/FILE/LINE/ISSUE/CAUSE/PROVENANCE' skills/review-gate/SKILL.md
}

@test "INT-001: ORDER — Tier selection section precedes Pass 1" {
  tier_line=$(grep -n '### Tier selection' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  pass1_line=$(grep -n '### Pass 1' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  [ -n "$tier_line" ]
  [ -n "$pass1_line" ]
  [ "$tier_line" -lt "$pass1_line" ]
}

# ── INT-004: findings-queue recording ────────────────────────────────────────

@test "INT-004: findings-queue is invoked" {
  grep -F 'findings-queue' skills/review-gate/SKILL.md
}

@test "INT-004: GATES_PY resolution is a capability probe, not first-exists" {
  grep -F 'findings-queue list >/dev/null 2>&1' skills/review-gate/SKILL.md
}

@test "INT-004: findings are recorded and resolved" {
  grep -F 'findings-queue add' skills/review-gate/SKILL.md
  grep -F 'findings-queue resolve' skills/review-gate/SKILL.md
}

@test "INT-004: deduped count is reported" {
  grep -F 'deduped:' skills/review-gate/SKILL.md
}

@test "INT-004: degraded persistence is surfaced (env flag + footer)" {
  grep -F 'DEGRADED_PERSISTENCE' skills/review-gate/SKILL.md
  grep -F 'findings persistence: DEGRADED' skills/review-gate/SKILL.md
}

@test "INT-004: every gates.py call runs from REPO_ROOT" {
  grep -F 'cd "$REPO_ROOT"' skills/review-gate/SKILL.md
}

@test "INT-004: ORDER — resolved-sig consult precedes Pass 1" {
  consult_line=$(grep -n 'findings-queue list' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  pass1_line=$(grep -n '### Pass 1' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  [ -n "$consult_line" ]
  [ -n "$pass1_line" ]
  [ "$consult_line" -lt "$pass1_line" ]
}

@test "INT-004: ORDER — findings-queue add recording lands between Merge-and-rank and Output-and-exit" {
  merge_line=$(grep -n '### Merge and rank' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  add_line=$(grep -n 'findings-queue add' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  output_line=$(grep -n '### Output and exit' skills/review-gate/SKILL.md | head -1 | cut -d: -f1)
  [ -n "$merge_line" ]
  [ -n "$add_line" ]
  [ -n "$output_line" ]
  [ "$merge_line" -lt "$add_line" ]
  [ "$add_line" -lt "$output_line" ]
}
