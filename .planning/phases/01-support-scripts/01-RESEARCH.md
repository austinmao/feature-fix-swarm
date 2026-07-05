# Phase 1: Support scripts - Research

**Researched:** 2026-07-05
**Domain:** Bash CLI tooling (deterministic assertions) wrapping a Node.js CLI (`gsd-tools`) and a Markdown state file
**Confidence:** HIGH

## Summary

This phase builds two small, independent, read-only bash scripts under `scripts/gsd/`:
`consent-check.sh` (REQ-01, asserts a gsd capability is active) and `state-phase.sh`
(REQ-02, extracts a completed-phase integer from `.planning/STATE.md`). Neither script
touches `lib/gates.py` or any existing skill. Both must be shellcheck-clean and have
tests; the existing `python3 -m pytest lib/tests -q` (190 passed) must remain
unaffected because neither script is imported by or modifies any Python module.

The codebase already has three precedent scripts in `scripts/gsd/` (`gsd-run.sh`,
`gates-test-command.sh`, `review-gate-command.sh`) that establish the house style:
`#!/usr/bin/env bash` + `set -uo pipefail` (no `-e`), `REPO_ROOT="$(git rev-parse
--show-toplevel 2>/dev/null || pwd)"`, `command -v <tool>` existence checks before
invocation, and `usage: <script> <args>` + `exit 2` for argument errors. All three
pass `shellcheck -S warning` today with zero findings — that is the bar the new
scripts must clear. `bats` (1.13.0) is installed on the dev machine and pulled by CI
(`apt-get install -y bats`) with auto-discovery via `find . -name '*.bats'`, so per
`PROJECT.md`'s own instruction ("Tests... live in `tests/` as bats files if bats
exists") the tests should be `.bats` files, not `.test.sh` self-checks.

**Primary recommendation:** Invoke `node "$REPO_ROOT/node_modules/.bin/gsd-tools"
capability list` and parse the JSON with `node -e` (zero new dependency — node is
already required to run gsd-tools itself) rather than `jq` or grep/sed. For
`state-phase.sh`, do not call `gsd-tools state get` (it only resolves
`.planning/STATE.md` relative to `--cwd`, not an arbitrary file argument as REQ-02
requires) — instead grep the `## Current Position` body directly for the `Phase: X
of Y` line, which is the one BODY signal that mirrors what the frontmatter
`progress.*` counters are supposed to (but are documented-unreliably) compute.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-------------------|
| REQ-01 | `scripts/gsd/consent-check.sh <capability-id>` exits 0 iff `gsd-tools capability list` (via `node node_modules/.bin/gsd-tools`) reports the capability active/consented; exits 1 with actionable message otherwise; exits 2 on usage error; fail-closed on gsd-tools missing/erroring | Pattern 1 (fail-closed subprocess wrapper) + Pattern 2 (JSON-argv handoff to `node -e`) under Architecture Patterns; empirically verified `capability list` JSON shape and fail-closed exit codes under Sources (Primary) |
| REQ-02 | `scripts/gsd/state-phase.sh [state-file]` (default `.planning/STATE.md`) prints the completed-phases integer derived from the BODY (not frontmatter `percent`/`completed_phases`); exits 2 if file missing | Pattern 3 (BODY-only text extraction) under Architecture Patterns; STATE.md template structure verified via `templates/state.md` and `gsd-roadmapper.md`; ambiguity in "completed" semantics flagged in Open Questions #1 / Assumption A2 / Pitfall 2 |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Capability/consent assertion (REQ-01) | CLI / local tooling | Node CLI (`gsd-tools`) | `consent-check.sh` is a thin bash wrapper; the actual consent ledger lookup is owned by `gsd-tools capability list` (Node), not reimplemented in bash |
| STATE.md phase extraction (REQ-02) | CLI / local tooling | — | Pure file-read + text-parse; no service, no DB, no network — single-tier |
| Test verification | CI / local tooling | — | `bats` (shell) + existing `pytest` (Python) — both already wired into `.github/workflows/ci.yml` |

There is no browser, server, or database tier in this phase — it is exclusively local
CLI tooling that other GSD workflow phases (FFS preflight/gates, a later phase) will
shell out to.

## Standard Stack

### Core
| Tool | Version (verified) | Purpose | Why Standard |
|------|---------------------|---------|--------------|
| bash | `/usr/bin/env bash` (macOS default is 3.2; dev machine has 5.3.9) | Script runtime | Project constraint: "bash 3.2-safe (macOS default): no mapfile, no `read -a`" — the existing 3 scripts in `scripts/gsd/` already follow this |
| node | v25.9.0 `[VERIFIED: node --version]` | Runs `gsd-tools` and does the JSON parsing for `consent-check.sh` | Already a hard dependency — `gsd-tools` is a `.cjs` file; REQ-01 explicitly mandates invocation "via `node node_modules/.bin/gsd-tools`" |
| `@opengsd/gsd-core` | 1.6.1 (pinned in `package.json`) `[VERIFIED: package.json]` | Provides `gsd-tools capability list` | Already the project's pinned orchestration dependency — do not add a second copy or a wrapper package |
| bats-core | 1.13.0 (Homebrew, dev machine) `[VERIFIED: bats --version]`; installed in CI via `apt-get install -y bats` | Shell script tests | `PROJECT.md` mandates bats-if-available; CI already has a `bats` job that auto-discovers `*.bats` files anywhere in the repo — zero CI wiring needed |
| shellcheck | 0.11.0 (Homebrew, dev machine) `[VERIFIED: shellcheck --version]`; CI installs via `apt-get install -y shellcheck` | Lint gate | Existing `ci.yml` `shellcheck` job already runs `-S warning`; the 3 existing `scripts/gsd/*.sh` files pass with 0 findings today (`[VERIFIED: ran shellcheck -S warning scripts/gsd/*.sh scripts/harness/*.sh locally, rc=0]`) |
| python3 / pytest | 3.14.6 / 9.0.2 `[VERIFIED: python3 --version / pytest --version]` | Existing regression baseline | `lib/tests` (190 tests) must stay green — this phase must not touch `lib/gates.py`, `lib/runtime_proof.py`, or any Python module |

### Supporting
None. This phase adds no npm/pip/cargo dependencies.

### Alternatives Considered
| Instead of | Could use | Tradeoff |
|------------|-----------|----------|
| `node -e` inline JSON parsing | `jq` | `jq` is present locally (`/usr/bin/jq` 1.7.1) but is NOT installed by any CI job in `.github/workflows/ci.yml` (`pytest`, `shellcheck`, `bats` jobs — none run `apt-get install jq`); GitHub's `ubuntu-latest` image ships `jq` preinstalled `[ASSUMED — not verified against the actual runner image used by this repo's CI, only general GitHub-hosted-runner knowledge]`, but relying on `node` (already a hard, verified-present dependency for `gsd-tools` itself) adds zero new environment assumptions. Recommend `node`. |
| Bash-only regex/`grep -oP` JSON scraping | `node -e` | `grep -oP` is a GNU-grep-only flag (BSD/macOS default `grep` lacks `-P`), and the project explicitly targets macOS (bash 3.2 constraint) — a portable JSON scrape without PCRE support is fragile across multi-line pretty-printed JSON. `node -e` is exact and portable. |
| Hand-rolled regex on `## Current Position` for `state-phase.sh` | `gsd-tools state get Phase` | `gsd-tools state get` (in `capability-state`/`state-command-router.cjs`) resolves `.planning/STATE.md` relative to `--cwd`, not an arbitrary caller-supplied filename — REQ-02 explicitly requires `state-phase.sh [state-file]` to accept **any path** (default `.planning/STATE.md`), so `gsd-tools` cannot be reused directly for the general case. A tests-only fixture file cannot be pointed to this way. Direct grep/sed is required for REQ-02; do not hand-roll a full Markdown parser — a single anchored regex on the known `Phase: X of Y` line is sufficient. |

**Installation:** No new install step. All tools above are already present (`package.json` devDependency, Homebrew casks, or system Python/Node).

## Package Legitimacy Audit

Not applicable — this phase installs zero new packages (no `npm install`, `pip
install`, or `cargo add`). It only shells out to the already-pinned
`@opengsd/gsd-core@1.6.1` devDependency and system tools (bash, node, python3,
bats, shellcheck) that are already present in this repo/CI.

**Packages removed due to [SLOP] verdict:** none — no packages evaluated.
**Packages flagged as suspicious [SUS]:** none.

## Architecture Patterns

### System Architecture Diagram

```
REQ-01: consent-check.sh <capability-id>
  │
  ├─▶ resolve REPO_ROOT (git rev-parse --show-toplevel || pwd)
  │
  ├─▶ command -v node?  ──── missing ───▶ stderr "node not found" ──▶ exit 1 (fail-closed)
  │        │ present
  │        ▼
  ├─▶ node "$REPO_ROOT/node_modules/.bin/gsd-tools" capability list
  │        │
  │        ├── non-zero exit / unparsable stdout ──▶ stderr actionable msg ──▶ exit 1 (fail-closed)
  │        │ zero exit, JSON array
  │        ▼
  ├─▶ node -e '<parse JSON from argv/stdin, find entry by id, check status>'
  │        │
  │        ├── id found AND status === "active" ──▶ exit 0
  │        └── id absent OR status !== "active"  ──▶ stderr "capability '<id>' not"
  │                                                    " installed/consented — run"
  │                                                    " gsd capability install <id>"
  │                                                  ──▶ exit 1
  │
  └─▶ arg count != 1 ──▶ stderr "usage: consent-check.sh <capability-id>" ──▶ exit 2

REQ-02: state-phase.sh [state-file=.planning/STATE.md]
  │
  ├─▶ file missing ──▶ stderr "state-phase: <path> not found" ──▶ exit 2
  │        │ file present
  │        ▼
  ├─▶ strip/skip YAML frontmatter (--- ... ---) — NEVER read progress.* / completed_phases from it
  │
  ├─▶ grep BODY "## Current Position" section for the `Phase: X of Y` line
  │        │
  │        ├── not found ──▶ stderr actionable msg ──▶ exit 1 (cannot derive)
  │        │ found: X, Status line
  │        ▼
  ├─▶ completed = (Status contains "Phase complete") ? X : (X - 1)
  │
  └─▶ print completed (single integer) to stdout ──▶ exit 0
```

### Recommended Project Structure
```
scripts/gsd/
├── gates-test-command.sh        # existing — untouched
├── gsd-run.sh                   # existing — untouched
├── review-gate-command.sh       # existing — untouched
├── consent-check.sh             # NEW — REQ-01
└── state-phase.sh               # NEW — REQ-02

tests/
├── test_agent_catalog.py        # existing — untouched
├── consent-check.bats           # NEW — REQ-01 test
└── state-phase.bats             # NEW — REQ-02 test
```
No CI wiring is needed for the `bats` job (auto-discovers `*.bats` anywhere via
`find . -name '*.bats' -not -path './.git/*'`). The `shellcheck` job's glob
(`scripts/*.sh scripts/hooks/*.sh scripts/harness/*.sh`) does **not** include
`scripts/gsd/*.sh` — see Common Pitfalls.

### Pattern 1: Fail-closed subprocess wrapper (REQ-01)
**What:** Wrap a Node CLI call; any failure to invoke or parse it is treated as
"not consented," never as "consented."
**When to use:** Any assertion script whose "yes" answer gates a downstream
action (here: FFS preflight/gates deciding whether to proceed).
**Example (verified pattern from this repo, `scripts/gsd/gates-test-command.sh`):**
```bash
#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if ! command -v node >/dev/null 2>&1; then
  echo "consent-check: node not found on PATH — cannot verify capability, failing closed" >&2
  exit 1
fi

GSD_TOOLS="$REPO_ROOT/node_modules/.bin/gsd-tools"
if [ ! -e "$GSD_TOOLS" ]; then
  echo "consent-check: $GSD_TOOLS not found — cannot verify capability, failing closed" >&2
  exit 1
fi
```

### Pattern 2: JSON-argv handoff to `node -e` (avoids injection AND avoids jq)
**What:** Never string-interpolate a caller-controlled argument into a `node -e`
source string. Pass it as a real process argument (`process.argv`), and JSON output
via `process.stdin` — both safe from shell/JS injection.
**Example (new pattern for this phase, not from an external doc — derived from
"don't hand-roll JSON parsing when node is already required"):**
```bash
CAP_ID="$1"
LIST_JSON="$(node "$GSD_TOOLS" capability list 2>/dev/null)"
LIST_RC=$?
if [ "$LIST_RC" -ne 0 ] || [ -z "$LIST_JSON" ]; then
  echo "consent-check: gsd-tools capability list failed (rc=$LIST_RC) — failing closed" >&2
  exit 1
fi

printf '%s' "$LIST_JSON" | node -e '
  let raw = "";
  process.stdin.on("data", c => raw += c);
  process.stdin.on("end", () => {
    const capId = process.argv[1];
    let list;
    try { list = JSON.parse(raw); } catch { process.exit(1); }
    const entry = Array.isArray(list) ? list.find(e => e && e.id === capId) : null;
    process.exit(entry && entry.status === "active" ? 0 : 1);
  });
' "$CAP_ID"
CHECK_RC=$?
if [ "$CHECK_RC" -ne 0 ]; then
  echo "consent-check: capability '$CAP_ID' is not active/consented on this machine — run 'gsd capability install $CAP_ID' (or the appropriate gsd onboarding step) first" >&2
  exit 1
fi
exit 0
```
This mirrors the fail-closed contract literally: `list.find` returning `undefined`
(capability absent, e.g. `ffs-gates` today) and a present-but-non-`"active"` status
both fall through to the same `exit 1` branch.

### Pattern 3: BODY-only text extraction, bypassing known-unreliable frontmatter (REQ-02)
**What:** `.planning/STATE.md`'s YAML frontmatter carries `progress.completed_phases`
and `progress.percent`, but these are a **documented-unreliable** upstream defect
class at the pinned `@opengsd/gsd-core@1.6.1` (see below) — several closed issues
(#1514, #1446, #1264, #274) and two still-open as of this research (#2012, #2022)
all describe the frontmatter counters going stale or wrong. REQ-02 explicitly
forbids reading them. The BODY's `## Current Position` section is the
operator-facing text a human reads to know real status, so it is the fallback
source of truth.
**Example:**
```bash
STATE_FILE="${1:-.planning/STATE.md}"
if [ ! -f "$STATE_FILE" ]; then
  echo "state-phase: $STATE_FILE not found" >&2
  exit 2
fi

# Strip YAML frontmatter (--- ... ---) so a `percent:`/`completed_phases:` line in
# the frontmatter can never be mistaken for the BODY's Phase/Status lines.
BODY="$(awk '
  BEGIN{fm=0}
  NR==1 && $0=="---"{fm=1; next}
  fm==1 && $0=="---"{fm=2; next}
  fm!=1{print}
' "$STATE_FILE")"

PHASE_LINE="$(printf '%s\n' "$BODY" | grep -E '^Phase:[[:space:]]*[0-9]+[[:space:]]+of[[:space:]]+[0-9]+' | head -1)"
if [ -z "$PHASE_LINE" ]; then
  echo "state-phase: no 'Phase: X of Y' line found in $STATE_FILE body" >&2
  exit 1
fi
CURRENT="$(printf '%s\n' "$PHASE_LINE" | sed -E 's/^Phase:[[:space:]]*([0-9]+).*/\1/')"

STATUS_LINE="$(printf '%s\n' "$BODY" | grep -E '^Status:' | head -1)"
if printf '%s' "$STATUS_LINE" | grep -qi 'phase complete'; then
  COMPLETED="$CURRENT"
else
  COMPLETED=$((CURRENT - 1))
  [ "$COMPLETED" -lt 0 ] && COMPLETED=0
fi

printf '%s\n' "$COMPLETED"
exit 0
```

### Anti-Patterns to Avoid
- **Reading `progress.completed_phases` / `progress.percent` from frontmatter:**
  REQ-02 explicitly forbids this — it is the exact defect class (#1514/#1446/#1264/
  #274/#2012/#2022) this script exists to route around.
- **String-interpolating the capability-id or file path into a `node -e "..."` or
  `eval` source string:** injection risk; pass via `process.argv`/stdin instead.
- **Calling `gsd-tools state get`/`state get Phase`** for REQ-02: it only resolves
  `.planning/STATE.md` relative to `--cwd`, not an arbitrary filename argument, so
  it cannot satisfy `state-phase.sh [state-file]`'s "any path" contract.
- **`mapfile` / `read -a` / associative arrays:** breaks bash 3.2 (macOS system
  bash) per `PROJECT.md`'s explicit constraint.
- **`grep -P` (PCRE):** not available in BSD/macOS default `grep`; use `-E`
  (extended POSIX) as in the examples above, which is portable.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Capability/consent ledger lookup | A bash re-implementation of gsd-core's capability trust/ledger logic | `node node_modules/.bin/gsd-tools capability list` (already pinned dependency, already does the real lookup) | REQ-01 mandates this exact invocation; gsd-core's capability system (ledger + lock + consent store) is non-trivial (`capability-ledger.cjs`, `capability-lock.cjs`, `capability-consent.cjs`) — reimplementing any sliver of it in bash would silently drift from the real consent state |
| JSON parsing in bash | Regex/`grep -oP` scraping of pretty-printed JSON, or adding a `jq` dependency | `node -e` (node is already a hard dependency for `gsd-tools` itself) | Zero new dependency, correct on edge cases (nested braces, multi-line values), portable across macOS/Linux |
| STATE.md progress computation | A rewrite of gsd-core's `state.cjs` progress-rebuild logic in bash | A single anchored `Phase: X of Y` regex on the BODY | The full progress-rebuild logic (`cmdStateUpdateProgress`, disk-scan of `phases/`, `roadmap-parser.cjs`) is out of scope, requires scanning phase directories, and is exactly the kind of complexity REQ-02 exists to avoid touching — the BODY line is sufficient for a single integer |

**Key insight:** Both scripts are thin, fail-closed wrappers over machinery that
already exists (gsd-core's CLI, or a human-readable status line gsd-core itself
already writes) — the correct amount of new code is a few lines of bash glue, not a
reimplementation of gsd-core internals.

## Common Pitfalls

### Pitfall 1: CI's shellcheck job silently never lints the new scripts
**What goes wrong:** `.github/workflows/ci.yml`'s `shellcheck` job runs
`shellcheck -S warning setup.sh hooks/*.sh scripts/*.sh scripts/hooks/*.sh
scripts/harness/*.sh` — this glob list does **not** include `scripts/gsd/*.sh`.
A developer can introduce a shellcheck warning in `consent-check.sh` or
`state-phase.sh` and CI will stay green.
**Why it happens:** The glob was written before `scripts/gsd/` existed as a
lint target (the 3 existing files there were apparently added without updating
this line, though they happen to already be clean).
**How to avoid:** Run `shellcheck -S warning scripts/gsd/*.sh` manually/locally
per the phase's own success criterion #3 ("Both scripts shellcheck-clean"), and
consider a low-risk one-line addition to `ci.yml`'s glob
(`scripts/gsd/*.sh`) so this doesn't silently regress later — this is not
forbidden by `REQUIREMENTS.md`'s "Out of Scope" list (which only excludes
`lib/gates.py` and existing skills), but is optional/planner's discretion since
it's not one of REQ-01/REQ-02's literal success criteria.
**Warning signs:** `shellcheck scripts/gsd/*.sh` passes locally but was never
actually run in CI for the new files.

### Pitfall 2: Off-by-one on "completed phases" vs. "current phase"
**What goes wrong:** The BODY's `Phase: X of Y (...)` line encodes the **current**
phase, not a count of **completed** phases. A naive `print X` implementation
would report phase 1 as "1 completed" while phase 1 is still in progress.
**Why it happens:** REQ-02's wording ("completed-phases value derived from the
STATE.md BODY checklist/progress section") is somewhat underspecified — there is
no literal per-phase checklist in the actual STATE.md template (verified against
`node_modules/@opengsd/gsd-core/gsd-core/templates/state.md` and
`.claude/agents/gsd-roadmapper.md`'s "STATE.md Structure" section — both list
only "Current Position (phase, plan, status, progress bar)", not a checklist).
**How to avoid:** Use `Status: Phase complete...` as the tie-breaker: if status
says the current phase is complete, count it; otherwise subtract one. Encode
this exact rule in the plan's task description and pin it with test fixtures
(a "mid-phase" fixture and a "phase complete" fixture) so the interpretation is
locked by a passing test rather than left ambiguous.
**Warning signs:** A test asserting `state-phase.sh` returns `X` (not `X-1`) for
a STATE.md whose Status line does not say "Phase complete" would silently
encode the wrong interpretation — write the fixture/assertion pair deliberately.

### Pitfall 3: Command injection via `node -e` string interpolation
**What goes wrong:** Building the `node -e` source string with
`"... === '$CAP_ID' ..."` lets a capability-id like `x'; require('child_process')...`
(unlikely from a human caller, but this script may later be invoked
programmatically by FFS's own automation) break out of the intended JS
expression.
**Why it happens:** String interpolation into an executable source is a classic
injection vector, easy to reach for when writing "quick" one-liners.
**How to avoid:** Pass the capability-id as `process.argv[N]`, not as
interpolated source text (see Pattern 2's code example).
**Warning signs:** Any `node -e "...$VAR..."` in the diff should be a red flag
in code review.

### Pitfall 4: `.planning/STATE.md` does not exist yet in this repo
**What goes wrong:** This GSD project (`feature-fix-swarm` adoption tooling) has
`ROADMAP.md`/`REQUIREMENTS.md`/`PROJECT.md`/`config.json` but **no `STATE.md`
yet** (`[VERIFIED: ls .planning — STATE.md absent as of this research]`). Success
criterion #2 (`bash scripts/gsd/state-phase.sh .planning/STATE.md` prints an
integer and exits 0) implicitly assumes the file exists by execution time.
**Why it happens:** Standard gsd-core lifecycle creates `STATE.md` right after
`ROADMAP.md` (per `gsd-roadmapper.md`), but this project may have skipped that
step, or it will be created automatically when this phase's plan/execute
workflow runs.
**How to avoid:** Do not assume its presence at research/plan time. The
implementation task should verify at execution time whether `.planning/STATE.md`
exists; if the execute-phase workflow itself creates it before running success
criteria, no action is needed. If it genuinely doesn't exist when success
criterion #2 is checked, that is a phase-execution-order issue outside this
phase's scope (REQ-02 only specifies script behavior, not STATE.md's existence).
**Warning signs:** Running the literal success-criterion command right now
(`bash scripts/gsd/state-phase.sh .planning/STATE.md`) returns exit 2 ("file
missing") until either `STATE.md` is created by the wider gsd workflow or a
test fixture is used instead for the actual TDD tests.

## Code Examples

See the three patterns under **Architecture Patterns** above — those are the
verified, ready-to-adapt implementations for REQ-01 and REQ-02 respectively,
following this repo's own existing `scripts/gsd/*.sh` conventions (confirmed by
reading `gates-test-command.sh`, `gsd-run.sh`, `review-gate-command.sh`).

### Existing house style reference (verified, `scripts/gsd/gsd-run.sh`)
```bash
#!/usr/bin/env bash
set -uo pipefail
if [ $# -lt 1 ]; then
  echo "usage: gsd-run.sh <slash-command> [args...]" >&2
  exit 2
fi
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```
Both new scripts should follow this exact usage/exit-2 idiom for argument errors.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|---------------|--------|
| Reading `progress.completed_phases`/`progress.percent` frontmatter directly | Reading BODY `Current Position` text instead | This phase (REQ-02) | Frontmatter counters are a known-unreliable, actively-being-reworked upstream area (`ADR-1769 STATE.md Transition Module`, still landing across `1.7.0-rc.2`); our pin is `1.6.1`, pre-fix |
| N/A (no existing `ffs-gates` capability) | `consent-check.sh` correctly reports "not consented" for `ffs-gates` today | Confirmed empirically this session (`gsd-tools capability list` returns 32 entries, none named `ffs-gates`) | Matches phase success criterion #1 exactly: the script's exit-1 behavior for `ffs-gates` is *expected*, not a bug to fix — `ffs-gates` as an installable capability is deliberately deferred (per `spike-results/gsd-ruflo/research_gsd-core_20260705.md`'s reconciliation note: "BLOCKED at 1.6.1 per maintainer on #2004") |

**Deprecated/outdated:** None specific to this narrow phase; the broader
gsd-core 1.6.1→1.7.0 migration risk area is documented in
`spike-results/gsd-ruflo/research_gsd-core_20260705.md` (out of scope for this
phase, but useful background for the planner).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | GitHub's `ubuntu-latest` runner image ships `jq` preinstalled | Alternatives Considered | Low — this repo's CI does not currently depend on `jq` anywhere, and the recommendation is to use `node` instead, so this assumption does not gate any actual implementation choice |
| A2 | "Completed phases" = current phase minus one, unless Status says "Phase complete" (then = current phase) | Common Pitfalls / Pattern 3 | Medium — REQ-02's wording is ambiguous ("BODY checklist/progress section" has no literal checklist in the actual template); if the intended semantics differ, the planner/user should confirm before locking test fixtures, since this interpretation is currently the researcher's best inference, not a value pinned by an existing example or spec |
| A3 | It is acceptable (planner's discretion, not required) to add `scripts/gsd/*.sh` to `ci.yml`'s shellcheck glob | Common Pitfalls Pitfall 1 | Low — purely additive CI hygiene suggestion, not required by REQUIREMENTS.md or ROADMAP.md success criteria |

## Open Questions (RESOLVED)

1. **Exact semantics of "completed-phases value" for REQ-02**
   - RESOLVED: locked via 01-02-PLAN.md Task 1 paired fixtures (mid-phase → `X-1`, phase-complete → `X`), per Assumption A2 adopted in the plan's must_haves.
   - What we know: the frontmatter `progress.completed_phases`/`percent` counters
     are explicitly forbidden (documented-unreliable per this repo's own
     upstream research); the only phase-progress signal in STATE.md's BODY is
     the `## Current Position` section's `Phase: X of Y` and `Status` lines.
   - What's unclear: whether "completed" should be `X-1` (mid-phase) / `X`
     (phase complete) as recommended here, or some other definition (e.g.
     always `X-1` regardless of status, treating "phase complete" as
     synonymous with "about to advance").
   - Recommendation: lock this via two concrete Wave-0 test fixtures (one
     `Status: In progress`, one `Status: Phase complete — ready for
     verification`) with explicit expected integers in the plan itself, so the
     plan-checker and executor cannot each independently guess.

2. **Does `.planning/STATE.md` exist by the time success criterion #2 runs?**
   - RESOLVED: handled in 01-02-PLAN.md `<verification>` — hermetic fixture STATE.md proves the integer path; the literal `.planning/STATE.md` invocation is a deferred end-of-phase re-run once the file exists (option b below).
   - What we know: it does not exist in this repo as of this research pass.
   - What's unclear: whether the phase's own execute-phase workflow will
     create it first (standard gsd lifecycle), making the literal
     success-criterion command work naturally, or whether it needs to be
     created/scaffolded as part of this phase's task list.
   - Recommendation: the plan should either (a) note this is expected to
     resolve itself via the surrounding gsd workflow before verification runs,
     or (b) use a checked-in test fixture STATE.md for the bats tests
     (recommended regardless, for hermetic/fast tests) and treat the literal
     `.planning/STATE.md` invocation in success criterion #2 as an
     end-of-phase manual/CI check rather than a unit test dependency.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|--------------|-----------|---------|----------|
| bash | Both scripts | ✓ | 5.3.9 (dev); target macOS default is 3.2-safe per `PROJECT.md` | Write bash-3.2-safe code (no `mapfile`, no `read -a`) |
| node | REQ-01 (`gsd-tools` invocation + JSON parsing) | ✓ | v25.9.0 | None needed — required by `gsd-tools` itself already |
| `@opengsd/gsd-core` (`node_modules/.bin/gsd-tools`) | REQ-01 | ✓ | 1.6.1 (pinned, `node_modules` installed) | If missing, `consent-check.sh` must fail closed (exit 1) per REQ-01's explicit spec |
| bats-core | Tests for both scripts | ✓ | 1.13.0 (dev, Homebrew); CI installs via `apt-get install -y bats` | `scripts/gsd/*.test.sh` self-checks only if bats were unavailable — it is available |
| shellcheck | Success criterion #3 | ✓ | 0.11.0 (dev, Homebrew); CI installs via `apt-get install -y shellcheck` | None needed |
| python3 / pytest | Regression baseline (must stay 190 passed) | ✓ | 3.14.6 / 9.0.2 | None needed |
| jq | Not used (recommendation: use `node -e` instead) | ✓ locally (1.7.1) but not installed by any CI job | 1.7.1 (dev only) | N/A — recommended approach avoids this dependency entirely |

**Missing dependencies with no fallback:** none — every dependency this phase
needs is already present in both the dev environment and CI.

**Missing dependencies with fallback:** none.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework (shell) | bats-core 1.13.0 (dev + CI) |
| Framework (existing Python baseline) | pytest 9.0.2 |
| Config file | none — no `pytest.ini`/`pyproject.toml`/`conftest.py` exists; `bats` needs no config |
| Quick run command (this phase, shell) | `bats tests/consent-check.bats tests/state-phase.bats` |
| Quick run command (this phase, lint) | `shellcheck -S warning scripts/gsd/consent-check.sh scripts/gsd/state-phase.sh` |
| Full suite command (regression gate) | `python3 -m pytest lib/tests -q` (must remain "190 passed") |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|---------------------|--------------|
| REQ-01 | exits 0 iff capability is active | unit (bats) | `bats tests/consent-check.bats` | ❌ Wave 0 |
| REQ-01 | exits 1 with actionable message when capability absent/inactive/gsd-tools missing or erroring | unit (bats) | `bats tests/consent-check.bats` | ❌ Wave 0 |
| REQ-01 | exits 2 on usage error (wrong arg count) | unit (bats) | `bats tests/consent-check.bats` | ❌ Wave 0 |
| REQ-01 | `bash scripts/gsd/consent-check.sh ffs-gates` exits 1 today | smoke (manual/CI) | `bash scripts/gsd/consent-check.sh ffs-gates; echo $?` | ❌ Wave 0 (script doesn't exist yet) |
| REQ-02 | prints a single integer, exits 0, for a well-formed STATE.md fixture | unit (bats) | `bats tests/state-phase.bats` | ❌ Wave 0 |
| REQ-02 | exits 2 when file missing | unit (bats) | `bats tests/state-phase.bats` | ❌ Wave 0 |
| REQ-02 | never reads frontmatter `percent`/`completed_phases` (fixture with wrong frontmatter, correct body — must derive from body) | unit (bats) | `bats tests/state-phase.bats` | ❌ Wave 0 |
| Both | `python3 -m pytest lib/tests -q` still 190 passed (regression) | integration/regression | `python3 -m pytest lib/tests -q` | ✓ (already exists, currently green) |

### Sampling Rate
- **Per task commit:** `shellcheck -S warning scripts/gsd/<file>.sh && bats tests/<file>.bats`
- **Per wave merge:** `python3 -m pytest lib/tests -q` (must stay 190 passed) + full `bats tests/*.bats`
- **Phase gate:** all of the above green, plus the literal success-criterion
  commands from `ROADMAP.md` run once manually/in CI before `/gsd-verify-work`.

### Wave 0 Gaps
- [ ] `tests/consent-check.bats` — covers REQ-01 (needs a stubbed
  `node_modules/.bin/gsd-tools`-shaped fake so tests are hermetic; recommend
  making the script's `REPO_ROOT` resolution overridable via an env var, e.g.
  `CONSENT_CHECK_REPO_ROOT`, mirroring the `HOST_EXECUTOR_ROOT` override
  pattern already used in `scripts/harness/ruflo-host-executor.bats`)
- [ ] `tests/state-phase.bats` — covers REQ-02 (no stubbing needed; just write
  fixture `STATE.md` files to `$BATS_TEST_TMPDIR` and pass them as the
  argument)
- [ ] Framework install: none — bats and shellcheck are already present in
  dev and CI.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|----------------|---------|-------------------|
| V2 Authentication | No | N/A — no auth surface |
| V3 Session Management | No | N/A |
| V4 Access Control | No | N/A — local CLI, no multi-user access boundary |
| V5 Input Validation | Yes | Quote all variable expansions; pass caller-supplied values (`$1` capability-id, `$1` state-file path) as real process arguments to `node`, never string-interpolated into an `eval`'d or `node -e`'d source string |
| V6 Cryptography | No | N/A — no secrets, no crypto operations |

### Known Threat Patterns for bash-wrapping-a-CLI

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|----------------------|
| Command/code injection via string-interpolated `node -e "...$VAR..."` | Tampering | Pass values via `process.argv`, never interpolate into source text (Pattern 2 above) |
| Word-splitting / glob expansion on unquoted `$1`/`$STATE_FILE` | Tampering | Always double-quote variable expansions (`"$STATE_FILE"`, not `$STATE_FILE`) |
| Silent "fail open" (treating a `gsd-tools` crash or missing binary as "consented") | Elevation of Privilege | REQ-01 explicitly mandates fail-closed: any non-zero exit, missing `node`, or missing `node_modules/.bin/gsd-tools` must resolve to exit 1, never exit 0 |

## Sources

### Primary (HIGH confidence)
- Direct execution in this repo/session: `node node_modules/.bin/gsd-tools capability list` (returned 32 capabilities, all `status: "active"`, none named `ffs-gates`) `[VERIFIED: ran command this session]`
- Direct execution: `node node_modules/.bin/gsd-tools capability bogus` → `Error: Unknown capability subcommand...` exit 1; nonexistent module path → Node `MODULE_NOT_FOUND` exit 1 `[VERIFIED: ran command this session]`
- Direct execution: `shellcheck -S warning scripts/gsd/*.sh scripts/harness/*.sh` → 0 findings `[VERIFIED: ran command this session]`
- Direct execution: `python3 -m pytest lib/tests -q` → "190 passed" `[VERIFIED: ran command this session]`
- File read: `node_modules/@opengsd/gsd-core/gsd-core/templates/state.md` (canonical STATE.md template/structure) `[VERIFIED: read file this session]`
- File read: `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/config.json` `[VERIFIED: read file this session]`
- File read: `scripts/gsd/gates-test-command.sh`, `scripts/gsd/gsd-run.sh`, `scripts/gsd/review-gate-command.sh`, `.github/workflows/ci.yml`, `scripts/harness/ruflo-host-executor.bats` (existing house style + CI wiring + bats stubbing convention) `[VERIFIED: read file this session]`
- File read: `spike-results/gsd-ruflo/research_gsd-core_20260705.md` (this repo's own prior deep-research on gsd-core 1.6.1 STATE.md/capability defect classes) `[VERIFIED: read file this session]`
- File read: `.claude/agents/gsd-roadmapper.md` ("STATE.md Structure" section confirms no literal per-phase checklist in the template) `[VERIFIED: read file this session]`

### Secondary (MEDIUM confidence)
None used — no external web search was needed for this phase; it is entirely
internal-repo tooling with an already-pinned, already-installed dependency.

### Tertiary (LOW confidence)
- GitHub `ubuntu-latest` runner image ships `jq` preinstalled — general training
  knowledge, not verified against this repo's actual CI run `[ASSUMED]` (see
  Assumptions Log A1; does not gate any recommendation since `node` is used
  instead).

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every tool/version was verified by direct command execution this session, no external doc lookups needed
- Architecture: HIGH — patterns are copied from this repo's own existing `scripts/gsd/*.sh` files, verified to pass shellcheck today
- Pitfalls: MEDIUM-HIGH — the "off-by-one completed-phases" pitfall (Pitfall 2 / Assumption A2) is the one genuinely underspecified area in the requirements and should be confirmed via locked test fixtures rather than left to independent interpretation during planning/execution

**Research date:** 2026-07-05
**Valid until:** 30 days (stable, internal-repo tooling; the one external-drift risk is the pinned `@opengsd/gsd-core@1.6.1`→1.7.0 upgrade path, tracked separately in `spike-results/gsd-ruflo/research_gsd-core_20260705.md`, not this phase)
