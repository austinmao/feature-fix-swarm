# Phase 1: Levers - Research

**Researched:** 2026-07-10
**Domain:** Bash/Python CLI levers for an agent-orchestration skill suite (FFS) — hook JSON contracts, evidence-store extension, shell-script conventions
**Confidence:** HIGH (codebase-grounded; one external claim on Claude Code hook semantics is MEDIUM/CITED)

## Summary

Phase 1 builds six independent, RED-first levers with no cross-dependencies:
`scripts/hooks/delegation-enforcer.sh`, `scripts/gsd/security-surface.sh`
(extraction) + `scripts/gsd/review-tier.sh`, `scripts/gsd/liveness-check.sh`,
`scripts/gsd/learnings-harvest.sh`, `scripts/harness-audit.py`, and a
`findings-queue` subcommand family added to `lib/gates.py`. None of these
touch `gates.py verify_done`/`run_gate` exit semantics (explicit out-of-scope
guard). All six already have an established sibling pattern somewhere in this
repo — this research's job is to point the planner at the exact file/line to
copy the convention from, not to invent new conventions.

Two load-bearing discrepancies between `plan.md`'s architecture notes and the
actual repo were found and must be corrected before planning:

1. **`scripts/hooks/` already exists** (it is not "new dir" as plan.md
   states) — it holds `gsd-phase-evidence-gate.sh`, which is the closest
   working example of a PreToolUse hook that reads JSON on stdin via
   `python3 - "$INPUT" <<'PY'` and is registered in `.claude/settings.json`.
2. **`scripts/gsd/run_state/` does not exist.** The repo's actual `run_state`
   convention lives at `lib/run_state/` and is a SQLite-backed CLI
   (`~/.claude/state/runs.db`) for `/feature`/`/fix` native-`/goal` tracking —
   it has no `pid` column and is unrelated to worktree-liveness detection.
   `liveness-check.sh`'s "pid alive" and "mtime fresh" signals have **no
   existing data source to consume**; Phase 1 must invent the pidfile/state-dir
   input contract from scratch (see Open Questions).

**Primary recommendation:** For each lever, follow the copy pattern named in
its Architecture Patterns section below verbatim — this phase is convention-
reuse, not novel design, except for `liveness-check.sh`'s input contract
(genuinely new) and `review-tier.sh`'s "security-surface path" semantics
(needs one design call — see Open Questions).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Agent/Task spawn model injection | Claude Code hook (PreToolUse, host-side) | `.planning/config.json` (data) | Hooks are the only interception point before a tool call executes; must stay fail-open per Risk 1 in plan.md |
| Diff risk tiering | Standalone bash lever (`scripts/gsd/review-tier.sh`) | review-gate SKILL.md (Phase 2 consumer) | Logic belongs in a testable script, not skill prose (plan.md explicit) |
| Security-surface pattern list | Shared sourceable lib (`scripts/gsd/security-surface.sh`) | `security-model-fence.sh` + `review-tier.sh` (consumers) | "one home" requirement (AC-004) — single source of truth for a regex |
| Findings persistence | `lib/gates.py` evidence store (existing JSON file) | — | "a second store would violate single-authority" (plan.md, explicit) |
| Harness health scoring | Standalone Python script (`scripts/harness-audit.py`) | preflight skill (Phase 2 consumer) | Scoring + JSON output mirrors gates.py's own rationale; advisory only |
| Learnings persistence | `scripts/gsd/learnings-harvest.sh` → gbrain (external) or local JSONL archive | finish-tail skills (Phase 2 consumer) | fail-soft, exit 0 always; must never block ship |
| Autonomous-run liveness | `scripts/gsd/liveness-check.sh` | `lib/gates.py check-grant` (ship-grant signal reuse) | Composite signal script consumed later by adopt-wip prose (Phase 2) |

## Standard Stack

No new external dependencies. Everything in this phase is bash (POSIX/bash-3.2-safe) + Python 3 stdlib, matching the entire existing `scripts/gsd/` and `lib/` tree.

### Core
| Tool | Version (verified in this env) | Purpose | Why Standard |
|------|------|---------|--------------|
| bash | target: 3.2-safe (dev env has 5.3.9) | all `scripts/gsd/*.sh`, `scripts/hooks/*.sh` levers | every existing lever avoids `declare -A`/bash4+ isms (`[VERIFIED: repo grep — zero hits for declare -A across scripts/gsd, scripts/hooks]`); macOS ships bash 3.2 by default |
| python3 | 3.14.6 (dev env); stdlib only | `lib/gates.py` findings-queue, `scripts/harness-audit.py` | `lib/gates.py` already uses only `argparse, fcntl, hashlib, json, os, re, subprocess, sys, tempfile, pathlib` — no third-party imports `[VERIFIED: lib/gates.py imports, lines 53-62]` |
| bats-core | 1.x (`/opt/homebrew/bin/bats` present) | `tests/bats/*.bats` | existing convention for every `.sh` lever `[VERIFIED: tests/bats/ directory contents]` |
| shellcheck | 0.11.0 (present) | Phase 1 gate command | Phase 1 success criterion 3 runs it directly `[VERIFIED: shellcheck --version]` |
| pytest | present, no config file (auto-discovery) | Phase 1 gate command | `python3 -m pytest -q` currently reports "231 passed" at HEAD `[VERIFIED: ran python3 -m pytest -q in repo, output "231 passed"]` — matches plan.md's stated baseline exactly |

### Supporting
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `jq` (1.7.1 present) | JSON assertions inside `.bats` files | Existing bats files use `jq` for stdout JSON checks — spec.md's own stub example does (`jq -e '.tool_input.model == "sonnet"'`) |
| `gbrain` CLI (present in this dev env, NOT guaranteed on CI/consumer machines) | `learnings-harvest.sh` backend probe | Must be probed with `command -v gbrain` and treated as absent-by-default; invoke via `env -u DATABASE_URL gbrain doctor` per this repo's memory-routing discipline (prevents a shell-inherited `DATABASE_URL` from redirecting pglite) |

### Alternatives Considered
None — this phase is explicitly "PORT: build-fresh FFS-native implementations" per `prior-art.md`; no library adoption was considered or is appropriate (single-file bash/python levers).

**Installation:** none required — no new packages.

## Package Legitimacy Audit

**N/A for this phase.** No new external packages (npm/pip/cargo) are installed by any of the six levers — all are bash + Python stdlib, consistent with every existing script in `scripts/gsd/` and `lib/`. `gbrain` and `jq`/`shellcheck`/`bats` are pre-existing host tools consumed via `command -v`, not phase-installed dependencies.

## Architecture Patterns

### System Architecture Diagram

```
                    ┌─────────────────────────────┐
                    │  Claude Code orchestrator    │
                    │  (main-loop agent)           │
                    └──────────┬───────────────────┘
                               │ Agent/Task tool_use (PreToolUse)
                               ▼
          ┌────────────────────────────────────────┐
          │ scripts/hooks/delegation-enforcer.sh    │  reads stdin JSON,
          │  - subagent_type -> model_overrides[]   │  emits (possibly
          │  - .planning/config.json lookup         │  modified) JSON on
          │  - DELEGATION_ENFORCER=off passthrough  │  stdout
          └────────────────────────────────────────┘
                               │ spawn proceeds (pinned or original)
                               ▼
                    ┌─────────────────────────────┐
                    │  Sub-agent executes task     │
                    └──────────┬───────────────────┘
                               │ (elsewhere in the same run)
        ┌──────────────────────┼──────────────────────────┐
        ▼                      ▼                           ▼
┌───────────────┐   ┌───────────────────┐        ┌──────────────────┐
│ review-gate    │   │ finish-tail        │        │ adopt-wip /       │
│ (Phase 2       │   │ (Phase 2 consumer) │        │ overnight monitor │
│ consumer) calls│   │ calls              │        │ (Phase 2 consumer)│
│ review-tier.sh │   │ learnings-harvest  │        │ calls             │
│  ├─ sources    │   │  .sh               │        │ liveness-check.sh │
│  │ security-   │   │  ├─ globs          │        │  ├─ pidfile check │
│  │ surface.sh  │   │  │ .planning/**/   │        │  ├─ state-dir     │
│  │ (shared     │   │  │ learnings*.jsonl│        │  │ mtime check    │
│  │ w/ fence)   │   │  ├─ gbrain probe   │        │  └─ gates.py      │
│  └─ diff stats │   │  └─ archive        │        │     check-grant   │
│               │   │  fallback           │        │     "ship:gsd"    │
└───────┬────────┘   └────────────────────┘        └──────────────────┘
        │ findings recorded
        ▼
┌────────────────────────────────────────┐
│ lib/gates.py findings-queue (add/list/  │  reuses _load_store /
│ resolve) — writes under "findings" key  │  _save_store / _StoreLock
│ in $GATES_STORE (.feature-fix-swarm/    │  (existing evidence-store
│ evidence.json)                          │  machinery, NOT reinvented)
└────────────────────────────────────────┘

┌────────────────────────────────────────┐
│ scripts/harness-audit.py (standalone,   │  compares skills/<n>/SKILL.md
│ no producer/consumer link in Phase 1)   │  (repo source, "version:"
│  scores 0-100: dangling links,          │  frontmatter) vs installed
│  vendored-copy drift, dead model pins,  │  ~/.claude/skills/<n>/SKILL.md
│  hook-registration drift; --json        │  copy (setup.sh's cp target)
└────────────────────────────────────────┘
```

### Recommended Project Structure

No new directories — every new file lands in an existing, populated location:

```
scripts/
├── gsd/
│   ├── security-model-fence.sh   # existing — gets ONE line changed (source security-surface.sh)
│   ├── security-surface.sh       # NEW — sourceable, exports KEYWORDS-equivalent
│   ├── review-tier.sh            # NEW
│   ├── liveness-check.sh         # NEW
│   └── learnings-harvest.sh      # NEW
├── hooks/
│   ├── gsd-phase-evidence-gate.sh  # existing — copy its stdin/python3-heredoc pattern
│   └── delegation-enforcer.sh      # NEW
└── harness-audit.py                # NEW (repo root per plan.md, sibling of gates.py's rationale)
lib/
├── gates.py                        # EXTEND — add findings_add/findings_list/findings_resolve + CLI dispatch arm
└── tests/
    └── test_findings_queue.py      # NEW
tests/
├── bats/
│   ├── delegation-enforcer.bats    # NEW
│   ├── review-tier.bats            # NEW
│   ├── liveness-check.bats         # NEW
│   ├── learnings-harvest.bats      # NEW
│   └── security-model-fence.bats   # EXISTING — must still pass unmodified (regression pin)
└── test_harness_audit.py           # NEW (top-level tests/, matching tests/test_agent_catalog.py precedent)
```

### Pattern 1: PreToolUse hook stdin/stdout JSON contract

**What:** Read the full PreToolUse tool_use JSON on stdin (`INPUT=$(cat)`),
process/mutate with a `python3 - "$INPUT" <<'PY' ... PY` heredoc, print the
(possibly modified) JSON to stdout, exit 0.

**When to use:** `scripts/hooks/delegation-enforcer.sh` — this is the exact
shape `scripts/hooks/gsd-phase-evidence-gate.sh` already uses (minus the
modification part — that hook only blocks/passes, never rewrites).

**Example (existing pattern to copy, block-only variant):**
```bash
# Source: scripts/hooks/gsd-phase-evidence-gate.sh (this repo), lines 15-21
set -euo pipefail
[ "${GATES_BYPASS:-0}" = "1" ] && exit 0
INPUT=$(cat)
python3 - "$INPUT" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
...
PY
```

**Claude Code's actual modification contract** `[CITED: code.claude.com/docs/en/hooks]`:
a PreToolUse hook that exits 0 and prints JSON to stdout with a
`hookSpecificOutput` object containing `hookEventName: "PreToolUse"`,
`permissionDecision` (`"allow"|"deny"|"ask"`), and `updatedInput` will have
`updatedInput` merged into the tool's actual input before execution. This is
the mechanism `delegation-enforcer.sh` must emit to satisfy "the hook injects
the pin ... and the spawn proceeds at the pinned tier" (spec.md BDD, US1
happy path). No other hook in this repo currently exercises this contract —
this is genuinely new ground for the codebase, not a copy-paste. Plan.md's
own Risk list already flags this ("Hook JSON contract ... varies by Claude
Code version — enforcer must passthrough-on-any-doubt").

**Concrete field names confirmed from this repo's Task-tool usage
`[VERIFIED: skills/review-gate/SKILL.md:277, "Task({ subagent_type:
'gsd-verifier', model: 'sonnet', prompt: ... })"]`:** the tool_input carries
`subagent_type` (e.g. `"gsd-verifier"`) and, when pinned, `model` (e.g.
`"sonnet"`). `.planning/config.json`'s `model_overrides` object is keyed by
role name matching `subagent_type` exactly
(`[VERIFIED: .planning/config.json — "gsd-executor": "sonnet",
"gsd-verifier": "opus", etc.]`). Resolution logic: read
`tool_input.subagent_type`, if `tool_input.model` absent, look up
`model_overrides[subagent_type]`, inject as `tool_input.model` if found.

### Pattern 2: Sourceable, side-effect-free bash lib

**What:** A `.sh` file with only function/variable definitions, safe to
`source` under `set -u`, documented as "no side effects on source."

**When to use:** `scripts/gsd/security-surface.sh` (new) — must mirror
`scripts/gsd/adversary-host.sh`'s doc comment convention exactly, since
`security-model-fence.sh` AND `review-tier.sh` will both source it.

**Example:**
```bash
# Source: scripts/gsd/adversary-host.sh (this repo), lines 1-9
#!/usr/bin/env bash
# adversary-host.sh — sourceable lib: ...
# Sourced by plan-adversary.sh and qa-coverage-adversary.sh. No side effects
# on source (function definitions only) — safe to source under `set -u`.
```

**Exact extraction target** — the ENTIRE security-surface definition in
`security-model-fence.sh` is one line
`[VERIFIED: scripts/gsd/security-model-fence.sh:32]`:
```bash
KEYWORDS='auth|rls|row[ _-]?level|payment|stripe|crypto|jwt|jwks|oauth|owasp|secret|credential|password'
```
Extraction plan: move this line (rename if desired, e.g.
`SECURITY_SURFACE_PATTERN`) into `scripts/gsd/security-surface.sh` as a
sourceable variable/function; `security-model-fence.sh` sources it and uses
the same variable name it already has, so its own logic (line 45: `grep -Eiq
"$KEYWORDS" $SCAN_FILES`) is untouched — this satisfies the "fence behavior
unchanged" regression pin (`tests/bats/security-model-fence.bats`, 6 tests,
must still pass byte-for-byte). Do not touch any other line of
`security-model-fence.sh`.

### Pattern 3: gates.py evidence-store extension (findings-queue)

**What:** Add a new top-level namespace key to the SAME JSON store gates.py
already owns, reusing `_load_store` / `_save_store` / `_StoreLock`
verbatim — do not add a second file, a second lock, or a second
read/write helper.

**When to use:** `lib/gates.py` findings-queue (REQ-06).

**Existing store precedent to copy** `[VERIFIED: lib/gates.py:604-644]`: the
`grant`/`pending` commands already namespace unrelated state under a
top-level `_autonomy` key (`data.setdefault("_autonomy", {}).setdefault(run_id,
{})`), proving the store already supports multiple independent namespaces
without collision. `findings-queue` should do the same:
`data.setdefault("findings", [])` — a flat list of finding dicts, since
findings aren't run-scoped like `_autonomy` (spec.md doesn't scope
findings by run_id; INT-002 explicitly says "unrelated store keys untouched"
and "cross-subcommand isolation" from `pending`).

**Concrete pattern to copy for the three new functions** (mirrors
`record_pending`/`list_pending`/`grant_actions` shape exactly):
```python
# Source: lib/gates.py:628-641 (record_pending), this repo — copy this shape
def findings_add(store: Path, file: str, issue: str) -> str:
    sig = hashlib.sha256(f"{file}:{_normalize(issue)}".encode()).hexdigest()[:16]
    with _StoreLock(store):
        data = _load_store(store)
        findings = data.setdefault("findings", [])
        if not any(f["sig"] == sig for f in findings):
            findings.append({"sig": sig, "file": file, "issue": issue,
                             "resolved": False, "recorded_at": _now()})
        _save_store(store, data)
    return sig
```
(`_now()` already exists in gates.py — confirmed used by `grant_actions` at
line 604 and `preflight_check` at line 660; reuse it, do not reinvent
timestamps.)

**CLI dispatch:** `main()` is a hand-rolled `if cmd == "...":` chain (NOT
argparse subparsers, except the one-off `proof` command which builds a local
`argparse.ArgumentParser`). Add `findings-queue` as one `if cmd ==
"findings-queue":` arm, then dispatch on `args[0]` (`add|list|resolve`) —
this matches how `pending` already branches on whether `--action` was
passed vs a bare list call (`lib/gates.py:668-682`). Use the existing
`_flag(args, name, default)` helper for flag parsing (`lib/gates.py:772`),
not a new parser.

**Signature stability requirement (REQ-06):** `sha256(file +
normalized-issue)`. "Normalize" is undefined in spec.md/plan.md beyond the
dedup test ("same finding twice -> one entry" / EDGE-004 "same file +
reworded issue -> distinct signatures... dedup is exact-normalized, not
fuzzy"). Recommendation: lowercase + collapse whitespace only (`" ".join(s.split()).lower()`)
— matches the EDGE-004 explicit statement that fuzzy dedup is NOT wanted.

### Pattern 4: Findings-queue-adjacent — the `ship:gsd` grant convention (for liveness-check.sh signal 3)

**What:** "granted unconsumed ship action in flight" (AC-011 signal 3) maps
directly onto the EXISTING typed-grant convention, not a new one.

**Exact existing call to replicate** `[VERIFIED: scripts/gsd/review-gate-command.sh:30-44]`:
```bash
GATES_PY=""
for candidate in \
  "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$REPO_ROOT/lib/gates.py"; do
  [ -f "$candidate" ] && GATES_PY="$candidate" && break
done
python3 "$GATES_PY" check-grant "$RUN_ID" --action "ship:gsd"
```
`RUN_ID` derivation convention (same file, lines 21-25): env `GSD_RUN_ID` if
set, else branch-derived `spec-NNN` from `git branch --show-current`'s
leading 3-digit prefix; underivable -> fail closed. `liveness-check.sh`
should reuse this exact 3-candidate `GATES_PY` search and `RUN_ID`
derivation rather than inventing its own path-resolution logic.

### Pattern 5: Shell-script conventions (all levers must match)

`[VERIFIED: grep across scripts/gsd/*.sh, scripts/hooks/*.sh]`:
- Shebang: `#!/usr/bin/env bash` (100% of existing scripts).
- Header comment block: `<script-name>.sh — <one-line purpose>` then a
  paragraph explaining the WHY (not just the what), then a `# Usage:` line.
- `set -uo pipefail` is the dominant convention (7 of 9 scripts); only
  `canary-gate.sh` and `model-fallback.sh` use `set -euo pipefail`. Prefer
  `set -uo pipefail` for the new levers UNLESS the lever's own logic wants
  fail-fast semantics — `gsd-phase-evidence-gate.sh` (a hook) uses `set
  -euo pipefail` because a hook should abort loudly, not warn-and-fall-through.
  Recommendation: hooks use `set -euo pipefail`; standalone `scripts/gsd/*.sh`
  levers use `set -uo pipefail` (matches `security-model-fence.sh` itself,
  the sibling this phase is extracting from).
- Bash 3.2 safety: zero uses of `declare -A`, `local -n`, `${var,,}`,
  `${var^^}`, or `mapfile`/`readarray` anywhere in `scripts/gsd/` or
  `scripts/hooks/` `[VERIFIED: grep -rn]`. New levers must stay bash-3.2-safe
  (macOS ships 3.2 by default; this repo explicitly targets it).
- Fail-soft convention: `security-model-fence.sh` and `model-fallback.sh`
  both document "fail-soft + never silent" — WARN on stderr, exit 0. This is
  the pattern `learnings-harvest.sh` (AC-003: "always exit 0") and
  `delegation-enforcer.sh` (AC-001: "Exit 0 in all non-usage cases") must
  both follow.
- Atomic writes: gates.py's `_save_store` uses tempfile-in-same-dir +
  `os.replace` (never a torn file, never follows a pre-planted symlink).
  `learnings-harvest.sh`'s archive-fallback append (plan.md: "atomic
  (tmp+mv)") should mirror this in bash: `printf '%s\n' "$line" >>
  "$TMPFILE" && mv "$TMPFILE" "$ARCHIVE"` is NOT equivalent for an
  APPEND — for JSONL append-only files the simplest correct approach is
  `flock`-guarded direct append (see `append_result()` in gates.py, which is
  intentionally append-only and unlocked because it's a log, not a
  read-modify-write store) OR reuse `_StoreLock`-style flock if concurrent
  writers are a real risk. Recommend flock via `flock` command if available,
  else accept the same advisory-lock risk `_StoreLock` already accepts.

### Anti-Patterns to Avoid

- **Second evidence store:** do not create a new JSON file or lock for
  findings — `lib/gates.py`'s existing `_load_store`/`_save_store`/
  `_StoreLock` must be reused verbatim (plan.md Risk 2, explicit).
- **Blocking exit codes on advisory levers:** `delegation-enforcer.sh`,
  `learnings-harvest.sh`, and `harness-audit.py`'s preflight integration are
  ALL advisory-only per their ACs — never `exit 1`/`exit 2` from these in a
  way that would block the orchestrator. `liveness-check.sh` is the one
  exception that legitimately uses exit 0/1 as its actual return contract
  (AC-011: "exit 0 alive / 1 dead").
- **Hand-rolling YAML frontmatter parsing:** `harness-audit.py` needs to read
  `version:` from `SKILL.md` frontmatter for drift detection — do not add a
  PyYAML dependency or write a new parser; `lib/agents_manifest.py` already
  has `_parse_frontmatter(path) -> dict` (regex-based, no third-party
  import) `[VERIFIED: lib/agents_manifest.py:80-100]` — import/copy this
  function's approach.
- **Path-content confusion for "security-surface path":** `security-model-fence.sh`'s
  `KEYWORDS` regex matches file CONTENT (grep over `.planning/*.md` text),
  not file PATHS. `review-tier.sh`'s FULL-tier trigger is "any
  security-surface path" (AC-004) — this is a different matching mode
  (matching changed FILE PATHS, e.g. `auth/`, `payment/`) than what the
  shared regex was designed for. See Open Questions — do not silently reuse
  the content-regex as a path-glob without confirming the match target.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| JSON evidence store read/write/lock | A second store class or file | `lib/gates.py`'s `_load_store`/`_save_store`/`_StoreLock` | Race-safety (fcntl.flock) and atomic-write (tempfile+os.replace) already solved and tested |
| YAML/frontmatter parsing | New regex or PyYAML dependency | `lib/agents_manifest.py::_parse_frontmatter` | Already handles the unclosed-block edge case (codex v3.19 round 1 fix) |
| GATES_PY path resolution | New search logic per script | The 3-candidate loop in `scripts/gsd/review-gate-command.sh:31-36` | Already covers repo-root / vendored / packaged install locations |
| RUN_ID derivation | New env-var scheme | `GSD_RUN_ID` env fallback to branch-derived `spec-NNN`, `scripts/gsd/review-gate-command.sh:21-25` | Consistency across every lever that needs a run identity |
| CLI arg parsing in gates.py | argparse subparsers for findings-queue | The existing hand-rolled `if cmd == "...":` dispatch + `_flag()` helper | Matches every other gates.py subcommand except the one that legitimately needed argparse (`proof`, for its `--defer` repeat-flag complexity) |

**Key insight:** Every "don't hand-roll" item above is not a third-party
library recommendation — it's "this repo already solved this exact problem
three lines away, copy that." Phase 1's actual risk is NOT missing a
library; it's silently diverging from an established local convention that
the plan-checker or reviewer will flag as inconsistent.

## Runtime State Inventory

Not applicable — Phase 1 is greenfield lever construction (new files +
one line changed in `security-model-fence.sh`), not a rename/refactor/
migration. No stored data, live service config, OS-registered state, or
build artifacts carry names that this phase changes.

## Common Pitfalls

### Pitfall 1: Assuming `scripts/gsd/run_state/` exists
**What goes wrong:** A plan task says "read run-state from
`scripts/gsd/run_state/`" per plan.md's literal wording, and the task fails
immediately because the directory doesn't exist.
**Why it happens:** plan.md's architecture note describes an aspirational
convention name that collides with an unrelated existing module
(`lib/run_state/`, a SQLite CLI for `/feature`/`/fix` tracking with no `pid`
column).
**How to avoid:** Treat `liveness-check.sh`'s pid/mtime inputs as a NEW
contract this phase must define (e.g., a pidfile path + a directory to
mtime-scan, passed as CLI args or env vars) — do not attempt to wire it to
`lib/run_state`'s SQLite store, which has no pid tracking at all.
**Warning signs:** Any task description referencing "run_state" without
specifying whether it means `lib/run_state/` (SQLite, exists) or a
new bash-side convention (does not exist).

### Pitfall 2: PreToolUse hook that blocks instead of modifying
**What goes wrong:** `delegation-enforcer.sh` is written using this repo's
only existing hook pattern (`gsd-phase-evidence-gate.sh`, which only ever
exits 0 or 2) and ends up unable to inject `model` into the spawn — it can
only allow or block, never modify.
**Why it happens:** Every existing PreToolUse hook in this repo is
block-only; none exercise Claude Code's `hookSpecificOutput.updatedInput`
modification contract `[CITED: code.claude.com/docs/en/hooks]`.
**How to avoid:** Emit the modification-shaped JSON (`hookSpecificOutput`
with `updatedInput`) on stdout with exit 0, per Pattern 1 above — verify
against the live PATH-001 bats stub in spec.md (`jq -e
'.tool_input.model == "sonnet"'`) which asserts the OUTER shape is
`.tool_input.model`, not `.hookSpecificOutput.updatedInput.model` — this
is a real ambiguity to resolve during planning (see Open Questions).
**Warning signs:** bats test asserts on `.tool_input.model` but the hook
emits `.hookSpecificOutput.updatedInput.model` (or vice versa) — schema
mismatch, test never actually proves the injection worked end-to-end
against Claude Code's real hook contract (since bats only checks the
script's own stdout shape, not live hook execution).

### Pitfall 3: Extraction accidentally changing fence behavior
**What goes wrong:** Extracting `KEYWORDS` into `security-surface.sh`
subtly changes quoting/escaping (e.g., losing the single-quote literal
string, or the `[ _-]?` character class) and `tests/bats/security-model-fence.bats`
silently starts matching different text.
**Why it happens:** Shell variable re-export across `source` boundaries can
mangle special characters if not quoted identically.
**How to avoid:** Move the line verbatim; `security-model-fence.sh`'s only
change should be replacing the `KEYWORDS='...'` literal with a `source
"$(dirname "${BASH_SOURCE[0]}")/security-surface.sh"` line (or equivalent),
keeping the variable NAME (`KEYWORDS`) unchanged so line 45's `grep -Eiq
"$KEYWORDS"` needs zero further edits. Run `tests/bats/security-model-fence.bats`
after the extraction — it is the regression pin (Phase 1 success criterion 4).
**Warning signs:** Any diff to `security-model-fence.sh` beyond the single
line replacing the `KEYWORDS=` assignment with a `source` call.

### Pitfall 4: Hash-signature normalization drifting from EDGE-004
**What goes wrong:** `findings_add`'s normalization is too aggressive (e.g.
stemming, synonym folding) and two genuinely different findings collapse
onto the same signature, silently dropping a real finding.
**Why it happens:** "Normalize" is underspecified in spec.md beyond "not
fuzzy."
**How to avoid:** Keep normalization to whitespace-collapse + lowercase
only. EDGE-004 explicitly states reworded issues on the same file getting
distinct signatures is a "documented limitation," i.e., desired/accepted
behavior, not a bug to over-engineer away.
**Warning signs:** A test asserting two differently-worded findings on the
same file dedupe to one signature — that would contradict EDGE-004.

## Code Examples

### PreToolUse hook reading stdin JSON (existing pattern, block-only variant)
```bash
# Source: scripts/hooks/gsd-phase-evidence-gate.sh (this repo)
set -euo pipefail
[ "${GATES_BYPASS:-0}" = "1" ] && exit 0
INPUT=$(cat)
python3 - "$INPUT" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
ti = data.get("tool_input", {}) or {}
# ... inspect / decide ...
sys.exit(0)  # or sys.exit(2) to block
PY
```

### Evidence-store namespaced extension (existing pattern to copy for findings)
```python
# Source: lib/gates.py:628-644 (record_pending / list_pending), this repo
def record_pending(store: Path, run_id: str, action: str, reason: str) -> bool:
    if not ACTION_PAT.match(action):
        return False
    with _StoreLock(store):
        data = _load_store(store)
        auto = data.setdefault("_autonomy", {}).setdefault(run_id, {})
        pending = auto.setdefault("pending", [])
        if not any(p["action"] == action for p in pending):
            pending.append({"action": action, "reason": sanitize_reason(reason),
                            "recorded_at": _now()})
        _save_store(store, data)
    return True
```

### Frontmatter version read (existing pattern for harness-audit.py to reuse)
```python
# Source: lib/agents_manifest.py:80-100, this repo
def _parse_frontmatter(path: Path) -> dict:
    fields: dict[str, str] = {}
    lines = path.read_text(errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return fields
    closed = False
    for line in lines[1:200]:
        if line.strip() == "---":
            closed = True
            break
        m = FRONTMATTER_FIELD.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fields if closed else {}
```

## State of the Art

Not applicable in the "library upgraded" sense — this is a first-party
codebase with no external framework migrations in scope. The one relevant
"state of the art" fact is Claude Code's hook `updatedInput` modification
contract, which is a currently-documented (not deprecated) feature
`[CITED: code.claude.com/docs/en/hooks]` this repo has not yet exercised
anywhere.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Claude Code's PreToolUse hook `hookSpecificOutput.updatedInput` mechanism is what makes "the spawn proceeds at the pinned tier" (spec.md US1) actually work at runtime, as opposed to the hook merely printing modified JSON that nothing consumes | Architecture Patterns, Pattern 1 | If wrong, `delegation-enforcer.sh` can be built and bats-tested (proving its own stdout shape) but never actually pin a real spawn — the phase's own success criteria (bats-only) would still pass while the feature is inert. Recommend spec-decompose/planner confirm this against a live Claude Code hook test, or treat PATH-001's bats assertion as testing the SCRIPT's contract only (not live hook wiring, which is Phase 2/3 territory anyway since delegation-enforcer registration in `.claude/settings.json` is explicitly a consumer-repo action per plan.md, "FFS ships the script + registration snippet in docs, never writes consumer settings") |
| A2 | "security-surface path" (AC-004, review-tier FULL trigger) means matching CHANGED FILE PATHS against a path-shaped variant of the keyword list, not re-running the content-grep from security-model-fence.sh against diff content | Architecture Patterns, Anti-Patterns | If wrong, review-tier.sh either double-scans file content (slow, and semantically different from "security-surface path") or fails to trigger FULL tier on files that live in security-relevant directories but whose content doesn't literally contain the keyword strings |
| A3 | `liveness-check.sh` should accept a pidfile PATH (not a bare PID) as input, based on the Unit Test List wording "garbage/missing pid file" | Common Pitfalls, Pitfall 1 | If wrong (e.g. it should take a raw PID), the bats fixture design and CLI contract in the plan would need reshaping, though the underlying signal logic (kill -0 check) stays the same either way |

## Open Questions

1. **Does `delegation-enforcer.sh` need to emit `hookSpecificOutput.updatedInput`, or just a modified `tool_input.model` on stdout?**
   - What we know: spec.md's own bats stub asserts `jq -e '.tool_input.model == "sonnet"'` on the hook's raw stdout — NOT `.hookSpecificOutput.updatedInput.model`. This suggests the spec's own test intends the hook's stdout to BE the full modified input JSON (echoing the original envelope with `tool_input.model` set), which is simpler than the full `hookSpecificOutput` wrapper.
   - What's unclear: whether Claude Code's real runtime honors a bare modified-`tool_input` echo on stdout, or strictly requires the `hookSpecificOutput` wrapper to actually re-inject the model.
   - Recommendation: planner should have the executor implement to satisfy the bats stub literally (echo the full input JSON with `tool_input.model` injected) AND additionally research/confirm the live Claude Code hook JSON contract before Phase 2 wires real registration — Phase 1 only needs the SCRIPT to be correct and testable in isolation (no live Claude Code process in the loop), so this doesn't block Phase 1, but should be flagged for Phase 2/3.

2. **What does `security-surface path` matching look like concretely in `review-tier.sh`?**
   - What we know: AC-004 says "full = >20 files OR security-surface OR migration"; the shared pattern list is sourced from `security-surface.sh`. spec.md's PATH-003 fixture is "2-file auth-touching" diff.
   - What's unclear: does "auth-touching" mean a changed file whose PATH contains "auth" (e.g. `lib/auth/session.py`), or a changed file whose CONTENT (diff hunk text) matches the keyword regex?
   - Recommendation: match against changed FILE PATHS (`git diff --name-only`) using the same keyword list as a path-substring/glob test — this is cheaper (no diff-content scan needed) and matches the "2-file auth-touching" fixture description most naturally (a file literally named/pathed around auth). Planner should pin this as an explicit design decision in the Phase 1 plan rather than leaving it to the executor's interpretation.

3. **What exact CLI/env contract does `liveness-check.sh` expose for its pid/mtime inputs?**
   - What we know: no existing pidfile-writing convention exists anywhere in this repo to consume (see Common Pitfalls Pitfall 1). The Unit Test List implies a "pid file" (a file containing a PID) and a "state dir" for mtime scanning.
   - What's unclear: the exact flag names/positional-arg order plan.md doesn't specify beyond prose ("consumes run-state dir + optional GATES_STORE").
   - Recommendation: define a self-contained CLI: `liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]`, defaulting `--window-min` to 30 per AC-011, and reusing the `GATES_PY`/`RUN_ID` resolution pattern from Pattern 4 for the ship-grant signal. Since this is a standalone lever with no cross-deps (Phase 1 goal: "no cross-deps, max wave width"), its bats fixtures can freely mock the pidfile/state-dir without needing any other Phase 1 lever to exist first.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| bash | all 5 bash levers | ✓ | 5.3.9 (dev env); scripts must stay 3.2-safe | — |
| python3 | gates.py findings-queue, harness-audit.py | ✓ | 3.14.6 | — |
| bats-core | all `.bats` tests | ✓ | present at `/opt/homebrew/bin/bats` | — |
| shellcheck | Phase 1 gate criterion 3 | ✓ | 0.11.0 | — |
| jq | bats JSON assertions | ✓ | 1.7.1 | — |
| gbrain | `learnings-harvest.sh` backend probe | ✓ (this dev env only — NOT guaranteed on CI/consumer machines) | present | archive-fallback append to `.feature-fix-swarm/learnings-archive.jsonl` (already required by AC-003 regardless) |

**Missing dependencies with no fallback:** none — every dependency in this
phase either is present or already has a documented fallback in the spec
itself (gbrain absence is an explicit, tested edge case per PATH-002).

**Missing dependencies with fallback:** gbrain (fallback: local JSONL
archive append, already the AC-003-mandated behavior, not a gap this phase
introduces).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (Python), bats-core (bash) — dual framework, matching every existing lever pair in this repo |
| Config file | none — pytest auto-discovers `test_*.py` under both `tests/` and `lib/tests/`; bats has no config, invoked directly against file paths |
| Quick run command | `bats tests/bats/<new>.bats` / `python3 -m pytest lib/tests/test_findings_queue.py tests/test_harness_audit.py -q` |
| Full suite command | `python3 -m pytest -q && bats tests/bats/` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| REQ-01 | delegation-enforcer inject/passthrough/warn/exit-0 | bats | `bats tests/bats/delegation-enforcer.bats` | ❌ Wave 0 |
| REQ-02 | `DELEGATION_ENFORCER=off` passthrough | bats | `bats tests/bats/delegation-enforcer.bats` | ❌ Wave 0 (same file as REQ-01) |
| REQ-03 (lever half) | learnings-harvest glob/gbrain/archive/exit-0 | bats | `bats tests/bats/learnings-harvest.bats` | ❌ Wave 0 |
| REQ-04 (lever half) | review-tier + security-surface extraction | bats | `bats tests/bats/review-tier.bats` + `bats tests/bats/security-model-fence.bats` (regression pin) | review-tier.bats ❌ Wave 0; security-model-fence.bats ✅ exists |
| REQ-05 | `REVIEW_TIER` env override | bats | `bats tests/bats/review-tier.bats` | ❌ Wave 0 (same file as REQ-04) |
| REQ-06 | findings-queue add/list/resolve | pytest | `python3 -m pytest lib/tests/test_findings_queue.py -q` | ❌ Wave 0 |
| REQ-08 (lever half) | harness-audit scoring + `--json` | pytest | `python3 -m pytest tests/test_harness_audit.py -q` | ❌ Wave 0 |
| REQ-11 (lever half) | liveness-check truth table | bats | `bats tests/bats/liveness-check.bats` | ❌ Wave 0 |
| REQ-12 (cross-cutting, partial) | shellcheck-clean + baseline suites hold | shellcheck + full suite | `shellcheck scripts/gsd/review-tier.sh scripts/gsd/security-surface.sh scripts/gsd/liveness-check.sh scripts/gsd/learnings-harvest.sh scripts/hooks/delegation-enforcer.sh && python3 -m pytest -q` | N/A (gate, not a single test file) |

### Sampling Rate
- **Per task commit:** run the single new `.bats`/`test_*.py` file for that lever + `shellcheck <that-file>` if bash.
- **Per wave merge:** `bats tests/bats/security-model-fence.bats` (regression pin, cheap) + the full new-lever bats/pytest set.
- **Phase gate:** the exact 5 commands listed in ROADMAP.md's Phase 1 Success Criteria, run in order, all must exit 0 / meet count thresholds (pytest ≥231 confirmed as CURRENT baseline — this phase must not regress it and should land at 231 + N_new_pytest_tests).

### Wave 0 Gaps
- [ ] `tests/bats/delegation-enforcer.bats` — covers REQ-01, REQ-02
- [ ] `tests/bats/review-tier.bats` — covers REQ-04 (lever half), REQ-05
- [ ] `tests/bats/liveness-check.bats` — covers REQ-11 (lever half)
- [ ] `tests/bats/learnings-harvest.bats` — covers REQ-03 (lever half)
- [ ] `lib/tests/test_findings_queue.py` — covers REQ-06
- [ ] `tests/test_harness_audit.py` — covers REQ-08 (lever half)
- [ ] No new shared fixtures/conftest needed — every lever is independently testable per-file, matching the "no cross-deps, max wave width" Phase 1 goal. `lib/tests/test_gates.py`'s `_load(name)` importlib helper (loads `gates.py` by file path, not package import) should be copied verbatim into `test_findings_queue.py` if it needs the same dynamic-import approach — confirm by checking whether `lib/tests/test_findings_queue.py` can simply `from gates import ...` (depends on whether `lib/` is a package with `__init__.py`; `lib/tests/test_gates.py` uses the importlib-by-path approach specifically to avoid this question, recommend copying that, not re-deciding it).
- [ ] Framework install: none — bats/pytest/shellcheck already present.

## Security Domain

`.planning/config.json` has `"security_enforcement": true, "security_asvs_level": 1, "security_block_on": "high"` — section required.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | no auth surface in this phase |
| V3 Session Management | no | — |
| V4 Access Control | yes (narrow) | `delegation-enforcer.sh` and `liveness-check.sh`'s ship-grant check gate PRIVILEGED actions (model tier / ship authority) — but both are fail-open/advisory-only per spec (AC-001 "never blocks"), so this is an availability/cost-control concern, not an access-control boundary in the ASVS sense |
| V5 Input Validation | yes | all five new scripts parse untrusted-ish input: hook stdin JSON (attacker-adjacent — a compromised sub-agent transcript could feed malformed JSON), `.planning/config.json` (local file, still validate JSON parse failures fail-soft per existing `security-model-fence.sh` convention), findings-queue `--sig`/`file`/`issue` args (must not path-traverse or shell-inject — reuse `sanitize_reason()` pattern from gates.py for any printed/stored free text) |
| V6 Cryptography | no | findings-queue uses `hashlib.sha256` for a stable dedup signature, NOT a security/cryptographic guarantee — no secret material involved |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Malformed/hostile JSON on hook stdin (a sub-agent transcript or tool_use payload is attacker-influenceable in principle) | Tampering | `try: json.loads(...) except: sys.exit(0)` fail-open pattern already used by `gsd-phase-evidence-gate.sh` — delegation-enforcer.sh must do the same (never crash, never block on parse failure, per AC-001 "without config, passthrough + stderr warn") |
| Symlink-planted output path (findings-queue writes, learnings-archive writes) | Tampering | gates.py's `_save_store` already writes via tempfile-in-same-dir + `os.replace` (swaps the symlink itself rather than following it into a target write) — reuse this, do not write directly to the target path with `open(path, "w")` |
| Path traversal via findings-queue `file` argument (e.g. `../../etc/passwd` as the "file" in a finding) | Tampering | findings are metadata (a string field describing which file had an issue), not a path gates.py opens/writes — no traversal risk as long as `file` stays a free-text/display field only, never passed to `open()`/`Path()` directly. Confirm this constraint holds when the planner writes the findings_add signature. |
| `run_id`-controlled filenames (mirrors gates.py's own documented CVE-shaped fix for `proof`) | Tampering | gates.py's `proof` command already sanitizes `run_id` via `re.sub(r"[^A-Za-z0-9._-]", "_", run_id)` before using it in a filename (`lib/gates.py:923-926`) — if any new lever derives a filename from an operator/env-controlled `run_id` (e.g. `liveness-check.sh`'s state-dir lookup), apply the same sanitization, don't assume `run_id` is trusted just because it usually comes from `GSD_RUN_ID`/branch name. |

## Sources

### Primary (HIGH confidence — verified via direct tool read/grep of this repo)
- `specs/003-orchestration-hardening/spec.md` — ACs, BDD scenarios, edge cases (authoritative, locked)
- `specs/003-orchestration-hardening/plan.md` — architecture notes, unit test list, phase breakdown (authoritative, locked)
- `specs/003-orchestration-hardening/prior-art.md` — PORT decision + citations (authoritative, locked)
- `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md` — REQ IDs, Phase 1 success criteria
- `scripts/gsd/security-model-fence.sh` (read in full) — extraction source, line 32 KEYWORDS
- `tests/bats/security-model-fence.bats` (read in full) — 6 existing tests, regression pin
- `lib/gates.py` (read in full, 1027 lines) — evidence store, CLI dispatch, grant/pending pattern
- `lib/run_state/README.md`, `lib/run_state/state.py`, `lib/run_state/cli.py` — confirmed this is NOT the liveness-check.sh data source
- `lib/agents_manifest.py` — `_parse_frontmatter` reuse candidate
- `scripts/hooks/gsd-phase-evidence-gate.sh`, `hooks/tdd-gate.sh` — existing PreToolUse hook patterns
- `scripts/gsd/review-gate-command.sh` — `GATES_PY` resolution + `ship:gsd` grant convention + `RUN_ID` derivation
- `scripts/gsd/adversary-host.sh` — sourceable-lib doc-comment convention
- `.claude/settings.json` — hook registration format
- `skills/adopt-wip/SKILL.md` — confirms current (pre-Phase-1) liveness detection is prose-only, no script
- `skills/review-gate/SKILL.md:277` — confirms `subagent_type`/`model` Task tool field names
- `.planning/config.json` — `model_overrides` schema, `security_enforcement` config
- direct execution: `python3 -m pytest -q` -> "Pytest: 231 passed"; `bats tests/bats/` -> 64 tests, all ok

### Secondary (MEDIUM confidence)
- `[CITED: code.claude.com/docs/en/hooks]` — PreToolUse `hookSpecificOutput.updatedInput` modification contract (via WebSearch synthesis of the official docs page; not independently fetched/read line-by-line in this session — flagged in Open Questions as needing confirmation before Phase 2/3 wiring)

### Tertiary (LOW confidence)
- none

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new dependencies, entirely grounded in existing repo conventions
- Architecture: HIGH for 4 of 6 levers (direct precedent found for each); MEDIUM for `delegation-enforcer.sh`'s live hook-injection contract (external, uncited-in-repo) and `review-tier.sh`'s security-surface-path semantics (genuine design gap, flagged as Open Question)
- Pitfalls: HIGH — all four pitfalls are grounded in a specific discrepancy or ambiguity found via direct repo inspection, not speculative

**Research date:** 2026-07-10
**Valid until:** 30 days (stable first-party codebase; the one external citation — Claude Code hook contract — should be re-checked if Claude Code is upgraded before Phase 2/3 execution)
