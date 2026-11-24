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
  grep -F 'REVIEW_TIER_BASE=main "$FFS_T/scripts/gsd/review-tier.sh" --all' skills/review-gate/SKILL.md
}

@test "INT-001: LIGHT tier imperatively SKIPs Pass 2 and Pass 3" {
  grep -F 'SKIP Pass 2' skills/review-gate/SKILL.md
  grep -F 'SKIP Pass 3' skills/review-gate/SKILL.md
}

@test "INT-001: FULL tier names adversary-host.sh for the extra cross-model adversary" {
  grep -F 'adversary-host.sh' skills/review-gate/SKILL.md
}

@test "host routing is shared by ordinary GSD drives and ship review" {
  grep -F 'adversary-host.sh' scripts/gsd/gsd-run.sh
  grep -F 'adversary-host.sh' scripts/gsd/review-gate-command.sh
}

@test "Pass 2 resolves a typed judgment request on the opposite host" {
  grep -F 'REVIEW_MODEL_REQUEST=' skills/review-gate/SKILL.md
  grep -F '"kind":"tier","name":"judgment"' skills/review-gate/SKILL.md
  grep -F 'adversary_invoke_typed_request "$REVIEW_KIND" "$FALLBACK_KIND"' skills/review-gate/SKILL.md
}

@test "honest verifier is also opposite-host and has no Claude-only Task primitive" {
  grep -F 'adversary_invoke_typed_request "$HV_KIND" "$HV_FALLBACK_KIND"' skills/review-gate/SKILL.md
  ! grep -F 'Task({ subagent_type: "gsd-verifier"' skills/review-gate/SKILL.md
}

@test "honest verifier requires exactly one anchored verdict" {
  grep -F 'HV_VERDICT_COUNT=' skills/review-gate/SKILL.md
  grep -F '[ "$HV_VERDICT_COUNT" -ne 1 ]' skills/review-gate/SKILL.md
  grep -F 'duplicate or conflicting final verdict' skills/review-gate/SKILL.md
}

@test "INT-001: FULL-tier adversary_invoke passes a real model, not the review bin" {
  grep -F 'adversary_invoke_typed_request "$_adv_kind" "$_adv_fallback"' skills/review-gate/SKILL.md
  ! grep -F 'adversary_invoke_typed_request "$_adv_kind" "$_adv_fallback" 480 "$REVIEW_BIN"' skills/review-gate/SKILL.md
}

@test "INT-001: FULL-tier adversary fails closed when both hosts are unavailable" {
  grep -F 'FULL-tier mandatory adversary unavailable on both hosts' skills/review-gate/SKILL.md
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

@test "INT-001: review-gate retains the 1.9 contract after host-neutral bump" {
  grep -F 'version: "1.20.1"' skills/review-gate/SKILL.md
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

# --- F3: reviewer prompts ask for the smallest change (grep snapshots) ---

@test "F3 P4: Pass 1 asks for the smallest fix with a SCOPE field and drops doc-gap bullets" {
  grep -qF -- '- Unhandled exceptions on paths the diff adds' skills/review-gate/SKILL.md
  grep -qF -- '- Added code the goal does not need; existing code this diff makes deletable' skills/review-gate/SKILL.md
  grep -qF 'smallest fix at the root cause' skills/review-gate/SKILL.md
  grep -qF 'SCOPE: <in-spec | needs-new-requirement>' skills/review-gate/SKILL.md
  [ "$(grep -c 'Documentation gaps' skills/review-gate/SKILL.md)" -eq 0 ]
}

@test "F3 P5: adversarial prompt asks for the smallest fix" {
  grep -qF 'FIX (the smallest fix)' skills/review-gate/SKILL.md
}

@test "F3 P6: Pass 3 drops the missing-test bullets" {
  [ "$(grep -c 'Missing integration tests for new API endpoints' skills/review-gate/SKILL.md)" -eq 0 ]
  [ "$(grep -c 'Missing E2E tests for new user-facing flows' skills/review-gate/SKILL.md)" -eq 0 ]
  [ "$(grep -c 'Edge cases not tested' skills/review-gate/SKILL.md)" -eq 0 ]
}

@test "F3 P8: HV_PROMPT bounds the diff by the spec Intent block" {
  grep -qF "does not exceed the spec's Intent block" skills/review-gate/SKILL.md
}

@test "F2: findings-add maps SCOPE needs-new-requirement to --needs-requirement" {
  grep -qF '${FINDING_NEEDS_REQ:+--needs-requirement}' skills/review-gate/SKILL.md
}

@test "F2: fail text replaces the unconditional fix rule" {
  [ "$(grep -c 'Fix all CRITICAL and HIGH' skills/review-gate/SKILL.md)" -eq 0 ]
  grep -qF 'CRITICAL and security findings: fix.' skills/review-gate/SKILL.md
  grep -qF 'findings-queue retag <sig> --in-spec' skills/review-gate/SKILL.md
}

# ── findings-queue probe: run the fence itself ───────────────────────────────

# Runs the probe fence in a scratch repo. $HOME/.claude/lib/feature-fix-swarm holds
# both candidates: lib/gates.py (first) and gates.py (second).
probe_setup() {
  local skill="$PWD/skills/review-gate/SKILL.md"
  PROBE="$BATS_TEST_TMPDIR/probe"; FFS_HOME="$PROBE/home/.claude/lib/feature-fix-swarm"
  mkdir -p "$FFS_HOME/lib" "$PROBE/repo"
  awk '/^### Findings queue: capability probe/ {s=1} s && /^```bash/ {f=1; next} f && /^```/ {exit} f' "$skill" > "$PROBE/fence.sh"
  grep -q 'findings-queue list' "$PROBE/fence.sh"
  printf 'echo "GATES_PY=$GATES_PY DEGRADED=$DEGRADED_PERSISTENCE SIGS=$RESOLVED_SIGS"\n' >> "$PROBE/fence.sh"
  git -C "$PROBE/repo" init -q -b main
  git -C "$PROBE/repo" -c user.email=t@example.com -c user.name=t commit -q --allow-empty -m base
  printf 'import pathlib\npathlib.Path(__file__).with_suffix(".ran").touch()\nprint(%s)\n' \
    "'[{\"sig\": \"cafe\", \"resolved\": true}]'" > "$FFS_HOME/gates.py"
}

probe_run() {
  run env -u GATES_STORE -u FFS_T HOME="$PROBE/home" "$@" 'cd "$1" && . "$2"' _ "$PROBE/repo" "$PROBE/fence.sh"
}

probe_plant() {
  cp lib/gates.py "$FFS_HOME/lib/gates.py"
  mkdir "$PROBE/repo/.feature-fix-swarm"
  echo '{}' > "$PROBE/repo/.feature-fix-swarm/evidence.json"
  git -C "$PROBE/repo" add .feature-fix-swarm
}

@test "findings-queue probe: a refused evidence store ends the gate and tries no other runtime" {
  probe_setup
  probe_plant
  probe_run bash -c
  [ "$status" -eq 1 ]
  [[ "$output" == *"is tracked by git"* ]] || false
  [[ "$output" == *"GATE: FAIL"* ]] || false
  [ ! -e "$FFS_HOME/gates.ran" ]
}

@test "findings-queue probe: an unprovable evidence store ends the gate too" {
  probe_setup
  cp lib/gates.py "$FFS_HOME/lib/gates.py"
  echo broken > "$PROBE/repo/.git/index"
  probe_run bash -c
  [ "$status" -eq 1 ]
  [[ "$output" == *"GATE: FAIL"* ]] || false
  [[ "$output" != *"is tracked by git"* ]] || false
  [ ! -e "$FFS_HOME/gates.ran" ]
}

@test "findings-queue probe: noclobber neither hides a refusal nor degrades a healthy store" {
  probe_setup
  probe_run bash -C -c
  [ "$status" -eq 0 ]
  [[ "$output" == *"GATES_PY=$FFS_HOME/gates.py DEGRADED=0 SIGS=cafe"* ]] || false
  probe_plant
  probe_run bash -C -c
  [ "$status" -eq 1 ]
  [[ "$output" == *"GATE: FAIL"* ]] || false
}

@test "findings-queue probe: no temporary file ends the gate" {
  probe_setup
  probe_plant
  mkdir "$PROBE/bin"
  printf '#!/bin/sh\nexit 1\n' > "$PROBE/bin/mktemp"
  chmod +x "$PROBE/bin/mktemp"
  PATH="$PROBE/bin:$PATH" probe_run bash -c
  [ "$status" -eq 1 ]
  [[ "$output" == *"GATE: FAIL"* ]] || false
  [ ! -e "$FFS_HOME/gates.ran" ]
}

@test "findings-queue probe: a runtime without the command falls back to the next one" {
  probe_setup
  printf 'import sys\nprint("unknown command: findings-queue", file=sys.stderr)\nsys.exit(2)\n' > "$FFS_HOME/lib/gates.py"
  probe_run bash -c
  [ "$status" -eq 0 ]
  [[ "$output" == *"GATES_PY=$FFS_HOME/gates.py DEGRADED=0 SIGS=cafe"* ]]
}

@test "findings-queue probe: no working runtime degrades and does not end the gate" {
  probe_setup
  printf 'import sys\nsys.exit(3)\n' | tee "$FFS_HOME/lib/gates.py" > "$FFS_HOME/gates.py"
  probe_run bash -c
  [ "$status" -eq 0 ]
  [[ "$output" == *"GATES_PY= DEGRADED=1 SIGS="* ]]
}

@test "findings-queue probe: the queue is listed once, by the runtime that wins" {
  probe_setup
  cat > "$FFS_HOME/lib/gates.py" <<'PY'
import pathlib
with pathlib.Path(__file__).with_suffix(".calls").open("a") as f:
    f.write("x")
print('[{"sig": "beef", "resolved": true}]')
PY
  probe_run bash -c
  [ "$status" -eq 0 ]
  [[ "$output" == *"GATES_PY=$FFS_HOME/lib/gates.py DEGRADED=0 SIGS=beef"* ]] || false
  [ "$(cat "$FFS_HOME/lib/gates.calls")" = x ]
  [ ! -e "$FFS_HOME/gates.ran" ]
}
