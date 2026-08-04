#!/usr/bin/env bats

setup() {
  PACKAGE_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COLLECTOR="$PACKAGE_ROOT/skills/spec-status/scripts/collect-status-facts.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/specs/340-target/evidence"
  cd "$REPO" || return 1
  git init -q -b main
  git config user.email test@example.com
  git config user.name "Spec Status Test"

  printf '{"proof":"spec-340"}\n' > specs/340-target/evidence/proof.json
  mkdir -p .planning/archive/spec-340/phases/01-archive
  printf '# Project: spec-340 archived\n' > .planning/archive/spec-340/PROJECT.md
  printf '%s\n' '- [x] ARCHIVE-340-MARKER' > .planning/archive/spec-340/ROADMAP.md
  printf '%s\n' 'status: archived-340-state' > .planning/archive/spec-340/STATE.md
  git add specs/340-target .planning/archive/spec-340
  git commit -q -m 'feat(spec-340): archive completed proof'

  mkdir -p .planning/phases/99-active .planning/run-state
  printf '# Project: spec-342 active\n\nPrior gsd project (spec-340, terminal/shipped) is preserved in the archive.\n' > .planning/PROJECT.md
  printf '%s\n' '- [ ] ACTIVE-342-MARKER' > .planning/ROADMAP.md
  printf '%s\n' 'status: active-342-state' > .planning/STATE.md
  printf '%s\n' 'ACTIVE-342-RUNNER-MARKER' > .planning/run-state/gsd-run.status
  git add .planning/PROJECT.md .planning/ROADMAP.md .planning/STATE.md .planning/run-state/gsd-run.status
  git commit -q -m 'chore(spec-342): activate newer planning'
}

@test "explicit archived spec ignores another spec's active planning and runner" {
  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: archive:.planning/archive/spec-340"* ]]
  [[ "$output" == *"ARCHIVE-340-MARKER"* ]]
  [[ "$output" == *"phase 01-archive:"* ]]
  [[ "$output" == *"status: archived-340-state"* ]]
  [[ "$output" == *"spec-history:"* ]]
  [[ "$output" == *"feat(spec-340): archive completed proof"* ]]
  [[ "$output" != *"ACTIVE-342-MARKER"* ]]
  [[ "$output" != *"ACTIVE-342-RUNNER-MARKER"* ]]
  [[ "$output" == *"no runner state for spec 340"* ]]
}

@test "explicit archived spec ignores conflicting unrelated active identities" {
  printf '# Project: spec-303 active\n' > .planning/PROJECT.md
  printf '%s\n' '# STATE — spec-349' 'Run id: spec-349' > .planning/STATE.md

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: archive:.planning/archive/spec-340"* ]]
  [[ "$output" == *"ARCHIVE-340-MARKER"* ]]
  [[ "$output" != *"ACTIVE-342-MARKER"* ]]
  [[ "$output" != *"ACTIVE-342-RUNNER-MARKER"* ]]
}

@test "matching active planning wins over an older archive" {
  printf '# Project: spec-340 active\n' > .planning/PROJECT.md
  printf '%s\n' '- [ ] ACTIVE-340-MARKER' > .planning/ROADMAP.md
  printf '%s\n' 'status: active-340-state' > .planning/STATE.md
  printf '%s\n' 'ACTIVE-340-RUNNER-MARKER' > .planning/run-state/gsd-run.status

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: active:.planning"* ]]
  [[ "$output" == *"ACTIVE-340-MARKER"* ]]
  [[ "$output" == *"ACTIVE-340-RUNNER-MARKER"* ]]
  [[ "$output" != *"ARCHIVE-340-MARKER"* ]]
}

@test "numeric STATE identity selects matching active planning" {
  printf '# Project: current run\n' > .planning/PROJECT.md
  printf '%s\n' 'spec_id: 340' 'status: active-340-state' > .planning/STATE.md

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: active:.planning"* ]]
  [[ "$output" == *"ACTIVE-342-MARKER"* ]]
  [[ "$output" != *"ARCHIVE-340-MARKER"* ]]
}

@test "unrelated active planning without a requested archive is suppressed" {
  rm -rf .planning/archive/spec-340

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: none (active planning belongs to another spec; no archive found for spec 340)"* ]]
  [[ "$output" != *"ACTIVE-342-MARKER"* ]]
  [[ "$output" != *"ACTIVE-342-RUNNER-MARKER"* ]]
  [[ "$output" == *"no runner state for spec 340"* ]]
}

@test "unmarked active planning is not attributed to an explicit spec" {
  rm -rf .planning/archive/spec-340
  printf '# Project: legacy planning\n' > .planning/PROJECT.md
  printf '%s\n' 'status: legacy-active-state' > .planning/STATE.md

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: none (active planning belongs to another spec; no archive found for spec 340)"* ]]
  [[ "$output" != *"ACTIVE-342-MARKER"* ]]
  [[ "$output" != *"ACTIVE-342-RUNNER-MARKER"* ]]
  [[ "$output" == *"no runner state for spec 340"* ]]
}

@test "missing planning tree reports a clean absence" {
  rm -rf .planning

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"no .planning/"* ]]
  [[ "$output" == *"no runner state for spec 340"* ]]
}

@test "suffixed archive directory resolves for the requested spec" {
  mv .planning/archive/spec-340 .planning/archive/spec-340-release

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"planning-source: archive:.planning/archive/spec-340-release"* ]]
  [[ "$output" == *"ARCHIVE-340-MARKER"* ]]
}

@test "missing and malformed spec identifiers fail closed" {
  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR"
  [ "$status" -eq 1 ]
  [[ "$output" == *"usage: collect-status-facts.sh <spec-id>"* ]]

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" abc
  [ "$status" -eq 1 ]
  [[ "$output" == *"spec id must begin with its numeric prefix"* ]]

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340abc
  [ "$status" -eq 1 ]
  [[ "$output" == *"spec id must begin with its numeric prefix"* ]]
}

@test "slug input is accepted and a missing spec directory stays read-only" {
  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 999-not-present

  [ "$status" -eq 0 ]
  [[ "$output" == *"no spec dir for 999-not-present"* ]]
  [[ "$output" == *"no archive found for spec 999"* ]]
}

@test "conflicting active identities fail closed" {
  printf '# Project: spec-342 active\n' > .planning/PROJECT.md
  printf '%s\n' '# STATE — spec-340' 'Run id: spec-340' > .planning/STATE.md

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 1 ]
  [[ "$output" == *"conflicting active planning identities: 340, 342"* ]]
}

@test "duplicate spec and archive directories fail closed" {
  mkdir -p specs/340-decoy/evidence

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 1 ]
  [[ "$output" == *"ambiguous spec directories for 340"* ]]

  rm -rf specs/340-decoy
  cp -R .planning/archive/spec-340 .planning/archive/spec-340-decoy

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 1 ]
  [[ "$output" == *"ambiguous planning archives for spec 340"* ]]
}

@test "spec history excludes commits not reachable from current HEAD" {
  git switch -q -c abandoned-proof
  printf '{"proof":"unmerged-only-marker"}\n' > specs/340-target/evidence/proof.json
  git add specs/340-target/evidence/proof.json
  git commit -q -m 'test: unmerged-only-marker'
  git switch -q main

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" != *"unmerged-only-marker"* ]]
  [[ "$output" == *"feat(spec-340): archive completed proof"* ]]
}

@test "archived runner state never probes a stale pid" {
  mkdir -p .planning/archive/spec-340/run-state
  printf '%s\n' 'ARCHIVED-RUNNER-MARKER' > .planning/archive/spec-340/run-state/gsd-run.status
  printf '%s\n' "$$" > .planning/archive/spec-340/run-state/gsd-run.pid

  run env -u GSD_RUN_ID -u GATES_STORE bash "$COLLECTOR" 340

  [ "$status" -eq 0 ]
  [[ "$output" == *"ARCHIVED-RUNNER-MARKER"* ]]
  [[ "$output" == *"pid-liveness: ARCHIVED-NOT-PROBED"* ]]
  [[ "$output" != *"pid-liveness: ALIVE"* ]]
}

@test "ledger run id must match the requested spec" {
  run env GSD_RUN_ID=spec-342 GATES_STORE="$BATS_TEST_TMPDIR/evidence.json" bash "$COLLECTOR" 340

  [ "$status" -eq 1 ]
  [[ "$output" == *"ledger run id mismatch: requested 340, got GSD_RUN_ID=spec-342"* ]]
}
