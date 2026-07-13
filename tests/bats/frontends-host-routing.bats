#!/usr/bin/env bats

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "/fix keeps GSD debug on the invoking host" {
  grep -F 'Claude `/gsd-debug`,' skills/fix/SKILL.md
  grep -F 'Codex `$gsd-debug`' skills/fix/SKILL.md
  grep -F 'scripts/gsd/gsd-run.sh /gsd-debug' skills/fix/SKILL.md
}

@test "/task-swarm documents native resume and learning commands" {
  grep -F 'Claude `/gsd-resume-work`, Codex `$gsd-resume-work`' skills/task-swarm/SKILL.md
  grep -F '`/gsd-extract-learnings` or Codex `$gsd-extract-learnings`' skills/task-swarm/SKILL.md
}

@test "/task-swarm delegates review to the opposite host" {
  grep -F 'opposite-host plan gate' skills/task-swarm/SKILL.md
  grep -F 'only review gates' skills/task-swarm/SKILL.md
}

@test "/plan-decompose uses the shared opposite-host adapter" {
  grep -F '. scripts/gsd/adversary-host.sh' skills/plan-decompose/SKILL.md
  grep -F 'adversary_invoke "$REVIEW_KIND" 540' skills/plan-decompose/SKILL.md
  ! grep -F 'timeout 540 codex exec' skills/plan-decompose/SKILL.md
}

@test "/task-swarm delegates bounded gate repair instead of stopping early" {
  grep -F '`plan-decompose` owns all plan/task gate remediation' skills/task-swarm/SKILL.md
  grep -F 'PLAN-DECOMPOSE-BLOCKED' skills/task-swarm/SKILL.md
  ! grep -F 'STOP on any gate failure' skills/task-swarm/SKILL.md
}

@test "/plan-decompose owns bounded plan and task repair budgets" {
  grep -F 'PLAN_GATE_MAX_REPAIRS=${PLAN_GATE_MAX_REPAIRS:-2}' skills/plan-decompose/SKILL.md
  grep -F 'TASK_GATE_MAX_REPAIRS=${TASK_GATE_MAX_REPAIRS:-2}' skills/plan-decompose/SKILL.md
  grep -F '$SPEC_DIR/plan-decompose-blocked.md' skills/plan-decompose/SKILL.md
  grep -F 'generic “mandatory plan gate” message' skills/plan-decompose/SKILL.md
}
