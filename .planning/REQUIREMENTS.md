# Requirements: GSD adoption support tooling

## v1 Requirements

### Consent assertion

- [ ] **REQ-01**: `scripts/gsd/consent-check.sh <capability-id>` exits 0 iff `gsd-tools capability list` (run via `node node_modules/.bin/gsd-tools`) reports the capability as active/consented on this machine; exits 1 with an actionable message otherwise; exits 2 on usage error. Fail-closed: if gsd-tools is missing or errors, exit 1.

### STATE.md phase extraction

- [ ] **REQ-02**: `scripts/gsd/state-phase.sh [state-file]` (default `.planning/STATE.md`) prints the completed-phases value derived from the STATE.md BODY checklist/progress section (NOT the frontmatter `percent`/`completed_phases` counters, which are known-unreliable) as a single integer on stdout; exits 2 if the file is missing.

## Out of Scope

- Anything touching lib/gates.py or existing skills.
- Capability installation itself (only the assertion).
