# Project: feature-fix-swarm — GSD adoption support tooling

## What This Is

feature-fix-swarm (FFS) is a spec-driven implementation harness. It is adopting
@opengsd/gsd-core@1.6.1 as its orchestration engine. This gsd project covers the
small support tooling the adoption needs.

## Core Value

Deterministic gates (`lib/gates.py`) remain the sole completion authority; gsd
orchestrates. The support scripts let FFS assert gsd-side state deterministically.

## Context

- Repo root = this worktree. Existing test suite: `python3 -m pytest lib/tests -q` (190 passing).
- New scripts live in `scripts/gsd/`. Tests for shell scripts live in `tests/` as bats files if bats exists, else as `scripts/gsd/*.test.sh` self-checks.
- bash 3.2-safe (macOS default): no mapfile, no `read -a`.
- Never `git add -A`; stage files by explicit path. Never push.

## Constraints

- Do not modify `lib/gates.py`, `lib/runtime_proof.py`, or any existing skill files.
- Scripts must be shellcheck-clean.
